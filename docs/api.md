# API walkthrough

The browser creates a session and conversation automatically. The same flow is available
through HTTP. Tokens below are placeholders. Use the token returned by your local instance.

```bash
curl -X POST http://localhost:8000/api/sessions
# {"token":"..."}

curl -X POST http://localhost:8000/api/conversations \
  -H 'X-Demo-Session: SESSION_TOKEN'
# {"id":"..."}

curl -X POST http://localhost:8000/api/conversations/CONVERSATION_ID/turns \
  -H 'X-Demo-Session: SESSION_TOKEN' -H 'Content-Type: application/json' \
  -d '{"prompt":"Find the Atlas review details and draft a reply","scenario":"meeting"}'
# HTTP 202 {"id":"TURN_ID","status":"running"}

curl -N http://localhost:8000/api/turns/TURN_ID/events \
  -H 'X-Demo-Session: SESSION_TOKEN'

curl -N http://localhost:8000/api/turns/TURN_ID/events \
  -H 'X-Demo-Session: SESSION_TOKEN' -H 'Last-Event-ID: 5'

curl -X POST http://localhost:8000/api/turns/TURN_ID/cancel \
  -H 'X-Demo-Session: SESSION_TOKEN'

curl http://localhost:8000/api/turns/TURN_ID \
  -H 'X-Demo-Session: SESSION_TOKEN'

curl -X POST http://localhost:8000/api/actions/ACTION_ID/approve \
  -H 'X-Demo-Session: SESSION_TOKEN'
```

SSE frames use `id: SEQUENCE` followed by one JSON `data:` line:

```text
id: 2
data: {"turn_id":"...","seq":2,"type":"delta","text":"I'll check"}
```

| Frame | Meaning |
| --- | --- |
| `start` | Turn execution began |
| `delta` | Append visible text |
| `model_call` | Completed/incomplete call audit and reported usage |
| `tool` | Tool name, arguments, error flag, duration and surfaced IDs |
| `staged` | New draft and optional superseded action ID |
| `answer_complete` | Text generation finished, structured finalization still follows |
| `citation_rejected` | Invalid source IDs and whether this was the repair pass |
| `done` | Terminal answer, outcome and accepted citations |
| `cancelled` | Terminal user cancellation |
| `error` | Terminal error or server interruption |

Snapshot responses include the accumulated text, status, cursor, call audit, ledger,
citations and current conversation actions. Browser reconnects use both snapshots and
event replay. A browser uses streaming `fetch` rather than `EventSource` to send the
session header. Unauthorized ownership returns 404; active-turn and action conflicts
return 409. Invalid payloads return 422.
