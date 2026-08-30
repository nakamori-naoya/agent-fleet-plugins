#!/usr/bin/env python3
"""SQLite-backed Agent Fleet Core control plane.

This module intentionally owns runtime state, not fleet.yml semantics.  It only
accepts normalized JSON emitted by ``../spec/scripts/validate_fleet.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


COMMAND_TYPES = frozenset(
    {"fleet.provision", "task.assign", "message.send", "task.report", "fleet.reconcile"}
)
EVENT_SOURCE_TYPES = frozenset({"fleet", "member", "task", "runtime", "view", "provider", "system"})
TASK_TRANSITIONS = {
    "pending": frozenset({"assigned"}),
    "assigned": frozenset({"running"}),
    "running": frozenset({"blocked", "completed", "failed"}),
    "blocked": frozenset({"running"}),
    "completed": frozenset(),
    "failed": frozenset(),
}


class FleetError(RuntimeError):
    """A user-actionable control-plane error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load_fleet_config(path: Path, validator_command: Path | None = None) -> Mapping[str, Any]:
    """Validate fleet.yml through the stable spec CLI and return normalized JSON."""

    validator = validator_command or (
        Path(__file__).resolve().parent.parent / "spec" / "scripts" / "validate_fleet.py"
    )
    if not validator.is_file():
        raise FleetError(f"fleet spec validator not found: {validator}")
    completed = subprocess.run(
        [sys.executable, str(validator), str(path), "--output-json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or "fleet spec validation failed"
        raise FleetError(diagnostic)
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FleetError(f"fleet spec validator returned invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise FleetError("fleet spec validator JSON root must be an object")
    return document


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS fleets (
    fleet_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    fleet_id TEXT NOT NULL REFERENCES fleets(fleet_id),
    agent_ref TEXT NOT NULL,
    role_ref TEXT NOT NULL,
    is_manager INTEGER NOT NULL CHECK (is_manager IN (0,1)),
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (fleet_id, agent_ref)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    fleet_id TEXT NOT NULL REFERENCES fleets(fleet_id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('pending','assigned','running','blocked','completed','failed')),
    assignee_ref TEXT,
    planned_assignee_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (fleet_id, assignee_ref) REFERENCES members(fleet_id, agent_ref),
    FOREIGN KEY (fleet_id, planned_assignee_ref) REFERENCES members(fleet_id, agent_ref)
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fleet_id TEXT NOT NULL REFERENCES fleets(fleet_id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    command_id TEXT PRIMARY KEY,
    fleet_id TEXT NOT NULL REFERENCES fleets(fleet_id),
    sender_ref TEXT NOT NULL,
    target_agent_ref TEXT NOT NULL,
    command_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','acknowledged')),
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    FOREIGN KEY (fleet_id, sender_ref) REFERENCES members(fleet_id, agent_ref),
    FOREIGN KEY (fleet_id, target_agent_ref) REFERENCES members(fleet_id, agent_ref)
);
CREATE INDEX IF NOT EXISTS idx_events_fleet_event ON events(fleet_id, event_id);
CREATE INDEX IF NOT EXISTS idx_outbox_target_status ON outbox(fleet_id, target_agent_ref, status);
"""


class FleetStore:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, config: Mapping[str, Any]) -> dict[str, Any]:
        metadata = config.get("metadata", {})
        spec = config.get("spec", {})
        if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
            raise FleetError("normalized fleet metadata and spec must be mappings")
        fleet_id = str(metadata.get("id") or metadata.get("name") or "").strip()
        if not fleet_id:
            raise FleetError("normalized metadata.name is required")
        title = str(metadata.get("title") or metadata.get("name") or fleet_id)
        members = spec.get("members", [])
        tasks = spec.get("tasks", [])
        collaboration = spec.get("collaboration", {})
        if not isinstance(collaboration, Mapping):
            raise FleetError("normalized spec.collaboration must be a mapping")
        manager_ref = str(collaboration.get("manager") or "").strip()
        if not manager_ref:
            raise FleetError("normalized spec.collaboration.manager is required")
        if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
            raise FleetError("members must be a sequence")
        if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
            raise FleetError("tasks must be a sequence")

        created_at = utc_now()
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute(
                "INSERT INTO fleets(fleet_id,title,created_at) VALUES(?,?,?)",
                (fleet_id, title, created_at),
            )
            for item in members:
                if not isinstance(item, Mapping):
                    raise FleetError("each member must be a mapping")
                agent_ref = str(item.get("agent_ref") or item.get("ref") or "").strip()
                role_ref = str(item.get("role_ref") or "").strip()
                if not agent_ref or not role_ref:
                    raise FleetError("member agent_ref and role_ref are required")
                metadata = dict(item.get("metadata") or {})
                db.execute(
                    "INSERT INTO members(fleet_id,agent_ref,role_ref,is_manager,metadata_json) VALUES(?,?,?,?,?)",
                    (
                        fleet_id,
                        agent_ref,
                        role_ref,
                        int(agent_ref == manager_ref),
                        json.dumps(metadata, sort_keys=True),
                    ),
                )
            if not any(
                isinstance(item, Mapping)
                and str(item.get("agent_ref") or item.get("ref") or "").strip() == manager_ref
                for item in members
            ):
                raise FleetError(f"manager agent_ref is not a member: {manager_ref}")
            for item in tasks:
                if not isinstance(item, Mapping):
                    raise FleetError("each task must be a mapping")
                task_id = str(item.get("task_id") or item.get("id") or "").strip()
                if not task_id:
                    raise FleetError("task.task_id is required")
                planned_assignee = str(item.get("assignee") or "").strip() or None
                db.execute(
                    "INSERT INTO tasks(task_id,fleet_id,title,description,status,planned_assignee_ref,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        fleet_id,
                        str(item.get("title") or task_id),
                        str(item.get("description") or item.get("instructions") or ""),
                        "pending",
                        planned_assignee,
                        created_at,
                        created_at,
                    ),
                )
            self._append_event(
                db, fleet_id, "fleet", fleet_id, "fleet.provisioned", {"source": "fleet.yml"}
            )
        return {"fleet_id": fleet_id, "members": len(members), "tasks": len(tasks)}

    @staticmethod
    def _append_event(
        db: sqlite3.Connection,
        fleet_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> int:
        if entity_type not in EVENT_SOURCE_TYPES:
            raise FleetError(f"unsupported event source type: {entity_type}")
        cursor = db.execute(
            "INSERT INTO events(fleet_id,entity_type,entity_id,event_type,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (fleet_id, entity_type, entity_id, event_type, json.dumps(payload, sort_keys=True), utc_now()),
        )
        return int(cursor.lastrowid)

    def append_event(
        self,
        fleet_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> int:
        with self.connect() as db:
            return self._append_event(db, fleet_id, entity_type, entity_id, event_type, payload)

    def assign(self, fleet_id: str, task_id: str, agent_ref: str) -> dict[str, Any]:
        with self.connect() as db:
            member = db.execute(
                "SELECT 1 FROM members WHERE fleet_id=? AND agent_ref=?",
                (fleet_id, agent_ref),
            ).fetchone()
            if member is None:
                raise FleetError(f"unknown agent_ref: {agent_ref}")
            task = db.execute(
                "SELECT status,planned_assignee_ref FROM tasks WHERE fleet_id=? AND task_id=?",
                (fleet_id, task_id),
            ).fetchone()
            if task is None:
                raise FleetError(f"unknown task: {task_id}")
            if task["planned_assignee_ref"] and task["planned_assignee_ref"] != agent_ref:
                raise FleetError(
                    f"task {task_id!r} is declared for agent_ref {task['planned_assignee_ref']!r}"
                )
            self._require_transition(str(task["status"]), "assigned")
            db.execute(
                "UPDATE tasks SET status='assigned',assignee_ref=?,updated_at=? WHERE task_id=?",
                (agent_ref, utc_now(), task_id),
            )
            self._append_event(
                db,
                fleet_id,
                "task",
                task_id,
                "task.assigned",
                {"agent_ref": agent_ref},
            )
        return {"task_id": task_id, "status": "assigned", "assignee_ref": agent_ref}

    @staticmethod
    def _require_transition(current: str, target: str) -> None:
        if target not in TASK_TRANSITIONS.get(current, frozenset()):
            raise FleetError(f"invalid task transition: {current} -> {target}")

    def transition_task(
        self,
        fleet_id: str,
        task_id: str,
        target: str,
        agent_ref: str,
        report: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if target not in TASK_TRANSITIONS:
            raise FleetError(f"unknown task status: {target}")
        if target in {"completed", "failed", "blocked"} and report is None:
            raise FleetError(f"task.report payload is required for {target}")
        with self.connect() as db:
            task = db.execute(
                "SELECT status,assignee_ref FROM tasks WHERE fleet_id=? AND task_id=?",
                (fleet_id, task_id),
            ).fetchone()
            if task is None:
                raise FleetError(f"unknown task: {task_id}")
            if task["assignee_ref"] != agent_ref:
                raise FleetError("only the assigned agent may report task state")
            current = str(task["status"])
            self._require_transition(current, target)
            db.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE task_id=?",
                (target, utc_now(), task_id),
            )
            payload = {"from": current, "to": target, "report": report or {}}
            self._append_event(db, fleet_id, "task", task_id, "task.reported", payload)
        return {"task_id": task_id, "status": target, "assignee_ref": task["assignee_ref"]}

    def enqueue_command(
        self,
        fleet_id: str,
        sender_ref: str,
        target_agent_ref: str,
        command_type: str,
        payload: Mapping[str, Any],
        command_id: str | None = None,
    ) -> dict[str, Any]:
        if command_type not in COMMAND_TYPES:
            raise FleetError(f"unsupported command type: {command_type}")
        command_id = command_id or str(uuid.uuid4())
        with self.connect() as db:
            sender = db.execute(
                "SELECT is_manager FROM members WHERE fleet_id=? AND agent_ref=?",
                (fleet_id, sender_ref),
            ).fetchone()
            if sender is None or not sender["is_manager"]:
                raise FleetError("only a fleet manager may enqueue commands")
            target = db.execute(
                "SELECT 1 FROM members WHERE fleet_id=? AND agent_ref=?",
                (fleet_id, target_agent_ref),
            ).fetchone()
            if target is None:
                raise FleetError(f"unknown target agent_ref: {target_agent_ref}")
            db.execute(
                "INSERT INTO outbox(command_id,fleet_id,sender_ref,target_agent_ref,command_type,"
                "payload_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    command_id,
                    fleet_id,
                    sender_ref,
                    target_agent_ref,
                    command_type,
                    json.dumps(payload, sort_keys=True),
                    "pending",
                    utc_now(),
                ),
            )
            self._append_event(
                db,
                fleet_id,
                "system",
                command_id,
                "command.enqueued",
                {"command_type": command_type, "target_agent_ref": target_agent_ref},
            )
        return {"command_id": command_id, "status": "pending"}

    def acknowledge(self, fleet_id: str, command_id: str, agent_ref: str) -> dict[str, Any]:
        with self.connect() as db:
            command = db.execute(
                "SELECT status,target_agent_ref FROM outbox WHERE fleet_id=? AND command_id=?",
                (fleet_id, command_id),
            ).fetchone()
            if command is None:
                raise FleetError(f"unknown command: {command_id}")
            if command["target_agent_ref"] != agent_ref:
                raise FleetError("only the target agent may acknowledge a command")
            if command["status"] != "pending":
                raise FleetError(f"command is already {command['status']}")
            acknowledged_at = utc_now()
            db.execute(
                "UPDATE outbox SET status='acknowledged',acknowledged_at=? WHERE command_id=?",
                (acknowledged_at, command_id),
            )
            self._append_event(
                db, fleet_id, "system", command_id, "command.acknowledged", {"agent_ref": agent_ref}
            )
        return {"command_id": command_id, "status": "acknowledged"}

    def status(self, fleet_id: str) -> dict[str, Any]:
        with self.connect() as db:
            fleet = db.execute("SELECT * FROM fleets WHERE fleet_id=?", (fleet_id,)).fetchone()
            if fleet is None:
                raise FleetError(f"unknown fleet: {fleet_id}")
            members = [dict(row) for row in db.execute(
                "SELECT agent_ref,role_ref,is_manager,status FROM members WHERE fleet_id=? ORDER BY agent_ref",
                (fleet_id,),
            )]
            tasks = [dict(row) for row in db.execute(
                "SELECT task_id,title,status,assignee_ref,updated_at FROM tasks WHERE fleet_id=? ORDER BY task_id",
                (fleet_id,),
            )]
            pending = []
            for row in db.execute(
                "SELECT command_id,fleet_id,sender_ref,target_agent_ref,command_type,payload_json,created_at "
                "FROM outbox WHERE fleet_id=? AND status='pending' ORDER BY created_at",
                (fleet_id,),
            ):
                pending.append(
                    {
                        "apiVersion": "fleet.harness/v1",
                        "kind": "Command",
                        "metadata": {
                            "id": row["command_id"],
                            "fleet_id": row["fleet_id"],
                            "timestamp": row["created_at"],
                        },
                        "spec": {
                            "source": {"type": "member", "ref": row["sender_ref"]},
                            "target": {"type": "member", "ref": row["target_agent_ref"]},
                            "type": row["command_type"],
                            "payload": json.loads(row["payload_json"]),
                        },
                    }
                )
            events = []
            for row in db.execute(
                "SELECT event_id,fleet_id,entity_type,entity_id,event_type,payload_json,created_at "
                "FROM events WHERE fleet_id=? ORDER BY event_id",
                (fleet_id,),
            ):
                events.append(
                    {
                        "apiVersion": "fleet.harness/v1",
                        "kind": "Event",
                        "metadata": {
                            "id": str(row["event_id"]),
                            "fleet_id": row["fleet_id"],
                            "timestamp": row["created_at"],
                        },
                        "spec": {
                            "source": {"type": row["entity_type"], "ref": row["entity_id"]},
                            "type": row["event_type"],
                            "payload": json.loads(row["payload_json"]),
                        },
                    }
                )
        return {
            "fleet": dict(fleet),
            "members": members,
            "tasks": tasks,
            "outbox": pending,
            "events": events,
        }


def _json_object(value: str) -> Mapping[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-control")
    parser.add_argument("--db", type=Path, required=True, help="SQLite state path")
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", aliases=["fleet.provision"])
    init.add_argument("--config", type=Path, required=True)
    status = sub.add_parser("status", aliases=["fleet.reconcile"])
    status.add_argument("--fleet", required=True)
    assign = sub.add_parser("assign", aliases=["task.assign"])
    assign.add_argument("--fleet", required=True)
    assign.add_argument("--task", required=True)
    assign.add_argument("--agent-ref", required=True)
    transition = sub.add_parser("task-report", aliases=["task.report"])
    transition.add_argument("--fleet", required=True)
    transition.add_argument("--task", required=True)
    transition.add_argument("--status", required=True, choices=sorted(TASK_TRANSITIONS))
    transition.add_argument("--agent-ref", required=True)
    transition.add_argument("--report", type=_json_object)
    event = sub.add_parser("event")
    event.add_argument("--fleet", required=True)
    event.add_argument("--entity-type", required=True, choices=sorted(EVENT_SOURCE_TYPES))
    event.add_argument("--entity-id", required=True)
    event.add_argument("--type", required=True)
    event.add_argument("--payload", type=_json_object, default={})
    outbox = sub.add_parser("outbox")
    outbox.add_argument("--fleet", required=True)
    outbox.add_argument("--sender-ref", required=True)
    outbox.add_argument("--target-agent-ref", required=True)
    outbox.add_argument("--type", required=True, choices=sorted(COMMAND_TYPES))
    outbox.add_argument("--payload", type=_json_object, default={})
    outbox.add_argument("--command-id")
    message = sub.add_parser("message.send")
    message.add_argument("--fleet", required=True)
    message.add_argument("--sender-ref", required=True)
    message.add_argument("--target-agent-ref", required=True)
    message.add_argument("--payload", type=_json_object, required=True)
    message.add_argument("--command-id")
    ack = sub.add_parser("ack")
    ack.add_argument("--fleet", required=True)
    ack.add_argument("--command-id", required=True)
    ack.add_argument("--agent-ref", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = FleetStore(args.db)
    try:
        if args.action in {"init", "fleet.provision"}:
            result = store.initialize(load_fleet_config(args.config))
        elif args.action in {"status", "fleet.reconcile"}:
            result = store.status(args.fleet)
        elif args.action in {"assign", "task.assign"}:
            result = store.assign(args.fleet, args.task, args.agent_ref)
        elif args.action in {"task-report", "task.report"}:
            result = store.transition_task(
                args.fleet, args.task, args.status, args.agent_ref, args.report
            )
        elif args.action == "event":
            result = {"event_id": store.append_event(
                args.fleet, args.entity_type, args.entity_id, args.type, args.payload
            )}
        elif args.action in {"outbox", "message.send"}:
            result = store.enqueue_command(
                args.fleet,
                args.sender_ref,
                args.target_agent_ref,
                "message.send" if args.action == "message.send" else args.type,
                args.payload,
                args.command_id,
            )
        else:
            result = store.acknowledge(args.fleet, args.command_id, args.agent_ref)
    except (FleetError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
