import asyncio
from decimal import Decimal

import pytest

from agentic_turn.engine import Engine
from agentic_turn.models import Turn
from agentic_turn.providers import ScriptedProvider
from agentic_turn.store import Store
from agentic_turn.tools import Context, Tools
from agentic_turn.warehouse import Warehouse


@pytest.fixture
def store():
    value = Store(":memory:")
    yield value
    value.close()


async def run_turn(store, owner, cid, prompt):
    turn = Turn(conversation_id=cid, prompt=prompt, scenario="finance")
    store.create_turn(turn)
    await Engine(store, ScriptedProvider(delay=0)).run(turn, owner, asyncio.Event())
    return turn


async def test_finance_clarifies_and_computes_selected_version(store):
    owner = store.session()
    cid = store.conversation(owner)
    first = await run_turn(store, owner, cid, "Which categories exceeded budget in October 2030?")
    assert first.status == "done" and first.outcome == "needs_clarification"
    assert "original" in first.text and "revised" in first.text
    assert [tool["name"] for tool in first.tools] == ["warehouse_schema"]
    second = await run_turn(store, owner, cid, "Use original")
    assert second.status == "done" and second.outcome == "answered"
    assert [tool["name"] for tool in second.tools] == ["warehouse_schema", "budget_variance"]
    assert second.tools[-1]["arguments"]["version"] == "original"
    result = await Warehouse.from_env().query("2030-10", "original")
    total = sum((Decimal(row["delta"]) for row in result["rows"]), Decimal("0"))
    assert f"{total:,.2f}" in second.text
    assert second.citations[0].source_id == result["source_id"]
    assert store.actions(cid, owner) == []


async def test_finance_can_change_budget_version_in_same_conversation(store):
    owner = store.session()
    cid = store.conversation(owner)
    await run_turn(store, owner, cid, "October 2030 original budget")
    revised = await run_turn(store, owner, cid, "Now use revised")
    assert revised.outcome == "answered"
    assert revised.tools[-1]["arguments"]["version"] == "revised"


async def test_finance_rejects_model_supplied_sql_and_identity(store):
    owner = store.session()
    turn = Turn(conversation_id=store.conversation(owner), prompt="finance")
    context = Context(owner, turn, store)
    result = await Tools().call(
        "budget_variance", {"month": "2030-10", "sql": "DELETE FROM expenses"}, context
    )
    assert result.error
    other_tenant = Context(owner, turn, store, tenant="other")
    result = await Tools().call("warehouse_schema", {"month": "2030-10"}, other_tenant)
    assert result.error and result.content["error"] == "warehouse_not_available"


async def test_finance_error_does_not_invent_totals(store):
    owner = store.session()
    turn = Turn(
        conversation_id=store.conversation(owner), prompt="October 2030", scenario="finance"
    )

    class FailedWarehouse:
        async def describe(self, month):
            raise ValueError("Warehouse unavailable")

    store.create_turn(turn)
    engine = Engine(store, ScriptedProvider(delay=0), tools=Tools(FailedWarehouse()))
    await engine.run(turn, owner, asyncio.Event())
    assert turn.status == "done" and turn.outcome == "no_data"
    assert "No financial result" in turn.text
    assert turn.tools[0]["error"]


async def test_fresh_question_does_not_reuse_previous_version(store):
    owner = store.session()
    cid = store.conversation(owner)
    await run_turn(store, owner, cid, "October 2030 original budget")
    fresh = await run_turn(store, owner, cid, "Which categories exceeded budget in October 2030?")
    assert fresh.outcome == "needs_clarification"
    assert [tool["name"] for tool in fresh.tools] == ["warehouse_schema"]


@pytest.mark.parametrize("prompt", ["November 2030 budget", "2031-01-01 original budget"])
async def test_explicit_other_month_returns_no_data(store, prompt):
    owner = store.session()
    turn = await run_turn(store, owner, store.conversation(owner), prompt)
    assert turn.outcome == "no_data"
    assert "No financial data" in turn.text
    assert len(turn.tools) == 1


async def test_unknown_version_prompts_for_available_choices(store):
    owner = store.session()
    turn = await run_turn(
        store, owner, store.conversation(owner), "Use the imaginary budget for 2030-10-01."
    )
    assert turn.outcome == "needs_clarification"
    assert "original" in turn.text and "revised" in turn.text


async def test_configured_custom_version_names_are_supported(store, monkeypatch):
    import agentic_turn.warehouse as warehouse

    monkeypatch.setattr(
        warehouse,
        "RECORDS",
        [("2030-10-01", "board-approved", "Cloud", "100", "120")],
    )
    owner = store.session()
    turn = await run_turn(
        store, owner, store.conversation(owner), "Use the board-approved budget for 2030-10-01."
    )
    assert turn.outcome == "answered"
    assert "20.00" in turn.text and "board-approved" in turn.text
    assert "executed SQL" not in turn.text


async def test_missing_month_requires_clarification_without_reusing_old_month(store):
    owner = store.session()
    cid = store.conversation(owner)
    await run_turn(store, owner, cid, "October 2030 original budget")
    turn = await run_turn(store, owner, cid, "Which categories exceeded budget?")
    assert turn.outcome == "needs_clarification"
    assert turn.tools == []
