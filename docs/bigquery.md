# BigQuery budget analysis

The included tour can run entirely offline. For cloud verification, an operator
can point a local instance at **one dedicated synthetic view**. This demo has a
shared workspace: every visitor can start a session. Use synthetic data only.
If the Anthropic provider is enabled, tool results are included in model requests.
Turn snapshots and the audit trail are stored in the local demo database.

## Run with Google Cloud

1. Choose a BigQuery view with synthetic data that matches the [schema below](#source-and-query-contract).
   Set `BIGQUERY_TABLE` to its fully qualified name. The app issues SELECT queries only.
2. Install the optional adapter in the demo virtual environment with
   `python -m pip install '.[bigquery]'`. Configure Application Default Credentials
   for the operator identity. Grant permission to create query jobs in the execution
   project and read the selected view and its metadata. Keep credentials outside the repo.
3. Export the environment variables below in the shell that starts Uvicorn, using your
   own identifiers and the dataset's location. Opening the demo on loopback keeps the
   cloud workspace local. Google may charge for the executed queries.

```bash
export BIGQUERY_ENABLED=true
export BIGQUERY_PROJECT=your-execution-project
export BIGQUERY_TABLE=your-data-project.demo_finance.monthly_budget
export BIGQUERY_LOCATION=US
export BIGQUERY_MAX_BYTES_BILLED=33554432
uvicorn agentic_turn.api:app --host 127.0.0.1 --port 8000 --workers 1
```

Open <http://localhost:8000/?scenario=finance> and run the two-turn example.
Use the UI scenario selector if resuming a saved meeting. Live BigQuery works
with the scripted model too. The default Docker image and Compose file run
the credential-free local warehouse; the commands above use the Python environment.

## Source and query contract

The view supplies `month DATE` (the first day), `version STRING`, `category STRING`,
`planned NUMERIC` and `actual NUMERIC`. Totals are grouped by category for one
version and month. Positive actual-minus-plan differences are sorted from largest
to smallest. The configured view supplies a single currency or unit of account.
Choose a different month by including YYYY-MM in the prompt.

The operator selects the execution project and allowed source at startup. The
agent can inspect the schema, request the available versions and select a month
and version. The server constructs the query from a fixed template and binds
named parameters. With Google enabled it validates schema metadata, dry-runs
each SELECT, checks the estimated bytes and applies the same per-query cap
to execution. The default fixture runs the variance SQL and EXPLAIN QUERY PLAN
in SQLite. Only Google supplies a BigQuery byte estimate.

Email and calendar records remain local fixtures in both modes. Model calls,
warehouse calls and draft approval can be inspected separately on the timeline.
