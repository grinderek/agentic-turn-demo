import asyncio
import logging
from contextlib import suppress
from time import perf_counter
from typing import Any

from .models import ModelReply, Turn
from .providers import Provider
from .store import Store
from .tools import Context, Tools, data_block

logger = logging.getLogger(__name__)


class UserCancelled(Exception):
    pass


async def interruptible(awaitable, cancelled: asyncio.Event):
    """Cancellation interrupts a stalled provider read too, not just the next text delta."""
    operation = asyncio.ensure_future(awaitable)
    signal = asyncio.create_task(cancelled.wait())
    try:
        await asyncio.wait({operation, signal}, return_when=asyncio.FIRST_COMPLETED)
        if cancelled.is_set():
            raise UserCancelled
        return await operation
    finally:
        for task in (operation, signal):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, signal, return_exceptions=True)


class Engine:
    def __init__(
        self,
        store: Store,
        provider: Provider,
        tools: Tools | None = None,
        max_iterations: int = 6,
        timeout: float = 90,
    ):
        self.store, self.provider = store, provider
        self.tools = tools or Tools()
        self.max_iterations, self.timeout = max_iterations, timeout
        self.running: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    def start(self, turn: Turn, owner: str) -> None:
        self.store.create_turn(turn)
        cancelled = asyncio.Event()
        task = asyncio.create_task(self.run(turn, owner, cancelled))
        self.running[turn.id] = (task, cancelled)
        task.add_done_callback(lambda _: self.running.pop(turn.id, None))

    def cancel(self, tid: str) -> bool:
        entry = self.running.get(tid)
        if entry:
            entry[1].set()
        return entry is not None

    def audit(
        self, turn: Turn, phase: str, started: float, usage: dict | None, status: str = "complete"
    ) -> None:
        record = {
            "phase": phase,
            "model": self.provider.name,
            "duration_ms": round((perf_counter() - started) * 1000),
            "usage": usage,
            "status": status,
        }
        turn.calls.append(record)
        self.store.emit(turn, "model_call", **record)

    async def run(self, turn: Turn, owner: str, cancelled: asyncio.Event) -> None:
        self.store.emit(turn, "start")
        try:
            async with asyncio.timeout(self.timeout):
                await self.loop(turn, owner, cancelled)
        except UserCancelled:
            turn.status, turn.reason = "cancelled", "user_cancelled"
            self.store.discard(turn.id)
            self.store.emit(turn, "cancelled", reason=turn.reason)
        except asyncio.CancelledError:
            turn.status, turn.reason = "interrupted", "server_shutdown"
            self.store.discard(turn.id)
            self.store.emit(turn, "error", reason=turn.reason)
            raise
        except Exception as exc:
            logger.exception("Turn failed: %s", turn.id)
            turn.status = "error"
            turn.reason = (
                "turn_timeout"
                if isinstance(exc, TimeoutError)
                else (str(exc) if isinstance(exc, TurnFailure) else "provider_or_internal_error")
            )
            self.store.discard(turn.id)
            self.store.emit(turn, "error", reason=turn.reason)

    async def loop(self, turn: Turn, owner: str, cancelled: asyncio.Event) -> None:
        working = self.store.history(turn.conversation_id)
        pending = self.store.actions(turn.conversation_id, owner)
        import json

        working.append(
            {
                "role": "user",
                "content": turn.prompt
                + "\n<tool_data>Pending drafts: "
                + json.dumps(pending)
                + "</tool_data>",
            }
        )
        for step in range(self.max_iterations):
            if cancelled.is_set():
                raise UserCancelled
            started = perf_counter()
            reply = None
            stream = self.provider.stream(
                messages=working,
                tools=self.tools.definitions(),
                turn=turn,
                step=step,
                pending=pending,
            )
            try:
                while True:
                    try:
                        part = await interruptible(anext(stream), cancelled)
                    except StopAsyncIteration:
                        break
                    if isinstance(part, str):
                        turn.text += part
                        self.store.emit(turn, "delta", text=part)
                    else:
                        reply = part
            finally:
                with suppress(Exception):
                    await stream.aclose()
                self.audit(
                    turn,
                    f"iteration_{step + 1}",
                    started,
                    reply.usage if reply else None,
                    "complete" if reply else "incomplete",
                )
            if not isinstance(reply, ModelReply):
                raise TurnFailure("missing_model_result")
            if reply.stop_reason == "max_tokens":
                raise TurnFailure("output_truncated")
            if reply.calls:
                if reply.stop_reason != "tool_use":
                    raise TurnFailure("invalid_tool_stop")
                # Also bound fan-out in a single response, not only model iterations.
                if len(reply.calls) > 8:
                    raise TurnFailure("tool_call_limit")
                working.append({"role": "assistant", "content": reply.content})
                results = []
                for call in reply.calls:
                    tool_started = perf_counter()
                    result = await interruptible(
                        self.tools.call(
                            call.name, call.arguments, Context(owner, turn, self.store)
                        ),
                        cancelled,
                    )
                    turn.ledger = sorted(set(turn.ledger + result.source_ids))
                    record: dict[str, Any] = {
                        "name": call.name,
                        "arguments": call.arguments,
                        "error": result.error,
                        "source_ids": result.source_ids,
                        "duration_ms": round((perf_counter() - tool_started) * 1000),
                    }
                    turn.tools.append(record)
                    self.store.emit(turn, "tool", **record)
                    if result.action:
                        self.store.emit(
                            turn,
                            "staged",
                            action=result.action,
                            retired_action_id=result.retired_action_id,
                        )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": data_block(result),
                            "is_error": result.error,
                        }
                    )
                working.append({"role": "user", "content": results})
                pending = self.store.actions(turn.conversation_id, owner)
                continue
            if reply.stop_reason != "end_turn" or not turn.text.strip():
                raise TurnFailure("incomplete_answer")
            self.store.emit(turn, "answer_complete")
            await self.finalize(turn, cancelled)
            return
        raise TurnFailure("iteration_budget_exhausted")

    async def finalize(self, turn: Turn, cancelled: asyncio.Event) -> None:
        for repair in (False, True):
            started = perf_counter()
            result = None
            try:
                result = await interruptible(
                    self.provider.finalize(turn=turn, repair=repair), cancelled
                )
            finally:
                self.audit(
                    turn,
                    "citation_repair" if repair else "finalize",
                    started,
                    result.usage if result else None,
                    "complete" if result else "incomplete",
                )
            answer = result.answer
            invalid = [c.source_id for c in answer.citations if c.source_id not in turn.ledger]
            if invalid:
                self.store.emit(turn, "citation_rejected", source_ids=invalid, repair=repair)
            if invalid and not repair:
                continue
            turn.citations = [c for c in answer.citations if c.source_id in turn.ledger]
            turn.outcome, turn.status = answer.outcome, "done"
            self.store.emit(
                turn,
                "done",
                text=turn.text,
                outcome=turn.outcome,
                citations=[c.model_dump() for c in turn.citations],
            )
            return

    async def close(self) -> None:
        tasks = [entry[0] for entry in self.running.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.provider.close()


class TurnFailure(Exception):
    pass
