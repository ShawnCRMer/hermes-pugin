"""PUGin backend — one read-only route, mounted at /api/plugins/pugin/.

GET /linked-work?session_id=<stored chat id>
    Kanban tasks that belong to that chat, across every non-archived board:
    provenance ``session_id`` equals the chat (stamped by the native
    ``kanban_create`` tool; linked children inherit it), or ``created_by``
    equals the chat id exactly (the stamp CLI-created cards can carry:
    ``hermes kanban create --created-by "$HERMES_SESSION_ID"``). Only those
    tasks — never another chat's work.

    Returns ``running`` (with heartbeat/runtime facts for the stuck alarm),
    ``blocked`` (for the "needs you" nudge), ``recent_done`` (finished in the
    last ``DONE_WINDOW_S`` seconds, for the done hand-off) and a ``queued`` count.

Reads each board's SQLite file read-only with the standard library, using the
same board layout the dispatcher uses: ``<root>/kanban.db`` for the default
board and ``<root>/kanban/boards/<slug>/kanban.db`` for named boards, where
``<root>`` is ``HERMES_KANBAN_HOME`` or the default Hermes root (shared across
profiles by design). No writes, no model calls, no other side effects.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

try:  # FastAPI is present wherever dashboard plugin routes are mounted.
    from fastapi import APIRouter, HTTPException, Query
except Exception:  # pragma: no cover - allows importing the helpers in plain tests
    APIRouter = None  # type: ignore[assignment]

SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
RUNNING = "running"
BLOCKED = "blocked"
DONE = "done"
QUEUED = ("triage", "todo", "ready", "scheduled", "review")
DONE_WINDOW_S = 15 * 60
MAX_ROWS = 50
OPTIONAL = ("claim_expires", "started_at", "worker_started_at", "last_heartbeat_at", "max_runtime_seconds",
            "completed_at", "result", "block_kind", "last_failure_error")


def kanban_root() -> Path:
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root())


def board_databases(root: Path) -> list[tuple[str, Path]]:
    boards: list[tuple[str, Path]] = []
    default = root / "kanban.db"
    if default.is_file():
        boards.append(("default", default))
    named = root / "kanban" / "boards"
    if named.is_dir():
        for entry in sorted(named.iterdir()):
            if entry.name.startswith("_") or not entry.is_dir():
                continue
            db = entry / "kanban.db"
            if not db.is_file():
                continue
            try:
                meta = json.loads((entry / "board.json").read_text(encoding="utf-8"))
                if meta.get("archived") is True:
                    continue
            except (OSError, ValueError):
                pass
            boards.append((entry.name, db))
    seen: set[Path] = set()
    unique = []
    for slug, db in boards:  # an env-pinned DB can alias several slugs
        resolved = db.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append((slug, db))
    return unique


def _snippet(value, limit=140) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def tasks_for_session(session_id: str, root: Path | None = None, now: float | None = None) -> dict:
    if not SESSION_ID.match(session_id or ""):
        raise ValueError("invalid session_id")
    root = root or kanban_root()
    now = time.time() if now is None else now
    running: list[dict] = []
    blocked: list[dict] = []
    recent_done: list[dict] = []
    queued = 0
    unreadable: list[str] = []
    for slug, db in board_databases(root):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.Error:
            unreadable.append(slug)
            continue
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "session_id" not in cols:
                continue  # board predates provenance; nothing can be linked
            extra = [c if c in cols else "NULL" for c in OPTIONAL]
            owner = "(session_id = ? OR created_by = ?)" if "created_by" in cols else "(session_id = ? OR ? IS NULL)"
            done = "(status = ? AND completed_at >= ?)" if "completed_at" in cols else "(status = ? AND ? IS NULL)"
            rows = conn.execute(
                f"SELECT id, title, status, assignee, {', '.join(extra)} FROM tasks "
                f"WHERE {owner} AND (status IN (?, ?, ?, ?, ?, ?, ?) OR {done}) "
                "ORDER BY created_at LIMIT ?",
                (session_id, session_id, RUNNING, BLOCKED, *QUEUED, DONE, now - DONE_WINDOW_S, MAX_ROWS * 3),
            ).fetchall()
        except sqlite3.Error:
            unreadable.append(slug)
            continue
        finally:
            conn.close()
        for task_id, title, status, assignee, *rest in rows:
            f = dict(zip(OPTIONAL, rest))
            base = {"id": task_id, "title": title or "", "assignee": assignee or "", "board": slug}
            if status == RUNNING:
                claim = _num(f["claim_expires"])
                if claim is not None and claim <= now:
                    continue  # an expired claim is not live work
                running.append({**base,
                                "started_at": _num(f["worker_started_at"]) or _num(f["started_at"]),
                                "last_heartbeat_at": _num(f["last_heartbeat_at"]),
                                "max_runtime_seconds": _num(f["max_runtime_seconds"])})
            elif status == BLOCKED:
                blocked.append({**base, "block_kind": f["block_kind"] or "",
                                "reason": _snippet(f["last_failure_error"] or f["result"])})
            elif status == DONE:
                recent_done.append({**base, "completed_at": _num(f["completed_at"]), "result": _snippet(f["result"])})
            else:
                queued += 1
    return {"session_id": session_id, "running": running[:MAX_ROWS], "blocked": blocked[:MAX_ROWS],
            "recent_done": recent_done[:MAX_ROWS], "queued": queued, "unreadable_boards": unreadable,
            "checked_at": now}


router = APIRouter() if APIRouter is not None else None

if router is not None:

    @router.get("/linked-work")
    def linked_work(session_id: str = Query(..., max_length=128)) -> dict:
        try:
            return tasks_for_session(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/health")
    def health() -> dict:
        return {"ok": True}
