import asyncio
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest

from agentic_turn.warehouse import SCHEMA, Warehouse, WarehouseError


async def test_budget_requires_version_and_computes_aggregates():
    warehouse = Warehouse()
    ambiguous = await warehouse.query("2030-10")
    assert ambiguous["status"] == "needs_clarification"
    assert ambiguous["versions"] == ["original", "revised"]
    result = await warehouse.query("2030-10", "original")
    assert result["rows"] == [
        {"category": "Marketing", "planned": "5000", "actual": "6300", "delta": "1300"},
        {"category": "Cloud", "planned": "2000", "actual": "2600", "delta": "600"},
    ]
    assert "@version" in result["sql"]
    assert result["dry_run"]["performed"] is False
    assert (await warehouse.query("2030-10", "revised"))["rows"][0]["delta"] == "300"


async def test_bad_inputs_and_missing_month():
    warehouse = Warehouse()
    for value in ["2030-13", "2030-10-02", "2030-10; DELETE", "x"]:
        with pytest.raises(WarehouseError):
            await warehouse.query(value)
    with pytest.raises(WarehouseError):
        await warehouse.query("2030-10", "original'; DROP")
    assert (await warehouse.query("2030-11"))["status"] == "no_data"
    with pytest.raises(WarehouseError):
        Warehouse(table="other.table; DELETE")


class Config:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Job:
    total_bytes_processed = 42

    def __init__(self, rows):
        self.rows = rows
        self.cancelled = False

    def result(self, **kwargs):
        return self.rows

    def cancel(self, **kwargs):
        self.cancelled = True


class Client:
    def __init__(self):
        self.calls = []

    def get_table(self, table, **kwargs):
        return SimpleNamespace(
            schema=[
                SimpleNamespace(name=f["name"], field_type=f["type"], mode="NULLABLE")
                for f in SCHEMA
            ]
        )

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        if "DISTINCT" in sql:
            return Job([{"version": "original"}])
        return Job(
            [
                {
                    "category": "Cloud",
                    "planned": Decimal("2"),
                    "actual": Decimal("3"),
                    "delta": Decimal("1"),
                }
            ]
        )


SDK = SimpleNamespace(QueryJobConfig=Config, ScalarQueryParameter=lambda *args: args)


async def test_live_dry_run_precedes_bounded_parameterized_query():
    client = Client()
    warehouse = Warehouse(client, SDK, max_bytes=100)
    result = await warehouse.query("2030-10", "original")
    assert result["backend"] == "bigquery"
    assert result["rows"][0]["delta"] == "1"
    assert result["dry_run"]["bytes_processed"] == 42
    assert len(client.calls) == 4
    for _, kwargs in client.calls:
        assert kwargs["job_config"].maximum_bytes_billed == 100
        assert kwargs["job_config"].use_legacy_sql is False
        assert kwargs["timeout"] == 20
    assert client.calls[0][1]["job_config"].dry_run
    assert client.calls[2][1]["job_config"].dry_run
    assert client.calls[3][1]["job_config"].query_parameters == [
        ("month", "DATE", "2030-10-01"),
        ("version", "STRING", "original"),
    ]


async def test_over_budget_rejected_before_execution():
    client = Client()
    warehouse = Warehouse(client, SDK, max_bytes=10)
    with pytest.raises(WarehouseError, match="byte limit"):
        await warehouse.query("2030-10", "original")
    assert len(client.calls) == 1


def test_live_requires_explicit_enable(monkeypatch):
    monkeypatch.delenv("BIGQUERY_ENABLED", raising=False)
    monkeypatch.setenv("BIGQUERY_TABLE", "unsafe;")
    assert Warehouse.from_env().backend == "synthetic"


async def test_empty_version_rejected():
    with pytest.raises(WarehouseError):
        await Warehouse().query("2030-10", "")


async def test_schema_mismatch_prevents_query():
    client = Client()
    client.get_table = lambda *a, **k: SimpleNamespace(schema=[])
    with pytest.raises(WarehouseError, match="schema"):
        await Warehouse(client, SDK).query("2030-10")
    assert client.calls == []


async def test_cancel_during_dry_run_prevents_execution():
    entered, release = threading.Event(), threading.Event()
    client = Client()

    def query(sql, **kwargs):
        client.calls.append((sql, kwargs))
        entered.set()
        release.wait(timeout=2)
        return Job([])

    client.query = query
    task = asyncio.create_task(Warehouse(client, SDK).describe())
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.03)
    assert len(client.calls) == 1


async def test_cancel_active_execution_requests_job_cancellation():
    entered, release = threading.Event(), threading.Event()

    class BlockingJob(Job):
        def result(self, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return []

        def cancel(self, **kwargs):
            self.cancelled = True
            release.set()

    job = BlockingJob([])
    client = Client()
    client.query = lambda *args, **kwargs: (
        Job([]) if getattr(kwargs["job_config"], "dry_run", False) else job
    )
    task = asyncio.create_task(Warehouse(client, SDK).describe())
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert job.cancelled


async def test_incomplete_and_excessive_remote_rows_are_rejected():
    for rows in [
        [{"category": "Unknown", "planned": None, "actual": Decimal("3"), "delta": None}],
        [
            {
                "category": "Cloud",
                "planned": Decimal("2"),
                "actual": Decimal("3"),
                "delta": Decimal("1"),
                "invalid_rows": 1,
            }
        ],
        [
            {
                "category": str(i),
                "planned": Decimal("2"),
                "actual": Decimal("3"),
                "delta": Decimal("1"),
                "invalid_rows": 0,
            }
            for i in range(101)
        ],
    ]:
        client = Client()

        def query(sql, rows=rows, **kwargs):
            if "DISTINCT" in sql:
                return Job([{"version": "original"}])
            return Job(rows)

        client.query = query
        with pytest.raises(WarehouseError):
            await Warehouse(client, SDK).query("2030-10", "original")


async def test_invalid_remote_versions_rejected():
    for versions in [[None], [""], [" "], [str(i) for i in range(101)]]:
        client = Client()
        client.query = lambda *a, versions=versions, **k: Job([{"version": v} for v in versions])
        with pytest.raises(WarehouseError):
            await Warehouse(client, SDK).describe()
