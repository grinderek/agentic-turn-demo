import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .engine import Engine
from .models import Turn, TurnRequest
from .providers import AnthropicProvider, Provider, ScriptedProvider
from .store import Conflict, Store
from .tools import CALENDAR, EMAILS


def create_app(store: Store | None = None, provider: Provider | None = None) -> FastAPI:
    store = store or Store(os.getenv("DEMO_DB", "data/demo.sqlite3"))
    if provider is None:
        mode = os.getenv("LLM_PROVIDER", "demo")
        if mode == "demo":
            provider = ScriptedProvider()
        elif mode == "anthropic":
            key, model = os.getenv("ANTHROPIC_API_KEY"), os.getenv("ANTHROPIC_MODEL")
            if not key or not model:
                raise RuntimeError("Anthropic mode requires ANTHROPIC_API_KEY and ANTHROPIC_MODEL")
            provider = AnthropicProvider(key, model)
        else:
            raise RuntimeError("LLM_PROVIDER must be demo or anthropic")
    engine = Engine(store, provider)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.recover()
        yield
        await engine.close()
        store.close()

    app = FastAPI(title="Agentic Turn Demo", version="0.1.0", lifespan=lifespan)
    app.state.engine, app.state.store = engine, store
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    async def session(x_demo_session: Annotated[str | None, Header()] = None) -> str:
        if not x_demo_session or not store.has_session(x_demo_session):
            raise HTTPException(401, "Create a demo session first")
        return x_demo_session

    Owner = Annotated[str, Depends(session)]

    def owned_conversation(cid: str, owner: str) -> None:
        if not store.owns(cid, owner):
            raise HTTPException(404, "Conversation not found")

    def owned_turn(tid: str, owner: str) -> Turn:
        turn = store.get_turn(tid)
        if not turn:
            raise HTTPException(404, "Turn not found")
        owned_conversation(turn.conversation_id, owner)
        return turn

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static / "index.html")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "provider": provider.name, "synthetic_data": True}

    @app.post("/api/sessions", status_code=201)
    async def new_session():
        return {"token": store.session()}

    @app.post("/api/conversations", status_code=201)
    async def conversation(owner: Owner):
        return {"id": store.conversation(owner)}

    @app.post("/api/conversations/{cid}/turns", status_code=202)
    async def start(cid: str, body: TurnRequest, owner: Owner):
        owned_conversation(cid, owner)
        if len(engine.running) >= 16:
            raise HTTPException(429, "Demo is busy, try again shortly")
        turn = Turn(conversation_id=cid, **body.model_dump())
        try:
            engine.start(turn, owner)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": turn.id, "status": "running"}

    @app.get("/api/conversations/{cid}/latest")
    async def latest(cid: str, owner: Owner):
        owned_conversation(cid, owner)
        return {"id": store.latest_turn_id(cid)}

    @app.get("/api/turns/{tid}")
    async def snapshot(tid: str, owner: Owner):
        turn = owned_turn(tid, owner)
        return {**turn.model_dump(), "actions": store.actions(turn.conversation_id, owner)}

    @app.post("/api/turns/{tid}/cancel", status_code=202)
    async def cancel(tid: str, owner: Owner):
        turn = owned_turn(tid, owner)
        if turn.status != "running" or not engine.cancel(tid):
            raise HTTPException(409, "Turn has already finished")
        return {"status": "cancellation_requested"}

    @app.get("/api/turns/{tid}/events", response_class=StreamingResponse)
    async def events(
        tid: str,
        owner: Owner,
        after: int = Query(0, ge=0),
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        turn = owned_turn(tid, owner)
        try:
            cursor = int(last_event_id) if last_event_id is not None else after
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be a sequence number") from exc
        if cursor < 0 or cursor > turn.seq:
            raise HTTPException(400, "Cursor is outside this turn's event log")

        async def frames():
            nonlocal cursor
            idle = 0
            while True:
                batch = store.events(tid, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
                current = store.get_turn(tid)
                if current and current.status != "running":
                    # Terminal frame and terminal snapshot commit in the same transaction.
                    return
                await asyncio.sleep(0.1)
                idle += 1
                if idle >= 150:
                    yield ": heartbeat\n\n"
                    idle = 0

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/sources/{sid}")
    async def source(sid: str, owner: Owner):
        record = next(
            (r for r in EMAILS + CALENDAR if r["id"] == sid and r["tenant"] == "demo"), None
        )
        if not record:
            raise HTTPException(404, "Source not found")
        return {k: v for k, v in record.items() if k != "tenant"}

    @app.post("/api/actions/{aid}/{decision}")
    async def decide(aid: str, decision: str, owner: Owner):
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "Decision not found")
        try:
            result = store.decide(aid, owner, approve=decision == "approve")
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if not result:
            raise HTTPException(404, "Draft not found")
        return result

    return app


app = create_app()
