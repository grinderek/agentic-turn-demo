"""Read-only budget analytics with synthetic data and an optional BigQuery adapter."""

import asyncio
import os
import re
import sqlite3
import threading
from datetime import date
from decimal import Decimal
from typing import Any

SCHEMA = [
    {"name": "month", "type": "DATE"},
    {"name": "version", "type": "STRING"},
    {"name": "category", "type": "STRING"},
    {"name": "planned", "type": "NUMERIC"},
    {"name": "actual", "type": "NUMERIC"},
]
# Independently authored synthetic finance records, including multiple rows per category.
RECORDS = [
    ("2030-10-01", "original", "Marketing", "4000", "5200"),
    ("2030-10-01", "original", "Marketing", "1000", "1100"),
    ("2030-10-01", "original", "Cloud", "2000", "2600"),
    ("2030-10-01", "original", "Travel", "1500", "1100"),
    ("2030-10-01", "revised", "Marketing", "6000", "6300"),
    ("2030-10-01", "revised", "Cloud", "2500", "2600"),
    ("2030-10-01", "revised", "Travel", "1500", "1100"),
]
TABLE_PATTERN = re.compile(
    r"[a-z][a-z0-9-]{4,61}[a-z0-9]\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*"
)


class WarehouseError(ValueError):
    """Safe diagnostic that never includes credentials or provider exception text."""


def month_start(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}(?:-01)?", value):
        raise WarehouseError("month must use YYYY-MM or YYYY-MM-01")
    try:
        return date.fromisoformat(value if len(value) == 10 else value + "-01").isoformat()
    except ValueError as exc:
        raise WarehouseError("invalid calendar month") from exc


