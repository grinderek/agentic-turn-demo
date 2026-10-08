"""SQLite snapshots and an append-only per-turn event log, for one server process."""

import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from .models import Turn, new_id


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path: str = "data/demo.sqlite3"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, owner TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                status TEXT NOT NULL, snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                turn_id TEXT NOT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (turn_id, seq)
            );
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation_id TEXT NOT NULL,
                turn_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL
            );
        """)

    def session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES (?)", (token,))
        return token

    def has_session(self, token: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM sessions WHERE token=?", (token,)).fetchone() is not None
        )

    def conversation(self, owner: str) -> str:
        cid = new_id()
        with self.db:
            self.db.execute("INSERT INTO conversations VALUES (?,?)", (cid, owner))
        return cid

    def owns(self, cid: str, owner: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND owner=?", (cid, owner)
            ).fetchone()
            is not None
        )

    def create_turn(self, turn: Turn) -> None:
        with self.db:
            active = self.db.execute(
                "SELECT 1 FROM turns WHERE conversation_id=? AND status='running'",
                (turn.conversation_id,),
            ).fetchone()
            if active:
                raise Conflict("A turn is already running in this conversation")
            self.db.execute(
                "INSERT INTO turns VALUES (?,?,?,?)",
                (turn.id, turn.conversation_id, turn.status, turn.model_dump_json()),
            )

    def get_turn(self, tid: str) -> Turn | None:
        row = self.db.execute("SELECT snapshot FROM turns WHERE id=?", (tid,)).fetchone()
        return Turn.model_validate_json(row[0]) if row else None

    def latest_turn_id(self, cid: str) -> str | None:
        row = self.db.execute(
            "SELECT id FROM turns WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (cid,)
        ).fetchone()
        return row[0] if row else None

    def history(self, cid: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT snapshot FROM turns WHERE conversation_id=? AND status='done' "
            "ORDER BY rowid DESC LIMIT 8",
            (cid,),
        ).fetchall()
        messages = []
        for row in reversed(rows):
            turn = Turn.model_validate_json(row[0])
            messages.extend(
                [
                    {"role": "user", "content": turn.prompt},
                    {"role": "assistant", "content": turn.text},
                ]
            )
        return messages

    def emit(self, turn: Turn, kind: str, **payload: Any) -> dict[str, Any]:
        turn.seq += 1
        event = {"turn_id": turn.id, "seq": turn.seq, "type": kind, **payload}
        # Snapshot and frame commit together, before any subscriber can see the frame.
        with self.db:
            self.db.execute(
                "UPDATE turns SET status=?, snapshot=? WHERE id=?",
                (turn.status, turn.model_dump_json(), turn.id),
            )
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?)", (turn.id, turn.seq, json.dumps(event))
            )
        return event

    def events(self, tid: str, after: int) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT data FROM events WHERE turn_id=? AND seq>? ORDER BY seq", (tid, after)
            ).fetchall()
        ]

    def actions(self, cid: str, owner: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id,status,payload,turn_id FROM actions WHERE conversation_id=? AND owner=? "
            "ORDER BY rowid",
            (cid, owner),
        ).fetchall()
        return [{"id": r[0], "status": r[1], **json.loads(r[2]), "turn_id": r[3]} for r in rows]

    def stage(self, owner: str, turn: Turn, payload: dict, supersedes: str | None) -> dict:
        aid = new_id()
        with self.db:
            if supersedes:
                row = self.db.execute(
                    "SELECT status FROM actions WHERE id=? AND owner=? AND conversation_id=?",
                    (supersedes, owner, turn.conversation_id),
                ).fetchone()
                if not row or row[0] != "pending":
                    raise Conflict("Only your pending draft in this conversation can be replaced")
                self.db.execute("UPDATE actions SET status='superseded' WHERE id=?", (supersedes,))
            self.db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?)",
                (aid, owner, turn.conversation_id, turn.id, "pending", json.dumps(payload)),
            )
        return {"id": aid, "status": "pending", **payload, "turn_id": turn.id}

    def decide(self, aid: str, owner: str, approve: bool) -> dict | None:
        with self.db:
            row = self.db.execute(
                "SELECT * FROM actions WHERE id=? AND owner=?", (aid, owner)
            ).fetchone()
            if not row:
                return None
            turn = self.get_turn(row["turn_id"])
            if row["status"] != "pending" or not turn or turn.status != "done":
                raise Conflict("Only a pending draft from a completed turn can be decided")
            status = "executed_demo" if approve else "rejected"
            self.db.execute("UPDATE actions SET status=? WHERE id=?", (status, aid))
            return {"id": aid, "status": status, **json.loads(row["payload"])}

    def discard(self, tid: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE actions SET status='discarded' WHERE turn_id=? AND status='pending'", (tid,)
            )

    def recover(self) -> None:
        # A process restart has no running provider stream to resume. Keep partial content.
        rows = self.db.execute("SELECT snapshot FROM turns WHERE status='running'").fetchall()
        for row in rows:
            turn = Turn.model_validate_json(row[0])
            turn.status, turn.reason = "interrupted", "server_restart"
            self.discard(turn.id)
            self.emit(turn, "error", reason=turn.reason)

    def close(self) -> None:
        self.db.close()
