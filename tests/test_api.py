import asyncio
import json

import httpx
import pytest

from agentic_turn.api import create_app
from agentic_turn.providers import ScriptedProvider
from agentic_turn.store import Store


@pytest.fixture
async def client():
    store = Store(":memory:")
    app = create_app(store, ScriptedProvider(delay=0.001))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as value:
        yield value, app
    await app.state.engine.close()
    store.close()


async def identity(client):
    token = (await client.post("/api/sessions")).json()["token"]
    headers = {"X-Demo-Session": token}
    cid = (await client.post("/api/conversations", headers=headers)).json()["id"]
    return headers, cid


async def wait_done(app, tid):
    for _ in range(1000):
        turn = app.state.store.get_turn(tid)
        if turn.status != "running":
            return turn
        await asyncio.sleep(0.001)
    raise AssertionError("Turn did not finish")


async def test_api_snapshot_and_sse_resume_agree(client):
    http, app = client
    headers, cid = await identity(http)
    tid = (
        await http.post(
            f"/api/conversations/{cid}/turns", headers=headers, json={"prompt": "Prepare a reply"}
        )
    ).json()["id"]
    turn = await wait_done(app, tid)
    assert (await http.get(f"/api/conversations/{cid}/latest", headers=headers)).json()["id"] == tid
    response = await http.get(f"/api/turns/{tid}/events", headers=headers)
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == turn.text
    resumed = await http.get(f"/api/turns/{tid}/events", headers={**headers, "Last-Event-ID": "4"})
    replay = [
        json.loads(line[6:]) for line in resumed.text.splitlines() if line.startswith("data: ")
    ]
    assert replay == events[4:]
    snapshot = (await http.get(f"/api/turns/{tid}", headers=headers)).json()
    assert snapshot["seq"] == events[-1]["seq"]
    assert snapshot["actions"][0]["status"] == "pending"
    aid = snapshot["actions"][0]["id"]
    result = await http.post(f"/api/actions/{aid}/approve", headers=headers)
    assert result.json()["status"] == "executed_demo"
    assert (await http.post(f"/api/actions/{aid}/approve", headers=headers)).status_code == 409


async def test_every_owned_route_rejects_another_session(client):
    http, app = client
    first, cid = await identity(http)
    second, _ = await identity(http)
    tid = (
        await http.post(
            f"/api/conversations/{cid}/turns", headers=first, json={"prompt": "Prepare a reply"}
        )
    ).json()["id"]
    assert (
        await http.post(f"/api/conversations/{cid}/turns", headers=second, json={"prompt": "steal"})
    ).status_code == 404
    assert (await http.get(f"/api/turns/{tid}", headers=second)).status_code == 404
    assert (await http.get(f"/api/conversations/{cid}/latest", headers=second)).status_code == 404
    assert (await http.get(f"/api/turns/{tid}/events", headers=second)).status_code == 404
    assert (await http.post(f"/api/turns/{tid}/cancel", headers=second)).status_code == 404
    await wait_done(app, tid)
    aid = (await http.get(f"/api/turns/{tid}", headers=first)).json()["actions"][0]["id"]
    assert (await http.post(f"/api/actions/{aid}/approve", headers=second)).status_code == 404
    assert (await http.get("/api/sources/mail-other-001", headers=first)).status_code == 404


async def test_auth_validation_and_turn_conflict(client):
    http, app = client
    assert (await http.post("/api/conversations")).status_code == 401
    headers, cid = await identity(http)
    route = f"/api/conversations/{cid}/turns"
    assert (await http.post(route, headers=headers, json={"prompt": ""})).status_code == 422
    first = await http.post(route, headers=headers, json={"prompt": "reply"})
    assert first.status_code == 202
    assert (await http.post(route, headers=headers, json={"prompt": "again"})).status_code == 409
    tid = first.json()["id"]
    assert (await http.post(f"/api/turns/{tid}/cancel", headers=headers)).status_code == 202
    turn = await wait_done(app, tid)
    assert turn.status == "cancelled"
    assert (await http.post(f"/api/turns/{tid}/cancel", headers=headers)).status_code == 409
    assert (
        await http.get(f"/api/turns/{tid}/events?after=99999", headers=headers)
    ).status_code == 400


async def test_independent_conversations_can_run_concurrently(client):
    http, app = client
    first, cid1 = await identity(http)
    second, cid2 = await identity(http)
    r1 = await http.post(f"/api/conversations/{cid1}/turns", headers=first, json={"prompt": "one"})
    r2 = await http.post(f"/api/conversations/{cid2}/turns", headers=second, json={"prompt": "two"})
    t1, t2 = await asyncio.gather(wait_done(app, r1.json()["id"]), wait_done(app, r2.json()["id"]))
    assert t1.status == t2.status == "done"
    assert t1.id != t2.id