class Warehouse:
    """One server-selected source. Model arguments cannot select projects or tables."""

    def __init__(
        self,
        client=None,
        sdk=None,
        *,
        project="demo-project",
        table="demo-project.finance.budget",
        location="US",
        max_bytes=33_554_432,
        timeout=20,
    ):
        if not TABLE_PATTERN.fullmatch(table):
            raise WarehouseError("invalid configured BigQuery table identifier")
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
            raise WarehouseError("invalid execution project")
        if max_bytes <= 0 or timeout <= 0:
            raise WarehouseError("query limits must be positive")
        self.client, self.sdk = client, sdk
        self.project, self.table, self.location = project, table, location
        self.max_bytes, self.timeout = max_bytes, timeout

    @classmethod
    def from_env(cls):
        if os.getenv("BIGQUERY_ENABLED", "").lower() != "true":
            return cls()
        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise WarehouseError("install google-cloud-bigquery to enable BigQuery") from exc
        project = os.getenv("BIGQUERY_PROJECT", "")
        table = os.getenv("BIGQUERY_TABLE", "")
        try:
            max_bytes = int(os.getenv("BIGQUERY_MAX_BYTES_BILLED", "33554432"))
        except ValueError as exc:
            raise WarehouseError("invalid BigQuery byte limit") from exc
        # Validate configuration before constructing an ADC-backed client.
        instance = cls(
            project=project,
            table=table,
            max_bytes=max_bytes,
            location=os.getenv("BIGQUERY_LOCATION", "US"),
        )
        try:
            instance.client = bigquery.Client(project=project)
        except Exception as exc:
            raise WarehouseError("BigQuery ADC initialization failed") from exc
        instance.sdk = bigquery
        return instance

    @property
    def backend(self):
        return "bigquery" if self.client is not None else "synthetic"

    @property
    def source_id(self):
        return "warehouse-budget"

    def _base(self, month):
        return {
            "backend": self.backend,
            "source": self.table,
            "source_id": self.source_id,
            "execution_project": self.project,
            "period": month,
        }

    async def describe(self, month="2030-10"):
        month = month_start(month)
        if self.client is None:
            versions = sorted({r[1] for r in RECORDS if r[0] == month})
        else:
            await self._validate_schema()
            sql = (
                f"SELECT DISTINCT version FROM `{self.table}` "
                "WHERE month = @month ORDER BY version LIMIT 101"
            )
            result = await self._execute(sql, {"month": ("DATE", month)})
            versions = [r["version"] for r in result["rows"]]
            if len(versions) > 100 or any(
                not isinstance(v, str) or not v.strip() for v in versions
            ):
                raise WarehouseError("invalid or excessive budget versions")
        return {**self._base(month), "schema": SCHEMA, "versions": versions}

    async def query(self, month: str, version: str | None = None):
        month = month_start(month)
        description = await self.describe(month)
        versions = description["versions"]
        if version is None and len(versions) > 1:
            return {**description, "status": "needs_clarification"}
        if not versions:
            return {**description, "status": "no_data", "rows": []}
        version = versions[0] if version is None else version
        if not isinstance(version, str) or version not in versions:
            raise WarehouseError("select one of the available budget versions")
        sql = f"""SELECT category,
       SUM(planned) AS planned,
       SUM(actual) AS actual,
       SUM(actual) - SUM(planned) AS delta,
       SUM(CASE WHEN planned IS NULL OR actual IS NULL OR category IS NULL
                THEN 1 ELSE 0 END) AS invalid_rows
FROM `{self.table}`
WHERE month = @month AND version = @version
GROUP BY category
HAVING invalid_rows > 0 OR SUM(actual) > SUM(planned)
ORDER BY delta DESC, category
LIMIT 101"""
        parameters = {"month": ("DATE", month), "version": ("STRING", version)}
        if self.client is None:
            # All fixture amounts are whole currency units, stored as exact integers.
            # This executes the same SELECT as BigQuery, including named parameters.
            with sqlite3.connect(":memory:") as connection:
                connection.execute(
                    f"CREATE TABLE `{self.table}` "
                    "(month TEXT, version TEXT, category TEXT, planned INTEGER, actual INTEGER)"
                )
                connection.executemany(
                    f"INSERT INTO `{self.table}` VALUES (?, ?, ?, ?, ?)",
                    [(m, v, c, int(p), int(a)) for m, v, c, p, a in RECORDS],
                )
                values = {k: value for k, (_, value) in parameters.items()}
                connection.execute("EXPLAIN QUERY PLAN " + sql, values).fetchall()
                cursor = connection.execute(sql, values)
                names = [column[0] for column in cursor.description]
                rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
            rows = [
                {k: str(v) if k != "category" else v for k, v in r.items() if k != "invalid_rows"}
                for r in rows
            ]
            dry_run = {
                "performed": False,
                "validation": "SQLite EXPLAIN QUERY PLAN",
                "reason": "synthetic local SQL; no Google request or byte estimate",
            }
        else:
            result = await self._execute(sql, parameters)
            if any(r.get("invalid_rows", 0) for r in result["rows"]):
                raise WarehouseError("budget source contains incomplete data")
            rows = [
                {
                    k: str(v) if isinstance(v, Decimal) else v
                    for k, v in r.items()
                    if k != "invalid_rows"
                }
                for r in result["rows"]
            ]
            dry_run = result["dry_run"]
        if len(rows) > 100:
            raise WarehouseError("more than 100 budget categories; narrow the source view")
        return {
            **self._base(month),
            "status": "answered",
            "version": version,
            "rows": rows,
            "sql": sql,
            "parameters": {k: {"type": t, "value": v} for k, (t, v) in parameters.items()},
            "dry_run": dry_run,
        }

    async def _validate_schema(self):
        try:
            table = await asyncio.to_thread(
                self.client.get_table, self.table, timeout=self.timeout, retry=None
            )
        except Exception as exc:
            raise WarehouseError("cannot read configured BigQuery table schema") from exc
        fields = {f.name: (f.field_type, f.mode) for f in table.schema}
        if any(
            fields.get(f["name"]) not in [(f["type"], "NULLABLE"), (f["type"], "REQUIRED")]
            for f in SCHEMA
        ):
            raise WarehouseError("configured table must match the budget schema")

    async def _execute(self, sql: str, parameters: dict[str, tuple[str, Any]]):
        cancelled = threading.Event()
        active = {}
        try:
            return await asyncio.to_thread(self._execute_sync, sql, parameters, cancelled, active)
        except asyncio.CancelledError:
            cancelled.set()
            if job := active.get("job"):
                await asyncio.to_thread(self._cancel_job, job)
            raise
        except WarehouseError:
            raise
        except Exception as exc:
            raise WarehouseError("BigQuery request failed or timed out") from exc

    def _cancel_job(self, job):
        try:
            job.cancel(timeout=self.timeout, retry=None)
        except Exception:
            pass

    def _execute_sync(self, sql, parameters, cancelled, active):
        params = [self.sdk.ScalarQueryParameter(k, t, v) for k, (t, v) in parameters.items()]
        options = dict(
            use_legacy_sql=False,
            query_parameters=params,
            maximum_bytes_billed=self.max_bytes,
            use_query_cache=False,
        )
        check = self.client.query(
            sql,
            job_config=self.sdk.QueryJobConfig(**options, dry_run=True),
            location=self.location,
            timeout=self.timeout,
            retry=None,
            job_retry=None,
        )
        size = check.total_bytes_processed
        if size is None or size > self.max_bytes:
            raise WarehouseError("query exceeds the configured byte limit")
        if cancelled.is_set():
            raise WarehouseError("BigQuery request cancelled")
        job = self.client.query(
            sql,
            job_config=self.sdk.QueryJobConfig(**options),
            location=self.location,
            timeout=self.timeout,
            retry=None,
            job_retry=None,
        )
        active["job"] = job
        if cancelled.is_set():
            self._cancel_job(job)
            raise WarehouseError("BigQuery request cancelled")
        try:
            rows = [
                dict(r)
                for r in job.result(
                    timeout=self.timeout, max_results=101, retry=None, job_retry=None
                )
            ]
            if any(any(v is None for v in r.values()) for r in rows):
                raise WarehouseError("budget query returned incomplete data")
        except Exception:
            self._cancel_job(job)
            raise
        return {
            "rows": rows,
            "dry_run": {
                "performed": True,
                "bytes_processed": size,
                "maximum_bytes_billed": self.max_bytes,
            },
        }
