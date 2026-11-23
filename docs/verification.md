# Verification

## Live provider check - 2026-10-09

The standalone runner was exercised with real Anthropic API calls using
`claude-sonnet-4-6`, streaming responses and typed structured output. All input
records were the repository's synthetic Atlas meeting fixtures.

| Case | Verified result |
| --- | --- |
| Meeting | Inbox search, source read, calendar lookup and reply staging completed; citations referenced IDs surfaced by tools; completed calls recorded provider-reported usage |
| Draft revision | The agent re-read the source, staged a shorter reply and superseded the previous draft; exactly one pending draft remained |
| Approval | The replacement draft was approved through the synthetic action path |
| Live cancellation | Cancellation during a real text stream kept partial content, stopped the turn and skipped finalization |

The check exposed a presentation bug: text from successive model calls could join
without whitespace. The runner now emits a paragraph separator between those calls.
A regression test checks both the visible transcript and the persisted delta replay.
The live check was repeated successfully after the fix.

Live generation is nondeterministic. These results describe the exercised cases,
not a guarantee that every possible prompt will use the same tool sequence.
The default provider remains scripted, and no credential is included in the repository.

## Automated verification

The tests cover the runner, scoped tools, SQLite persistence, API ownership, cancellation,
timeouts, citation repair, bounded iterations, replay, draft lifecycle and the live SDK
adapter through simulated HTTP responses. The standard test suite requires no API key.

Run `ruff check .`, `ruff format --check .` and `pytest -q` as described in the README.
The GitHub Actions workflow runs the suite on Python 3.12 and 3.14.


## Finance walkthrough - 2026-11-23

The local FastAPI app was exercised from the browser using the scripted provider.
The first budget question prompted for a version. Selecting the original plan showed
Marketing 5,000 planned / 6,300 actual and Cloud 2,000 planned / 2,600 actual.
The SQL disclosure and turn-scoped citation displayed the calculated rows,
bound month and version, and local SQLite validation metadata. The existing meeting
tour staged a synthetic mail draft and accepted the user's approval. The
[walkthrough](walkthrough.gif) contains captures from these local runs.

The warehouse tests check schema validation, dry-run ordering, byte limits,
typed parameters, cancellation, ambiguous versions and incomplete or oversized
results. A separate local contract check exercised google-cloud-bigquery 3.47.0
with anonymous credentials and a simulated HTTP transport. It verified request
serialization, DATE and STRING parameters, dry-run flags and NUMERIC result parsing.
These finance checks ran without contacting or billing a Google Cloud project.
