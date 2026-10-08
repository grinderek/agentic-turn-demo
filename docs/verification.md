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

27 tests cover the runner, scoped tools, SQLite persistence, API ownership, cancellation,
timeouts, citation repair, bounded iterations, replay, draft lifecycle and the live SDK
adapter through simulated HTTP responses. The standard test suite requires no API key.

Run `ruff check .`, `ruff format --check .` and `pytest -q` as described in the README.
The GitHub Actions workflow runs the suite on Python 3.12 and 3.14.
