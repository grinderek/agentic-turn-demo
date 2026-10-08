import asyncio

import pytest

from agentic_turn.engine import Engine
from agentic_turn.models import Answer, Citation, FinalReply, ModelReply, Turn
from agentic_turn.providers import ScriptedProvider
from agentic_turn.store import Store


@pytest.fixture
def store():
    value = Store(":memory:")
    yield value
    value.close()


def prepare(store, scenario="meeting"):
    owner = store.session()
    cid = store.conversation(owner)
    return owner, Turn(conversation_id=cid, prompt="Prepare the Atlas reply", scenario=scenario)


async def execute(store, provider=None, scenario="meeting", **options):
    owner, turn = prepare(store, scenario)
    engine = Engine(store, provider or ScriptedProvider(delay=0), **options)
    store.create_turn(turn)
    await engine.run(turn, owner, asyncio.Event())
    return turn


@pytest.mark.parametrize("scenario", ["meeting", "tool_error", "bad_citation"])
async def test_real_runner_completes_scripted_scenarios(store, scenario):
    turn = await execute(store, scenario=scenario)
    assert turn.status == "done"
    assert turn.text == store.get_turn(turn.id).text
    assert "14:00-14:30 UTC" in turn.text
    assert set(c.source_id for c in turn.citations) <= set(turn.ledger)
    events = store.events(turn.id, 0)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["turn_id"] == turn.id for e in events)
    assert events[-1]["type"] == "done"
    assert all(c["usage"] is None for c in turn.calls)  # No invented token counters.
    if scenario == "tool_error":
        assert turn.tools[0]["error"] is True
        assert turn.tools[1]["error"] is False
    if scenario == "bad_citation":
        assert [c["phase"] for c in turn.calls][-2:] == ["finalize", "citation_repair"]
        assert sum(e["type"] == "citation_rejected" for e in events) == 1


async def test_iteration_budget_stops_repeating_tools(store):
    turn = await execute(store, scenario="budget", max_iterations=3)
    assert turn.status == "error"
    assert turn.reason == "iteration_budget_exhausted"
    assert len(turn.calls) == len(turn.tools) == 3
    assert not any(e["type"] == "done" for e in store.events(turn.id, 0))


class StalledProvider(ScriptedProvider):
    def __init__(self):
        self.entered = asyncio.Event()
        self.closed = False
        self.finalized = False

    async def stream(self, **kwargs):
        try:
            yield "Partial answer."
            self.entered.set()
            await asyncio.Event().wait()
        finally:
            self.closed = True

    async def finalize(self, **kwargs):
        self.finalized = True
        raise AssertionError("Cancelled turn must not finalize")


async def test_cancel_interrupts_a_stalled_read_and_closes_stream(store):
    provider = StalledProvider()
    owner, turn = prepare(store)
    engine = Engine(store, provider)
    engine.start(turn, owner)
    task = engine.running[turn.id][0]
    await asyncio.wait_for(provider.entered.wait(), 1)
    assert engine.cancel(turn.id)
    await asyncio.wait_for(task, 1)
    saved = store.get_turn(turn.id)
    assert saved.status == "cancelled"
    assert saved.text == "Partial answer."
    assert saved.calls[-1]["usage"] is None
    assert provider.closed and not provider.finalized
    assert store.history(turn.conversation_id) == []


async def test_timeout_preserves_partial_answer(store):
    provider = StalledProvider()
    turn = await execute(store, provider, timeout=0.03)
    assert turn.status == "error" and turn.reason == "turn_timeout"
    assert turn.text == "Partial answer."
    assert provider.closed


async def test_pre_cancel_does_not_call_provider(store):
    owner, turn = prepare(store)
    store.create_turn(turn)
    cancelled = asyncio.Event()
    cancelled.set()
    await Engine(store, StalledProvider()).run(turn, owner, cancelled)
    assert turn.status == "cancelled" and turn.calls == []


class BrokenProvider(ScriptedProvider):
    async def stream(self, **kwargs):
        yield "Already visible."
        raise RuntimeError("Simulated transport failure")


async def test_stream_failure_has_error_terminal_and_persisted_text(store):
    turn = await execute(store, BrokenProvider(delay=0))
    assert turn.status == "error" and turn.text == "Already visible."
    assert store.events(turn.id, 0)[-1]["type"] == "error"


class InvalidCitationsProvider(ScriptedProvider):
    async def finalize(self, **kwargs):
        return FinalReply(
            Answer(outcome="answered", citations=[Citation(source_id="missing", reason="invented")])
        )


async def test_failed_repair_filters_invalid_ids_and_is_bounded(store):
    turn = await execute(store, InvalidCitationsProvider(delay=0))
    assert turn.status == "done" and turn.citations == []
    assert [c["phase"] for c in turn.calls].count("citation_repair") == 1
    assert sum(e["type"] == "citation_rejected" for e in store.events(turn.id, 0)) == 2


class TruncatedProvider(ScriptedProvider):
    async def stream(self, **kwargs):
        yield "Partial"
        yield ModelReply(text="Partial", stop_reason="max_tokens")


async def test_truncation_is_not_reported_as_success(store):
    turn = await execute(store, TruncatedProvider(delay=0))
    assert turn.status == "error" and turn.reason == "output_truncated"


async def test_draft_revision_retires_previous_action(store):
    owner, first = prepare(store)
    engine = Engine(store, ScriptedProvider(delay=0))
    store.create_turn(first)
    await engine.run(first, owner, asyncio.Event())
    old = store.actions(first.conversation_id, owner)[0]
    second = Turn(conversation_id=first.conversation_id, prompt="Make the reply shorter")
    store.create_turn(second)
    await engine.run(second, owner, asyncio.Event())
    actions = store.actions(first.conversation_id, owner)
    assert actions[0]["status"] == "superseded" and actions[1]["status"] == "pending"
    assert len(actions[1]["body"]) < len(old["body"])


async def test_cancel_after_staging_discards_draft(store):
    class CancelAfterDraft(ScriptedProvider):
        async def stream(self, **kwargs):
            if kwargs["step"] == 3:
                cancelled.set()
            async for part in super().stream(**kwargs):
                yield part

    owner, turn = prepare(store)
    store.create_turn(turn)
    cancelled = asyncio.Event()
    await Engine(store, CancelAfterDraft(delay=0)).run(turn, owner, cancelled)
    assert turn.status == "cancelled"
    assert store.actions(turn.conversation_id, owner)[0]["status"] == "discarded"
