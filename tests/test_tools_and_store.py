import pytest

from agentic_turn.models import Turn
from agentic_turn.store import Conflict, Store
from agentic_turn.tools import Context, Tools


@pytest.fixture
def context():
    store = Store(":memory:")
    owner = store.session()
    turn = Turn(conversation_id=store.conversation(owner), prompt="test")
    store.create_turn(turn)
    yield Context(owner, turn, store)
    store.close()


async def test_model_cannot_supply_identity(context):
    result = await Tools().call("search_emails", {"query": "Atlas", "tenant": "other"}, context)
    assert result.error


async def test_read_tools_hide_other_tenant_record(context):
    result = await Tools().call("read_source", {"source_id": "mail-other-001"}, context)
    assert result.content == {"records": []}
    found = await Tools().call("search_emails", {"query": "Atlas"}, context)
    assert set(found.source_ids) == {"mail-001", "mail-002"}


async def test_draft_must_use_a_record_surfaced_in_this_turn(context):
    result = await Tools().call("draft_reply", {"email_id": "mail-001", "body": "Hi"}, context)
    assert result.error
    assert context.store.actions(context.turn.conversation_id, context.owner) == []


async def test_unknown_tool_is_recoverable(context):
    result = await Tools().call("send_email", {}, context)
    assert result.error and result.content["error"] == "unknown_tool"


def test_approval_is_owned_terminal_and_single_use(context):
    store, owner, turn = context.store, context.owner, context.turn
    action = store.stage(owner, turn, {"body": "Draft"}, None)
    assert store.decide(action["id"], "someone-else", True) is None
    with pytest.raises(Conflict):
        store.decide(action["id"], owner, True)
    turn.status = "done"
    store.emit(turn, "done")
    assert store.decide(action["id"], owner, True)["status"] == "executed_demo"
    with pytest.raises(Conflict):
        store.decide(action["id"], owner, True)


def test_replacement_cannot_cross_conversations_and_rolls_back(context):
    store, owner, turn = context.store, context.owner, context.turn
    old = store.stage(owner, turn, {"body": "Original"}, None)
    other = Turn(conversation_id=store.conversation(owner), prompt="test")
    with pytest.raises(Conflict):
        store.stage(owner, other, {"body": "Replacement"}, old["id"])
    assert store.actions(turn.conversation_id, owner)[0]["status"] == "pending"


def test_one_active_turn_per_conversation(context):
    with pytest.raises(Conflict):
        context.store.create_turn(Turn(conversation_id=context.turn.conversation_id, prompt="next"))


def test_restart_marks_inflight_turn_interrupted_and_keeps_partial_text(tmp_path):
    path = str(tmp_path / "test.sqlite3")
    store = Store(path)
    owner = store.session()
    turn = Turn(conversation_id=store.conversation(owner), prompt="test", text="partial")
    store.create_turn(turn)
    store.emit(turn, "delta", text="partial")
    store.stage(owner, turn, {"body": "Draft"}, None)
    store.close()
    reopened = Store(path)
    reopened.recover()
    saved = reopened.get_turn(turn.id)
    assert saved.status == "interrupted" and saved.text == "partial"
    assert reopened.events(turn.id, 1)[0]["reason"] == "server_restart"
    assert reopened.actions(turn.conversation_id, owner)[0]["status"] == "discarded"
    reopened.close()
