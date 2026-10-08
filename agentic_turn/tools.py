"""Synthetic records and tools; identity comes from server context, never model arguments."""

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import ToolResult, Turn
from .store import Conflict, Store

EMAILS = [
    {
        "id": "mail-001",
        "tenant": "demo",
        "sender": "maya@example.test",
        "subject": "Atlas design review",
        "body": "Can we review the Atlas prototype on 2030-06-12 at 14:00 UTC? Allow 30 minutes.",
    },
    {
        "id": "mail-002",
        "tenant": "demo",
        "sender": "maya@example.test",
        "subject": "Atlas review agenda",
        "body": "For our design review: cover onboarding and the export screen. Bring the mockups.",
    },
    {
        "id": "mail-003",
        "tenant": "demo",
        "sender": "newsletter@example.test",
        "subject": "Weekly reading",
        "body": "Three articles about typography.",
    },
    {
        "id": "mail-other-001",
        "tenant": "other",
        "sender": "private@example.test",
        "subject": "Atlas design review",
        "body": "A different tenant's synthetic record.",
    },
]
CALENDAR = [
    {
        "id": "event-001",
        "tenant": "demo",
        "date": "2030-06-12",
        "title": "Team sync",
        "start": "09:00 UTC",
        "end": "09:30 UTC",
    },
    {
        "id": "event-002",
        "tenant": "demo",
        "date": "2030-06-12",
        "title": "Planning",
        "start": "16:00 UTC",
        "end": "17:00 UTC",
    },
]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchArgs(Args):
    query: str = Field(min_length=1, max_length=200)


class CalendarArgs(Args):
    day: str


class ReadArgs(Args):
    source_id: str


class DraftArgs(Args):
    email_id: str
    body: str = Field(min_length=1, max_length=4000)
    supersedes_action_id: str | None = None


SCHEMAS = {
    "search_emails": (SearchArgs, "Search your synthetic inbox by words in subject or body."),
    "list_calendar": (CalendarArgs, "List your synthetic events on an ISO date (YYYY-MM-DD)."),
    "read_source": (ReadArgs, "Read one synthetic email or event by its source ID."),
    "draft_reply": (
        DraftArgs,
        "Stage a reply to an email surfaced in this turn. Requires human "
        "approval later. To revise a pending draft, supply supersedes_action_id.",
    ),
}


@dataclass(frozen=True)
class Context:
    owner: str
    turn: Turn
    store: Store
    tenant: str = "demo"


class Tools:
    def definitions(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": desc, "input_schema": schema.model_json_schema()}
            for name, (schema, desc) in SCHEMAS.items()
        ]

    async def call(self, name: str, arguments: dict, context: Context) -> ToolResult:
        if name not in SCHEMAS:
            return ToolResult({"error": "unknown_tool"}, error=True)
        try:
            args = SCHEMAS[name][0].model_validate(arguments)
            records = [r for r in EMAILS + CALENDAR if r["tenant"] == context.tenant]
            if isinstance(args, SearchArgs):
                words = args.query.casefold().split()
                found = [
                    r
                    for r in records
                    if "body" in r
                    and all(w in (r["subject"] + " " + r["body"]).casefold() for w in words)
                ]
            elif isinstance(args, CalendarArgs):
                date.fromisoformat(args.day)
                found = [r for r in records if r.get("date") == args.day]
            elif isinstance(args, ReadArgs):
                found = [r for r in records if r["id"] == args.source_id]
            else:
                assert isinstance(args, DraftArgs)
                email = next((r for r in records if r["id"] == args.email_id and "body" in r), None)
                if not email or args.email_id not in context.turn.ledger:
                    return ToolResult({"error": "email_not_surfaced_in_this_turn"}, error=True)
                action = context.store.stage(
                    context.owner,
                    context.turn,
                    {
                        "kind": "reply",
                        "recipient": email["sender"],
                        "subject": "Re: " + email["subject"],
                        "body": args.body,
                    },
                    args.supersedes_action_id,
                )
                return ToolResult(
                    {"draft": action}, action=action, retired_action_id=args.supersedes_action_id
                )
            public = [{k: v for k, v in r.items() if k != "tenant"} for r in found]
            return ToolResult({"records": public}, source_ids=[r["id"] for r in found])
        except (ValidationError, ValueError, Conflict) as exc:
            # A bad tool argument is recoverable feedback, not a crashed turn.
            return ToolResult({"error": "invalid_arguments", "detail": str(exc)}, error=True)


def data_block(result: ToolResult) -> str:
    # An instruction boundary, not a claim of complete prompt-injection protection.
    return "<tool_data>" + json.dumps(result.content, ensure_ascii=False) + "</tool_data>"
