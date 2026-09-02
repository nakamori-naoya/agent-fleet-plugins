#!/usr/bin/env python3
"""SQLite-backed Agent Fleet Core control plane.

This module intentionally owns runtime state, not fleet.yml semantics.  It only
accepts normalized JSON emitted by ``../spec/scripts/validate_fleet.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


COMMAND_TYPES = frozenset(
    {
        "fleet.provision",
        "context.sync",
        "task.assign",
        "message.send",
        "task.report",
        "fleet.reconcile",
    }
)
EVENT_SOURCE_TYPES = frozenset({"fleet", "member", "task", "runtime", "view", "provider", "system"})
TASK_TRANSITIONS = {
    "pending": frozenset({"assigned"}),
    "assigned": frozenset({"running"}),
    "running": frozenset({"blocked", "reported", "failed"}),
    "blocked": frozenset({"running"}),
    "reported": frozenset({"running", "accepted"}),
    "accepted": frozenset(),
    "failed": frozenset(),
}


class FleetError(RuntimeError):
    """A user-actionable control-plane error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load_fleet_config(
    path: Path,
    validator_command: Path | None = None,
    role_catalog: Path | None = None,
) -> Mapping[str, Any]:
    """Validate fleet.yml through the stable spec CLI and return normalized JSON."""

    validator = validator_command or (
        Path(__file__).resolve().parent.parent / "spec" / "scripts" / "validate_fleet.py"
    )
    if not validator.is_file():
        raise FleetError(f"fleet spec validator not found: {validator}")
    argv = [sys.executable, str(validator), str(path)]
    if role_catalog is not None:
        argv.extend(["--role-catalog", str(role_catalog)])
    argv.append("--output-json")
    completed = subprocess.run(
        argv,
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
    config_hash TEXT NOT NULL,
    profile_ref TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS fleet_contexts (
    fleet_id TEXT PRIMARY KEY REFERENCES fleets(fleet_id),
    objective TEXT NOT NULL,
    completion_criteria_json TEXT NOT NULL,
    stop_conditions_json TEXT NOT NULL,
    manager_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member_context_state (
    fleet_id TEXT NOT NULL,
    agent_ref TEXT NOT NULL,
    context_revision INTEGER NOT NULL DEFAULT 1,
    confirmed_revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fleet_id, agent_ref),
    FOREIGN KEY (fleet_id, agent_ref) REFERENCES members(fleet_id, agent_ref)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT NOT NULL,
    fleet_id TEXT NOT NULL REFERENCES fleets(fleet_id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('pending','assigned','running','blocked','reported','accepted','failed')),
    assignee_ref TEXT,
    planned_assignee_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (fleet_id, task_id),
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
CREATE TABLE IF NOT EXISTS task_contexts (
    fleet_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    expected_output TEXT NOT NULL,
    completion_criteria_json TEXT NOT NULL,
    PRIMARY KEY (fleet_id, task_id),
    FOREIGN KEY (fleet_id, task_id) REFERENCES tasks(fleet_id, task_id)
);
CREATE TABLE IF NOT EXISTS task_dependencies (
    fleet_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    dependency_task_id TEXT NOT NULL,
    PRIMARY KEY (fleet_id, task_id, dependency_task_id),
    FOREIGN KEY (fleet_id, task_id) REFERENCES tasks(fleet_id, task_id),
    FOREIGN KEY (fleet_id, dependency_task_id) REFERENCES tasks(fleet_id, task_id)
);
CREATE TABLE IF NOT EXISTS task_reports (
    fleet_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    reporter_ref TEXT NOT NULL,
    report_json TEXT NOT NULL,
    next_report_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (fleet_id, report_id),
    FOREIGN KEY (fleet_id, task_id) REFERENCES tasks(fleet_id, task_id),
    FOREIGN KEY (fleet_id, reporter_ref) REFERENCES members(fleet_id, agent_ref)
);
CREATE TABLE IF NOT EXISTS report_deadline_state (
    fleet_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    latest_report_id TEXT NOT NULL,
    consecutive_missed_deadlines INTEGER NOT NULL DEFAULT 0,
    requires_user_decision INTEGER NOT NULL DEFAULT 0 CHECK (requires_user_decision IN (0,1)),
    notification_id TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (fleet_id, task_id),
    FOREIGN KEY (fleet_id, task_id) REFERENCES tasks(fleet_id, task_id),
    FOREIGN KEY (fleet_id, latest_report_id) REFERENCES task_reports(fleet_id, report_id)
);
CREATE TABLE IF NOT EXISTS outbox (
    command_id TEXT NOT NULL,
    fleet_id TEXT NOT NULL REFERENCES fleets(fleet_id),
    sender_ref TEXT NOT NULL,
    target_agent_ref TEXT NOT NULL,
    command_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','processing','sending','retry','delivered','unknown','abandoned','acknowledged')),
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    result_detail TEXT,
    delivered_at TEXT,
    activation_consumed_at TEXT,
    activation_session_id TEXT,
    activation_runtime_product TEXT,
    PRIMARY KEY (fleet_id, command_id),
    FOREIGN KEY (fleet_id, sender_ref) REFERENCES members(fleet_id, agent_ref),
    FOREIGN KEY (fleet_id, target_agent_ref) REFERENCES members(fleet_id, agent_ref)
);
CREATE TABLE IF NOT EXISTS operations (
    fleet_id TEXT NOT NULL REFERENCES fleets(fleet_id),
    operation_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (fleet_id, operation_id)
);
CREATE INDEX IF NOT EXISTS idx_events_fleet_event ON events(fleet_id, event_id);
CREATE INDEX IF NOT EXISTS idx_task_reports_task_created
    ON task_reports(fleet_id, task_id, created_at, report_id);
CREATE INDEX IF NOT EXISTS idx_outbox_target_status ON outbox(fleet_id, target_agent_ref, status);
CREATE INDEX IF NOT EXISTS idx_task_dependencies_dependency
    ON task_dependencies(fleet_id, dependency_task_id, task_id);
"""


class FleetStore:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            if Path(self.db_path).is_symlink():
                raise FleetError("Core state database must not be a symbolic link")
            parent = Path(self.db_path).parent
            parent_existed = parent.exists()
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not parent_existed:
                parent.chmod(0o700)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        if self.db_path != ":memory:":
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
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
        objective = str(spec.get("objective") or "").strip()
        completion_criteria = spec.get("completion_criteria")
        stop_conditions = spec.get("stop_conditions")
        if not objective:
            raise FleetError("normalized spec.objective is required")
        if not isinstance(completion_criteria, list) or not completion_criteria:
            raise FleetError("normalized spec.completion_criteria is required")
        if not isinstance(stop_conditions, list) or not stop_conditions:
            raise FleetError("normalized spec.stop_conditions is required")
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
        view = spec.get("view")
        profile_ref = (
            str(view.get("profile_ref") or "").strip()
            if isinstance(view, Mapping)
            else ""
        )
        config_hash = hashlib.sha256(
            json.dumps(
                config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        created_at = utc_now()
        with self.connect() as db:
            db.executescript(SCHEMA)
            fleet_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(fleets)")
            }
            if "config_hash" not in fleet_columns:
                db.execute(
                    "ALTER TABLE fleets ADD COLUMN config_hash TEXT NOT NULL DEFAULT ''"
                )
            if "profile_ref" not in fleet_columns:
                db.execute(
                    "ALTER TABLE fleets ADD COLUMN profile_ref TEXT NOT NULL DEFAULT ''"
                )
            context_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(member_context_state)")
            }
            if "confirmed_revision" not in context_columns:
                db.execute(
                    "ALTER TABLE member_context_state ADD COLUMN "
                    "confirmed_revision INTEGER NOT NULL DEFAULT 0"
                )
            outbox_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(outbox)")
            }
            for name in (
                "activation_consumed_at",
                "activation_session_id",
                "activation_runtime_product",
            ):
                if name not in outbox_columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {name} TEXT")
            existing = db.execute(
                "SELECT config_hash FROM fleets WHERE fleet_id=?", (fleet_id,)
            ).fetchone()
            if existing is not None:
                if existing["config_hash"] != config_hash:
                    raise FleetError(
                        f"fleet {fleet_id!r} already exists with a different configuration"
                    )
                counts = db.execute(
                    "SELECT (SELECT count(*) FROM members WHERE fleet_id=?) AS members,"
                    "(SELECT count(*) FROM tasks WHERE fleet_id=?) AS tasks",
                    (fleet_id, fleet_id),
                ).fetchone()
                return {
                    "fleet_id": fleet_id,
                    "members": counts["members"],
                    "tasks": counts["tasks"],
                    "idempotent": True,
                }
            db.execute(
                "INSERT INTO fleets(fleet_id,title,config_hash,profile_ref,created_at) "
                "VALUES(?,?,?,?,?)",
                (fleet_id, title, config_hash, profile_ref, created_at),
            )
            db.execute(
                "INSERT INTO fleet_contexts(fleet_id,objective,completion_criteria_json,"
                "stop_conditions_json,manager_ref) VALUES(?,?,?,?,?)",
                (
                    fleet_id,
                    objective,
                    json.dumps(completion_criteria, ensure_ascii=False, sort_keys=True),
                    json.dumps(stop_conditions, ensure_ascii=False, sort_keys=True),
                    manager_ref,
                ),
            )
            for item in members:
                if not isinstance(item, Mapping):
                    raise FleetError("each member must be a mapping")
                agent_ref = str(item.get("agent_ref") or item.get("ref") or "").strip()
                role_ref = str(item.get("role_ref") or "").strip()
                if not agent_ref or not role_ref:
                    raise FleetError("member agent_ref and role_ref are required")
                metadata = dict(item.get("metadata") or {})
                role_definition = item.get("role_definition")
                if not isinstance(role_definition, Mapping):
                    raise FleetError("member role_definition is required")
                metadata["role_definition"] = dict(role_definition)
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
                db.execute(
                    "INSERT INTO member_context_state(fleet_id,agent_ref,context_revision) "
                    "VALUES(?,?,1)",
                    (fleet_id, agent_ref),
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
            for item in tasks:
                task_id = str(item.get("task_id") or item.get("id") or "").strip()
                for dependency in item.get("depends_on") or []:
                    db.execute(
                        "INSERT INTO task_dependencies(fleet_id,task_id,dependency_task_id) "
                        "VALUES(?,?,?)",
                        (fleet_id, task_id, str(dependency)),
                    )
                expected_output = str(item.get("expected_output") or "").strip()
                task_completion = item.get("completion_criteria")
                if not expected_output:
                    raise FleetError("task.expected_output is required")
                if not isinstance(task_completion, list) or not task_completion:
                    raise FleetError("task.completion_criteria is required")
                db.execute(
                    "INSERT INTO task_contexts(fleet_id,task_id,expected_output,"
                    "completion_criteria_json) VALUES(?,?,?,?)",
                    (
                        fleet_id,
                        task_id,
                        expected_output,
                        json.dumps(task_completion, ensure_ascii=False, sort_keys=True),
                    ),
                )
            self._append_event(
                db, fleet_id, "fleet", fleet_id, "fleet.provisioned", {"source": "fleet.yml"}
            )
        return {"fleet_id": fleet_id, "members": len(members), "tasks": len(tasks)}

    @staticmethod
    def _enqueue_context_sync(
        db: sqlite3.Connection,
        fleet_id: str,
        agent_ref: str,
        revision: int,
        reason: str,
    ) -> str:
        manager = db.execute(
            "SELECT manager_ref FROM fleet_contexts WHERE fleet_id=?", (fleet_id,)
        ).fetchone()
        if manager is None:
            raise FleetError(f"unknown fleet: {fleet_id}")
        command_id = f"context-sync:{fleet_id}:{agent_ref}:revision:{revision}"
        existing = db.execute(
            "SELECT command_id FROM outbox WHERE fleet_id=? AND command_id=?",
            (fleet_id, command_id),
        ).fetchone()
        if existing is not None:
            return command_id
        db.execute(
            "UPDATE outbox SET status='abandoned',result_detail=? "
            "WHERE fleet_id=? AND target_agent_ref=? AND command_type='context.sync' "
            "AND status IN ('pending','retry')",
            ("superseded by a newer context revision", fleet_id, agent_ref),
        )
        previous = db.execute(
            "SELECT payload_json FROM outbox WHERE fleet_id=? AND target_agent_ref=? "
            "AND command_type='context.sync' ORDER BY created_at DESC,command_id DESC LIMIT 1",
            (fleet_id, agent_ref),
        ).fetchone()
        previous_payload = json.loads(previous["payload_json"]) if previous else {}
        previous_control = previous_payload.get("control")
        control = (
            dict(previous_control)
            if isinstance(previous_control, Mapping)
            else {
                "fleet_id": fleet_id,
                "reporting": {
                    "progress_action": "task.progress",
                    "state_action": "task.report",
                    "required_identity": agent_ref,
                    "manager_ref": manager["manager_ref"],
                },
            }
        )
        payload = {
            "reason": reason,
            "context_revision": revision,
            "activation_token": secrets.token_urlsafe(32),
            "control": control,
        }
        db.execute(
            "INSERT INTO outbox(command_id,fleet_id,sender_ref,target_agent_ref,command_type,"
            "payload_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                command_id,
                fleet_id,
                manager["manager_ref"],
                agent_ref,
                "context.sync",
                json.dumps(payload, sort_keys=True),
                "pending",
                utc_now(),
            ),
        )
        FleetStore._append_event(
            db,
            fleet_id,
            "system",
            command_id,
            "command.enqueued",
            {"command_type": "context.sync", "target_agent_ref": agent_ref},
        )
        return command_id

    @staticmethod
    def _bump_context(
        db: sqlite3.Connection, fleet_id: str, agent_ref: str, reason: str
    ) -> int:
        changed = db.execute(
            "UPDATE member_context_state SET context_revision=context_revision+1 "
            "WHERE fleet_id=? AND agent_ref=?",
            (fleet_id, agent_ref),
        )
        if changed.rowcount != 1:
            raise FleetError(f"unknown agent_ref: {agent_ref}")
        row = db.execute(
            "SELECT context_revision FROM member_context_state "
            "WHERE fleet_id=? AND agent_ref=?",
            (fleet_id, agent_ref),
        ).fetchone()
        revision = int(row["context_revision"])
        FleetStore._enqueue_context_sync(db, fleet_id, agent_ref, revision, reason)
        return revision

    @staticmethod
    def _context_capsule(
        db: sqlite3.Connection, fleet_id: str, agent_ref: str
    ) -> dict[str, Any]:
        member = db.execute(
            "SELECT m.role_ref,m.metadata_json,c.context_revision FROM members m "
            "JOIN member_context_state c ON c.fleet_id=m.fleet_id "
            "AND c.agent_ref=m.agent_ref WHERE m.fleet_id=? AND m.agent_ref=?",
            (fleet_id, agent_ref),
        ).fetchone()
        fleet = db.execute(
            "SELECT objective,completion_criteria_json,stop_conditions_json,manager_ref "
            "FROM fleet_contexts WHERE fleet_id=?",
            (fleet_id,),
        ).fetchone()
        if member is None or fleet is None:
            raise FleetError("role context is unavailable")
        assignments = []
        for task in db.execute(
            "SELECT t.task_id,t.status,t.description,tc.expected_output,"
            "tc.completion_criteria_json FROM tasks t JOIN task_contexts tc "
            "ON tc.fleet_id=t.fleet_id AND tc.task_id=t.task_id WHERE t.fleet_id=? "
            "AND t.assignee_ref=? AND t.status IN ('assigned','running','blocked','reported') "
            "ORDER BY t.task_id",
            (fleet_id, agent_ref),
        ):
            assignments.append(
                {
                    "task_id": task["task_id"],
                    "status": task["status"],
                    "instructions": task["description"],
                    "expected_output": task["expected_output"],
                    "completion_criteria": json.loads(task["completion_criteria_json"]),
                }
            )
        member_metadata = json.loads(member["metadata_json"])
        role_definition = member_metadata.get("role_definition")
        if not isinstance(role_definition, Mapping):
            raise FleetError("resolved role definition is unavailable")
        return {
            "fleet_id": fleet_id,
            "context_revision": member["context_revision"],
            "agent": {
                "agent_ref": agent_ref,
                "role_ref": member["role_ref"],
                "role_definition": dict(role_definition),
            },
            "fleet": {
                "objective": fleet["objective"],
                "completion_criteria": json.loads(fleet["completion_criteria_json"]),
                "stop_conditions": json.loads(fleet["stop_conditions_json"]),
            },
            "assignments": assignments,
            "reporting": {
                "manager_ref": fleet["manager_ref"],
                "strategy": "manager",
                "completion_requires_manager_acceptance": True,
            },
        }

    def confirm_context(
        self, fleet_id: str, agent_ref: str, revision: int
    ) -> dict[str, Any]:
        if revision < 1:
            raise FleetError("context revision must be positive")
        with self.connect() as db:
            current = db.execute(
                "SELECT context_revision,confirmed_revision FROM member_context_state "
                "WHERE fleet_id=? AND agent_ref=?",
                (fleet_id, agent_ref),
            ).fetchone()
            if current is None:
                raise FleetError(f"unknown agent_ref: {agent_ref}")
            if revision != current["context_revision"]:
                raise FleetError(
                    "context revision is not current: "
                    f"expected {current['context_revision']}, got {revision}"
                )
            db.execute(
                "UPDATE member_context_state SET confirmed_revision=? "
                "WHERE fleet_id=? AND agent_ref=?",
                (revision, fleet_id, agent_ref),
            )
            self._append_event(
                db,
                fleet_id,
                "member",
                agent_ref,
                "context.confirmed",
                {"context_revision": revision},
            )
        return {
            "fleet_id": fleet_id,
            "agent_ref": agent_ref,
            "context_revision": revision,
            "status": "confirmed",
        }

    def consume_context_activation(
        self,
        fleet_id: str,
        command_id: str,
        activation_token: str,
        session_id: str,
        runtime_product: str,
    ) -> dict[str, Any]:
        """Consume one Core-issued context activation and return authoritative context."""

        if not session_id.strip():
            raise FleetError("session_id is required")
        if runtime_product not in {"claude", "codex"}:
            raise FleetError("runtime_product must be claude or codex")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            command = db.execute(
                "SELECT sender_ref,target_agent_ref,command_type,payload_json,status,"
                "activation_consumed_at,activation_session_id,activation_runtime_product "
                "FROM outbox WHERE fleet_id=? AND command_id=?",
                (fleet_id, command_id),
            ).fetchone()
            if command is None or command["command_type"] != "context.sync":
                raise FleetError("context activation is invalid")
            manager = db.execute(
                "SELECT manager_ref FROM fleet_contexts WHERE fleet_id=?", (fleet_id,)
            ).fetchone()
            if manager is None or command["sender_ref"] != manager["manager_ref"]:
                raise FleetError("context activation source is invalid")
            payload = json.loads(command["payload_json"])
            expected_token = payload.get("activation_token")
            if (
                not isinstance(expected_token, str)
                or not secrets.compare_digest(expected_token, activation_token)
            ):
                raise FleetError("context activation token is invalid")
            already_consumed = command["activation_consumed_at"] is not None
            if already_consumed and (
                command["activation_session_id"] != session_id
                or command["activation_runtime_product"] != runtime_product
            ):
                raise FleetError("context activation was already consumed")
            if command["status"] not in {
                "processing",
                "sending",
                "delivered",
                "unknown",
            }:
                raise FleetError("context activation is not being delivered")
            context = self._context_capsule(
                db, fleet_id, command["target_agent_ref"]
            )
            control = payload.get("control")
            safe_control = dict(control) if isinstance(control, Mapping) else {}
            if len(
                json.dumps(
                    {"context": context, "control": safe_control},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ) > 3_000:
                raise FleetError("context activation exceeds the safe hook size limit")
            expected_revision = payload.get("context_revision")
            if (
                not isinstance(expected_revision, int)
                or isinstance(expected_revision, bool)
                or expected_revision != context["context_revision"]
            ):
                raise FleetError("context activation revision is invalid")
            if not already_consumed:
                consumed_at = utc_now()
                db.execute(
                    "UPDATE outbox SET activation_consumed_at=?,activation_session_id=?,"
                    "activation_runtime_product=? WHERE fleet_id=? AND command_id=?",
                    (
                        consumed_at,
                        session_id,
                        runtime_product,
                        fleet_id,
                        command_id,
                    ),
                )
                db.execute(
                    "UPDATE member_context_state SET confirmed_revision=? "
                    "WHERE fleet_id=? AND agent_ref=?",
                    (
                        context["context_revision"],
                        fleet_id,
                        command["target_agent_ref"],
                    ),
                )
                db.execute(
                    "UPDATE outbox SET status='abandoned',result_detail=? "
                    "WHERE fleet_id=? AND target_agent_ref=? AND command_type='context.sync' "
                    "AND command_id<>? AND status IN ('pending','retry')",
                    (
                        "superseded by confirmed current context",
                        fleet_id,
                        command["target_agent_ref"],
                        command_id,
                    ),
                )
                self._append_event(
                    db,
                    fleet_id,
                    "member",
                    command["target_agent_ref"],
                    "context.confirmed",
                    {
                        "command_id": command_id,
                        "context_revision": context["context_revision"],
                        "runtime_product": runtime_product,
                        "session_id": session_id,
                    },
                )
                if command["status"] in {"sending", "unknown"}:
                    db.execute(
                        "UPDATE outbox SET status='delivered',result_detail=?,delivered_at=?,"
                        "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL "
                        "WHERE fleet_id=? AND command_id=?",
                        ("confirmed by Agent Fleet hook", consumed_at, fleet_id, command_id),
                    )
                    self._append_event(
                        db,
                        fleet_id,
                        "system",
                        command_id,
                        "delivery.delivered",
                        {"detail": "confirmed by Agent Fleet hook"},
                    )
        return {
            "fleet_id": fleet_id,
            "agent_ref": command["target_agent_ref"],
            "context": context,
            "control": safe_control,
            "status": "confirmed",
            "idempotent": already_consumed,
        }

    def _session_activation_payload(
        self,
        db: sqlite3.Connection,
        fleet_id: str,
        agent_ref: str,
        session_id: str,
        runtime_product: str,
        context_revision: int,
    ) -> dict[str, Any]:
        activation = db.execute(
            "SELECT payload_json FROM outbox WHERE fleet_id=? AND target_agent_ref=? "
            "AND command_type='context.sync' AND activation_session_id=? "
            "AND activation_runtime_product=? AND activation_consumed_at IS NOT NULL "
            "ORDER BY activation_consumed_at DESC,command_id DESC LIMIT 1",
            (fleet_id, agent_ref, session_id, runtime_product),
        ).fetchone()
        if activation is None:
            raise FleetError("session is not bound to this fleet member")
        payload = json.loads(activation["payload_json"])
        if payload.get("context_revision") != context_revision:
            raise FleetError("session context is not current")
        return payload

    def current_session_context(
        self,
        fleet_id: str,
        agent_ref: str,
        session_id: str,
        runtime_product: str,
    ) -> dict[str, Any]:
        """Return current context only for a Core-confirmed agent session."""

        if not session_id.strip():
            raise FleetError("session_id is required")
        if runtime_product not in {"claude", "codex"}:
            raise FleetError("runtime_product must be claude or codex")
        with self.connect() as db:
            state = db.execute(
                "SELECT context_revision,confirmed_revision FROM member_context_state "
                "WHERE fleet_id=? AND agent_ref=?",
                (fleet_id, agent_ref),
            ).fetchone()
            if state is None:
                raise FleetError("unknown fleet member")
            if state["confirmed_revision"] != state["context_revision"]:
                raise FleetError("session context is not current")
            payload = self._session_activation_payload(
                db,
                fleet_id,
                agent_ref,
                session_id,
                runtime_product,
                state["context_revision"],
            )
            control = payload.get("control")
            safe_control = dict(control) if isinstance(control, Mapping) else {}
            context = self._context_capsule(db, fleet_id, agent_ref)
        return {
            "fleet_id": fleet_id,
            "agent_ref": agent_ref,
            "context": context,
            "control": safe_control,
            "status": "current",
        }

    def invalidate_contexts(self, fleet_id: str) -> dict[str, Any]:
        """Close normal-command delivery until every new session confirms context."""

        with self.connect() as db:
            fleet = db.execute(
                "SELECT 1 FROM fleets WHERE fleet_id=?", (fleet_id,)
            ).fetchone()
            if fleet is None:
                raise FleetError(f"unknown fleet: {fleet_id}")
            updated = db.execute(
                "UPDATE member_context_state SET context_revision=context_revision+1,"
                "confirmed_revision=0 WHERE fleet_id=?",
                (fleet_id,),
            )
            db.execute(
                "UPDATE outbox SET status='abandoned',result_detail=? "
                "WHERE fleet_id=? AND command_type='context.sync' "
                "AND status IN ('pending','retry')",
                ("invalidated before a new runtime session", fleet_id),
            )
            self._append_event(
                db,
                fleet_id,
                "system",
                fleet_id,
                "context.invalidated",
                {"member_count": updated.rowcount},
            )
        return {"fleet_id": fleet_id, "status": "invalidated"}

    def _verify_command_receipt(
        self,
        db: sqlite3.Connection,
        fleet_id: str,
        command_id: str,
        command_document: Mapping[str, Any],
        session_id: str,
        runtime_product: str,
    ) -> tuple[sqlite3.Row, dict[str, Any], dict[str, Any], bool]:
        if not session_id.strip():
            raise FleetError("session_id is required")
        if runtime_product not in {"claude", "codex"}:
            raise FleetError("runtime_product must be claude or codex")
        command = db.execute(
            "SELECT command_id,fleet_id,sender_ref,target_agent_ref,command_type,"
            "payload_json,status,created_at,activation_consumed_at,"
            "activation_session_id,activation_runtime_product FROM outbox "
            "WHERE fleet_id=? AND command_id=?",
            (fleet_id, command_id),
        ).fetchone()
        if command is None or command["command_type"] == "context.sync":
            raise FleetError("command receipt is invalid")
        expected = self._command_document(db, command)
        expected["spec"].pop("context", None)
        received = json.loads(
            json.dumps(command_document, ensure_ascii=False, sort_keys=True)
        )
        received_spec = received.get("spec")
        if not isinstance(received_spec, dict):
            raise FleetError("command receipt contract is invalid")
        received_spec.pop("context", None)
        if received != expected:
            raise FleetError("command receipt content does not match Core")
        already_consumed = command["activation_consumed_at"] is not None
        if already_consumed and (
            command["activation_session_id"] != session_id
            or command["activation_runtime_product"] != runtime_product
        ):
            raise FleetError("command was already consumed by another session")
        if command["status"] not in {"sending", "delivered", "unknown"}:
            raise FleetError("command is not being delivered")
        state = db.execute(
            "SELECT context_revision,confirmed_revision FROM member_context_state "
            "WHERE fleet_id=? AND agent_ref=?",
            (fleet_id, command["target_agent_ref"]),
        ).fetchone()
        if state is None or state["context_revision"] != state["confirmed_revision"]:
            raise FleetError("command target context is not current")
        self._session_activation_payload(
            db,
            fleet_id,
            command["target_agent_ref"],
            session_id,
            runtime_product,
            state["context_revision"],
        )
        control_row = db.execute(
            "SELECT payload_json FROM outbox WHERE fleet_id=? AND target_agent_ref=? "
            "AND command_type='context.sync' ORDER BY created_at DESC,command_id DESC LIMIT 1",
            (fleet_id, command["target_agent_ref"]),
        ).fetchone()
        control_payload = (
            json.loads(control_row["payload_json"])
            if control_row is not None
            else {}
        )
        control = control_payload.get("control")
        safe_control = dict(control) if isinstance(control, Mapping) else {}
        context = self._context_capsule(db, fleet_id, command["target_agent_ref"])
        return command, context, safe_control, already_consumed

    def prepare_command(
        self,
        fleet_id: str,
        command_id: str,
        command_document: Mapping[str, Any],
        session_id: str,
        runtime_product: str,
    ) -> dict[str, Any]:
        """Validate a command without confirming delivery."""

        with self.connect() as db:
            command, context, control, already_consumed = self._verify_command_receipt(
                db,
                fleet_id,
                command_id,
                command_document,
                session_id,
                runtime_product,
            )
        return {
            "fleet_id": fleet_id,
            "agent_ref": command["target_agent_ref"],
            "context": context,
            "control": control,
            "status": "prepared",
            "idempotent": already_consumed,
        }

    def consume_command(
        self,
        fleet_id: str,
        command_id: str,
        command_document: Mapping[str, Any],
        session_id: str,
        runtime_product: str,
    ) -> dict[str, Any]:
        """Confirm a locally persisted command receipt for one session."""

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            command, context, safe_control, already_consumed = self._verify_command_receipt(
                db,
                fleet_id,
                command_id,
                command_document,
                session_id,
                runtime_product,
            )
            if not already_consumed:
                consumed_at = utc_now()
                db.execute(
                    "UPDATE outbox SET activation_consumed_at=?,activation_session_id=?,"
                    "activation_runtime_product=? WHERE fleet_id=? AND command_id=?",
                    (consumed_at, session_id, runtime_product, fleet_id, command_id),
                )
                self._append_event(
                    db,
                    fleet_id,
                    "member",
                    command["target_agent_ref"],
                    "command.received",
                    {
                        "command_id": command_id,
                        "command_type": command["command_type"],
                        "runtime_product": runtime_product,
                        "session_id": session_id,
                    },
                )
                if command["status"] in {"sending", "unknown"}:
                    db.execute(
                        "UPDATE outbox SET status='delivered',result_detail=?,delivered_at=?,"
                        "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL "
                        "WHERE fleet_id=? AND command_id=?",
                        ("confirmed by Agent Fleet hook", consumed_at, fleet_id, command_id),
                    )
                    self._append_event(
                        db,
                        fleet_id,
                        "system",
                        command_id,
                        "delivery.delivered",
                        {"detail": "confirmed by Agent Fleet hook"},
                    )
        return {
            "fleet_id": fleet_id,
            "agent_ref": command["target_agent_ref"],
            "context": context,
            "control": safe_control,
            "status": "received",
            "idempotent": already_consumed,
        }

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

    @staticmethod
    def _assign_pending_in_tx(
        db: sqlite3.Connection,
        fleet_id: str,
        task_id: str,
        agent_ref: str,
        manager_ref: str,
        command_id: str,
    ) -> None:
        task = db.execute(
            "SELECT status,planned_assignee_ref,title,description FROM tasks "
            "WHERE fleet_id=? AND task_id=?",
            (fleet_id, task_id),
        ).fetchone()
        if task is None:
            raise FleetError(f"unknown task: {task_id}")
        if task["status"] != "pending":
            raise FleetError(f"task {task_id!r} is not pending")
        if task["planned_assignee_ref"] != agent_ref:
            raise FleetError(
                f"task {task_id!r} is declared for agent_ref "
                f"{task['planned_assignee_ref']!r}"
            )
        now = utc_now()
        db.execute(
            "UPDATE tasks SET status='assigned',assignee_ref=?,updated_at=? "
            "WHERE fleet_id=? AND task_id=?",
            (agent_ref, now, fleet_id, task_id),
        )
        FleetStore._bump_context(db, fleet_id, agent_ref, "task_assigned")
        FleetStore._append_event(
            db, fleet_id, "task", task_id, "task.assigned", {"agent_ref": agent_ref}
        )
        db.execute(
            "INSERT INTO outbox(command_id,fleet_id,sender_ref,target_agent_ref,command_type,"
            "payload_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                command_id,
                fleet_id,
                manager_ref,
                agent_ref,
                "task.assign",
                json.dumps(
                    {
                        "task_id": task_id,
                        "title": task["title"],
                        "description": task["description"],
                    },
                    sort_keys=True,
                ),
                "pending",
                now,
            ),
        )
        FleetStore._append_event(
            db,
            fleet_id,
            "system",
            command_id,
            "command.enqueued",
            {"command_type": "task.assign", "target_agent_ref": agent_ref},
        )

    def assign(
        self,
        fleet_id: str,
        task_id: str,
        agent_ref: str,
        manager_ref: str,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        command_id = command_id or str(uuid.uuid4())
        with self.connect() as db:
            manager = db.execute(
                "SELECT is_manager FROM members WHERE fleet_id=? AND agent_ref=?",
                (fleet_id, manager_ref),
            ).fetchone()
            if manager is None or not manager["is_manager"]:
                raise FleetError("only a fleet manager may assign tasks")
            member = db.execute(
                "SELECT 1 FROM members WHERE fleet_id=? AND agent_ref=?",
                (fleet_id, agent_ref),
            ).fetchone()
            if member is None:
                raise FleetError(f"unknown agent_ref: {agent_ref}")
            task = db.execute(
                "SELECT status,assignee_ref,planned_assignee_ref,title,description FROM tasks "
                "WHERE fleet_id=? AND task_id=?",
                (fleet_id, task_id),
            ).fetchone()
            if task is None:
                raise FleetError(f"unknown task: {task_id}")
            if task["planned_assignee_ref"] and task["planned_assignee_ref"] != agent_ref:
                raise FleetError(
                    f"task {task_id!r} is declared for agent_ref {task['planned_assignee_ref']!r}"
                )
            if command_id and task["assignee_ref"] == agent_ref:
                existing_command = db.execute(
                    "SELECT target_agent_ref,command_type FROM outbox "
                    "WHERE fleet_id=? AND command_id=?",
                    (fleet_id, command_id),
                ).fetchone()
                if (
                    existing_command is not None
                    and existing_command["target_agent_ref"] == agent_ref
                    and existing_command["command_type"] == "task.assign"
                ):
                    return {
                        "task_id": task_id,
                        "status": task["status"],
                        "assignee_ref": agent_ref,
                        "command_id": command_id,
                        "idempotent": True,
                    }
            self._require_transition(str(task["status"]), "assigned")
            self._assign_pending_in_tx(
                db, fleet_id, task_id, agent_ref, manager_ref, command_id
            )
        return {
            "task_id": task_id,
            "status": "assigned",
            "assignee_ref": agent_ref,
            "command_id": command_id,
        }

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
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        if target == "completed":
            target = "reported"
        if target not in TASK_TRANSITIONS:
            raise FleetError(f"unknown task status: {target}")
        if target in {"reported", "failed", "blocked"} and report is None:
            raise FleetError(f"task.report payload is required for {target}")
        with self.connect() as db:
            fingerprint = json.dumps(
                {
                    "action": "task.report",
                    "task_id": task_id,
                    "target": target,
                    "agent_ref": agent_ref,
                    "report": report,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            if operation_id:
                existing_operation = db.execute(
                    "SELECT fingerprint,result_json FROM operations "
                    "WHERE fleet_id=? AND operation_id=?",
                    (fleet_id, operation_id),
                ).fetchone()
                if existing_operation is not None:
                    if existing_operation["fingerprint"] != fingerprint:
                        raise FleetError("operation_id already has different content")
                    result = json.loads(existing_operation["result_json"])
                    result["idempotent"] = True
                    return result
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
            if target in {"reported", "failed"}:
                deadline_state = db.execute(
                    "SELECT notification_id FROM report_deadline_state "
                    "WHERE fleet_id=? AND task_id=?",
                    (fleet_id, task_id),
                ).fetchone()
                if deadline_state is not None and deadline_state["notification_id"]:
                    db.execute(
                        "UPDATE outbox SET status='abandoned',result_detail=? "
                        "WHERE fleet_id=? AND command_id=? AND status IN ('pending','retry')",
                        (
                            f"task entered terminal report state: {target}",
                            fleet_id,
                            deadline_state["notification_id"],
                        ),
                    )
                db.execute(
                    "DELETE FROM report_deadline_state WHERE fleet_id=? AND task_id=?",
                    (fleet_id, task_id),
                )
            db.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE fleet_id=? AND task_id=?",
                (target, utc_now(), fleet_id, task_id),
            )
            if target != "running":
                self._bump_context(db, fleet_id, agent_ref, f"task_{target}")
            payload = {"from": current, "to": target, "report": report or {}}
            self._append_event(db, fleet_id, "task", task_id, "task.reported", payload)
            if target in {"reported", "blocked", "failed"}:
                manager = db.execute(
                    "SELECT manager_ref FROM fleet_contexts WHERE fleet_id=?",
                    (fleet_id,),
                ).fetchone()
                if manager is None:
                    raise FleetError(f"unknown fleet: {fleet_id}")
                command_id = f"task-report:{fleet_id}:{task_id}:{uuid.uuid4()}"
                db.execute(
                    "INSERT INTO outbox(command_id,fleet_id,sender_ref,target_agent_ref,"
                    "command_type,payload_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        command_id,
                        fleet_id,
                        agent_ref,
                        manager["manager_ref"],
                        "task.report",
                        json.dumps(
                            {
                                "task_id": task_id,
                                "status": target,
                                "report": report or {},
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
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
                    {
                        "command_type": "task.report",
                        "target_agent_ref": manager["manager_ref"],
                    },
                )
            result = {
                "task_id": task_id,
                "status": target,
                "assignee_ref": task["assignee_ref"],
            }
            if operation_id:
                db.execute(
                    "INSERT INTO operations(fleet_id,operation_id,fingerprint,result_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        fleet_id,
                        operation_id,
                        fingerprint,
                        json.dumps(result, sort_keys=True),
                        utc_now(),
                    ),
                )
        return result

    def accept_task(
        self,
        fleet_id: str,
        task_id: str,
        manager_ref: str,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as db:
            fingerprint = json.dumps(
                {
                    "action": "task.accept",
                    "task_id": task_id,
                    "manager_ref": manager_ref,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            if operation_id:
                existing_operation = db.execute(
                    "SELECT fingerprint,result_json FROM operations "
                    "WHERE fleet_id=? AND operation_id=?",
                    (fleet_id, operation_id),
                ).fetchone()
                if existing_operation is not None:
                    if existing_operation["fingerprint"] != fingerprint:
                        raise FleetError("operation_id already has different content")
                    result = json.loads(existing_operation["result_json"])
                    result["idempotent"] = True
                    return result
            manager = db.execute(
                "SELECT is_manager FROM members WHERE fleet_id=? AND agent_ref=?",
                (fleet_id, manager_ref),
            ).fetchone()
            if manager is None or not manager["is_manager"]:
                raise FleetError("only a fleet manager may accept tasks")
            task = db.execute(
                "SELECT status,assignee_ref FROM tasks WHERE fleet_id=? AND task_id=?",
                (fleet_id, task_id),
            ).fetchone()
            if task is None:
                raise FleetError(f"unknown task: {task_id}")
            self._require_transition(str(task["status"]), "accepted")
            db.execute(
                "UPDATE tasks SET status='accepted',updated_at=? WHERE fleet_id=? AND task_id=?",
                (utc_now(), fleet_id, task_id),
            )
            if task["assignee_ref"]:
                self._bump_context(
                    db, fleet_id, task["assignee_ref"], "task_accepted"
                )
            self._append_event(
                db,
                fleet_id,
                "task",
                task_id,
                "task.accepted",
                {"manager_ref": manager_ref},
            )
            ready = list(
                db.execute(
                    "SELECT t.task_id,t.planned_assignee_ref FROM tasks t "
                    "WHERE t.fleet_id=? AND t.status='pending' "
                    "AND EXISTS (SELECT 1 FROM task_dependencies changed "
                    "WHERE changed.fleet_id=t.fleet_id AND changed.task_id=t.task_id "
                    "AND changed.dependency_task_id=?) "
                    "AND NOT EXISTS (SELECT 1 FROM task_dependencies d "
                    "JOIN tasks dependency ON dependency.fleet_id=d.fleet_id "
                    "AND dependency.task_id=d.dependency_task_id "
                    "WHERE d.fleet_id=t.fleet_id AND d.task_id=t.task_id "
                    "AND dependency.status<>'accepted') ORDER BY t.task_id",
                    (fleet_id, task_id),
                )
            )
            released_tasks: list[str] = []
            for successor in ready:
                assignee = successor["planned_assignee_ref"]
                if not assignee:
                    raise FleetError(
                        f"task {successor['task_id']!r} has no planned assignee"
                    )
                self._assign_pending_in_tx(
                    db,
                    fleet_id,
                    successor["task_id"],
                    assignee,
                    manager_ref,
                    f"task-assign:{fleet_id}:{successor['task_id']}:dependency-release",
                )
                released_tasks.append(successor["task_id"])
            result = {
            "task_id": task_id,
            "status": "accepted",
            "assignee_ref": task["assignee_ref"],
            "accepted_by": manager_ref,
            "released_tasks": released_tasks,
            }
            if operation_id:
                db.execute(
                    "INSERT INTO operations(fleet_id,operation_id,fingerprint,result_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        fleet_id,
                        operation_id,
                        fingerprint,
                        json.dumps(result, sort_keys=True),
                        utc_now(),
                    ),
                )
        return result

    def report_progress(
        self,
        fleet_id: str,
        task_id: str,
        agent_ref: str,
        report_id: str,
        report: Mapping[str, Any],
        next_report_at: str,
    ) -> dict[str, Any]:
        if not report_id.strip():
            raise FleetError("report_id is required")
        if not report:
            raise FleetError("progress report must not be empty")
        try:
            due_at = datetime.fromisoformat(next_report_at)
        except ValueError as exc:
            raise FleetError("next_report_at must be an ISO 8601 timestamp") from exc
        if due_at.tzinfo is None:
            raise FleetError("next_report_at must include a timezone")
        canonical_report = json.dumps(report, sort_keys=True, separators=(",", ":"))
        notification_id = f"report-notification:{report_id}"
        with self.connect() as db:
            existing = db.execute(
                "SELECT task_id,reporter_ref,report_json,next_report_at,created_at "
                "FROM task_reports WHERE fleet_id=? AND report_id=?",
                (fleet_id, report_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["task_id"] != task_id
                    or existing["reporter_ref"] != agent_ref
                    or existing["report_json"] != canonical_report
                    or existing["next_report_at"] != next_report_at
                ):
                    raise FleetError("report_id already exists with different content")
                return {
                    "report_id": report_id,
                    "task_id": task_id,
                    "status": "recorded",
                    "notification_id": notification_id,
                    "created_at": existing["created_at"],
                    "idempotent": True,
                }

            task = db.execute(
                "SELECT status,assignee_ref FROM tasks WHERE fleet_id=? AND task_id=?",
                (fleet_id, task_id),
            ).fetchone()
            if task is None:
                raise FleetError(f"unknown task: {task_id}")
            if task["assignee_ref"] != agent_ref:
                raise FleetError("only the assigned agent may report task progress")
            if task["status"] not in {"running", "blocked"}:
                raise FleetError("progress may only be reported for running or blocked tasks")
            manager = db.execute(
                "SELECT agent_ref FROM members WHERE fleet_id=? AND is_manager=1",
                (fleet_id,),
            ).fetchone()
            if manager is None:
                raise FleetError("fleet manager is missing")
            created_at = utc_now()
            previous_deadline = db.execute(
                "SELECT notification_id FROM report_deadline_state "
                "WHERE fleet_id=? AND task_id=?",
                (fleet_id, task_id),
            ).fetchone()
            if previous_deadline is not None and previous_deadline["notification_id"]:
                db.execute(
                    "UPDATE outbox SET status='abandoned',result_detail=? "
                    "WHERE fleet_id=? AND command_id=? AND status IN ('pending','retry')",
                    (
                        "superseded by a newer progress report",
                        fleet_id,
                        previous_deadline["notification_id"],
                    ),
                )
            db.execute(
                "DELETE FROM report_deadline_state WHERE fleet_id=? AND task_id=?",
                (fleet_id, task_id),
            )
            db.execute(
                "INSERT INTO task_reports(fleet_id,report_id,task_id,reporter_ref,report_json,"
                "next_report_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    fleet_id,
                    report_id,
                    task_id,
                    agent_ref,
                    canonical_report,
                    next_report_at,
                    created_at,
                ),
            )
            db.execute(
                "INSERT INTO outbox(command_id,fleet_id,sender_ref,target_agent_ref,command_type,"
                "payload_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    notification_id,
                    fleet_id,
                    agent_ref,
                    manager["agent_ref"],
                    "message.send",
                    json.dumps(
                        {
                            "notification_type": "task.progress",
                            "report_id": report_id,
                            "task_id": task_id,
                            "reporter_ref": agent_ref,
                            "report": report,
                            "next_report_at": next_report_at,
                        },
                        sort_keys=True,
                    ),
                    "pending",
                    created_at,
                ),
            )
            self._append_event(
                db,
                fleet_id,
                "task",
                task_id,
                "task.progress.reported",
                {
                    "report_id": report_id,
                    "reporter_ref": agent_ref,
                    "next_report_at": next_report_at,
                },
            )
        return {
            "report_id": report_id,
            "task_id": task_id,
            "status": "recorded",
            "notification_id": notification_id,
            "created_at": created_at,
            "idempotent": False,
        }

    def check_report_deadlines(
        self, fleet_id: str, now: str | None = None
    ) -> dict[str, Any]:
        """Record overdue facts and enqueue only the role-appropriate next action."""

        checked_time = self._parse_timestamp(now or utc_now(), "now")
        checked_at = checked_time.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        )
        observed: list[dict[str, Any]] = []
        changed = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            manager = db.execute(
                "SELECT manager_ref FROM fleet_contexts WHERE fleet_id=?", (fleet_id,)
            ).fetchone()
            if manager is None:
                raise FleetError(f"unknown fleet: {fleet_id}")
            for task in db.execute(
                "SELECT task_id,status,assignee_ref FROM tasks WHERE fleet_id=? "
                "AND status IN ('running','blocked') ORDER BY task_id",
                (fleet_id,),
            ):
                reports = list(
                    db.execute(
                        "SELECT report_id,created_at,next_report_at FROM task_reports "
                        "WHERE fleet_id=? AND task_id=? "
                        "ORDER BY created_at,report_id",
                        (fleet_id, task["task_id"]),
                    )
                )
                if not reports:
                    continue
                latest = reports[-1]
                latest_deadline = self._parse_timestamp(
                    latest["next_report_at"], "next_report_at"
                )
                missed = 0
                if latest_deadline < checked_time:
                    missed = 1
                    for index in range(len(reports) - 2, -1, -1):
                        deadline = self._parse_timestamp(
                            reports[index]["next_report_at"], "next_report_at"
                        )
                        next_reported = self._parse_timestamp(
                            reports[index + 1]["created_at"], "created_at"
                        )
                        if next_reported <= deadline:
                            break
                        missed += 1
                requires_user_decision = missed >= 2
                notification_id = None
                notification_type = None
                target_ref = None
                if missed == 1:
                    notification_type = "task.progress.check_required"
                    target_ref = task["assignee_ref"]
                elif requires_user_decision:
                    notification_type = "task.progress.user_decision_required"
                    target_ref = manager["manager_ref"]
                if notification_type and target_ref:
                    notification_id = (
                        f"report-deadline:{fleet_id}:{task['task_id']}:"
                        f"{latest['report_id']}:{notification_type}"
                    )
                existing = db.execute(
                    "SELECT latest_report_id,consecutive_missed_deadlines,"
                    "requires_user_decision,notification_id FROM report_deadline_state "
                    "WHERE fleet_id=? AND task_id=?",
                    (fleet_id, task["task_id"]),
                ).fetchone()
                same_state = (
                    existing is not None
                    and existing["latest_report_id"] == latest["report_id"]
                    and existing["consecutive_missed_deadlines"] == missed
                    and bool(existing["requires_user_decision"])
                    == requires_user_decision
                    and existing["notification_id"] == notification_id
                )
                if not same_state:
                    changed = True
                    if (
                        existing is not None
                        and existing["notification_id"]
                        and existing["notification_id"] != notification_id
                    ):
                        db.execute(
                            "UPDATE outbox SET status='abandoned',result_detail=? "
                            "WHERE fleet_id=? AND command_id=? "
                            "AND status IN ('pending','retry')",
                            (
                                "superseded by a newer deadline evaluation",
                                fleet_id,
                                existing["notification_id"],
                            ),
                        )
                    db.execute(
                        "INSERT INTO report_deadline_state("
                        "fleet_id,task_id,latest_report_id,consecutive_missed_deadlines,"
                        "requires_user_decision,notification_id,checked_at) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(fleet_id,task_id) DO UPDATE SET "
                        "latest_report_id=excluded.latest_report_id,"
                        "consecutive_missed_deadlines=excluded.consecutive_missed_deadlines,"
                        "requires_user_decision=excluded.requires_user_decision,"
                        "notification_id=excluded.notification_id,checked_at=excluded.checked_at",
                        (
                            fleet_id,
                            task["task_id"],
                            latest["report_id"],
                            missed,
                            int(requires_user_decision),
                            notification_id,
                            checked_at,
                        ),
                    )
                    self._append_event(
                        db,
                        fleet_id,
                        "task",
                        task["task_id"],
                        "task.progress.deadline_evaluated",
                        {
                            "latest_report_id": latest["report_id"],
                            "consecutive_missed_deadlines": missed,
                            "requires_user_decision": requires_user_decision,
                        },
                    )
                if notification_id:
                    existing_command = db.execute(
                        "SELECT 1 FROM outbox WHERE fleet_id=? AND command_id=?",
                        (fleet_id, notification_id),
                    ).fetchone()
                    if existing_command is None:
                        changed = True
                        payload = {
                            "notification_type": notification_type,
                            "task_id": task["task_id"],
                            "assignee_ref": task["assignee_ref"],
                            "latest_report_id": latest["report_id"],
                            "next_report_at": latest["next_report_at"],
                            "consecutive_missed_deadlines": missed,
                        }
                        db.execute(
                            "INSERT INTO outbox(command_id,fleet_id,sender_ref,"
                            "target_agent_ref,command_type,payload_json,status,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (
                                notification_id,
                                fleet_id,
                                manager["manager_ref"],
                                target_ref,
                                "message.send",
                                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                "pending",
                                checked_at,
                            ),
                        )
                        self._append_event(
                            db,
                            fleet_id,
                            "system",
                            notification_id,
                            "command.enqueued",
                            {
                                "command_type": "message.send",
                                "target_agent_ref": target_ref,
                            },
                        )
                if missed:
                    observed.append(
                        {
                            "task_id": task["task_id"],
                            "status": task["status"],
                            "consecutive_missed_deadlines": missed,
                            "requires_user_decision": requires_user_decision,
                            "notification_id": notification_id,
                        }
                    )
        return {
            "fleet_id": fleet_id,
            "checked_at": checked_at,
            "tasks": observed,
            "idempotent": not changed,
        }

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
            existing = db.execute(
                "SELECT sender_ref,target_agent_ref,command_type,payload_json,status "
                "FROM outbox WHERE fleet_id=? AND command_id=?",
                (fleet_id, command_id),
            ).fetchone()
            if existing is not None:
                existing_payload = json.loads(existing["payload_json"])
                comparable_existing = dict(existing_payload)
                comparable_existing.pop("activation_token", None)
                comparable_existing.pop("context_revision", None)
                if (
                    existing["sender_ref"] == sender_ref
                    and existing["target_agent_ref"] == target_agent_ref
                    and existing["command_type"] == command_type
                    and (
                        existing_payload == dict(payload)
                        or (
                            command_type == "context.sync"
                            and comparable_existing == dict(payload)
                        )
                    )
                ):
                    return {
                        "command_id": command_id,
                        "status": existing["status"],
                        "idempotent": True,
                    }
                raise FleetError(f"command_id {command_id!r} already has different content")
            stored_payload = dict(payload)
            if command_type == "context.sync":
                state = db.execute(
                    "SELECT context_revision FROM member_context_state "
                    "WHERE fleet_id=? AND agent_ref=?",
                    (fleet_id, target_agent_ref),
                ).fetchone()
                if state is None:
                    raise FleetError(f"unknown target agent_ref: {target_agent_ref}")
                stored_payload.pop("activation_token", None)
                stored_payload["context_revision"] = int(state["context_revision"])
                stored_payload["activation_token"] = secrets.token_urlsafe(32)
                db.execute(
                    "UPDATE outbox SET status='abandoned',result_detail=? "
                    "WHERE fleet_id=? AND target_agent_ref=? "
                    "AND command_type='context.sync' AND status IN ('pending','retry')",
                    (
                        "superseded by an explicit current context",
                        fleet_id,
                        target_agent_ref,
                    ),
                )
            encoded_payload = json.dumps(stored_payload, sort_keys=True)
            db.execute(
                "INSERT INTO outbox(command_id,fleet_id,sender_ref,target_agent_ref,command_type,"
                "payload_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    command_id,
                    fleet_id,
                    sender_ref,
                    target_agent_ref,
                    command_type,
                    encoded_payload,
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
                "UPDATE outbox SET status='acknowledged',acknowledged_at=? "
                "WHERE fleet_id=? AND command_id=?",
                (acknowledged_at, fleet_id, command_id),
            )
            self._append_event(
                db, fleet_id, "system", command_id, "command.acknowledged", {"agent_ref": agent_ref}
            )
        return {"command_id": command_id, "status": "acknowledged"}

    @staticmethod
    def _parse_timestamp(value: str, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise FleetError(f"{label} must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise FleetError(f"{label} must include a timezone")
        return parsed

    def _command_document(
        self, db: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        return {
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
                "context": self._context_capsule(
                    db, row["fleet_id"], row["target_agent_ref"]
                ),
            },
        }

    def claim_delivery(
        self,
        fleet_id: str,
        delivery_worker_id: str,
        now: str | None = None,
        lease_seconds: int = 30,
    ) -> dict[str, Any] | None:
        if not delivery_worker_id.strip():
            raise FleetError("delivery_worker_id is required")
        if lease_seconds <= 0:
            raise FleetError("lease_seconds must be positive")
        now_at = self._parse_timestamp(now or utc_now(), "now")
        now_value = now_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        lease_expires_at = (now_at + timedelta(seconds=lease_seconds)).isoformat(
            timespec="milliseconds"
        )
        lease_token = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired_sending = list(
                db.execute(
                    "SELECT command_id FROM outbox WHERE fleet_id=? AND status='sending' "
                    "AND lease_expires_at<=?",
                    (fleet_id, now_value),
                )
            )
            db.execute(
                "UPDATE outbox SET status='unknown',result_detail=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL WHERE fleet_id=? AND status='sending' "
                "AND lease_expires_at<=?",
                ("delivery process ended after external send began", fleet_id, now_value),
            )
            for expired in expired_sending:
                self._append_event(
                    db,
                    fleet_id,
                    "system",
                    expired["command_id"],
                    "delivery.unknown",
                    {"detail": "delivery lease expired after external send began"},
                )
            db.execute(
                "UPDATE outbox SET status='pending',lease_owner=NULL,lease_token=NULL,"
                "lease_expires_at=NULL WHERE fleet_id=? AND status='processing' "
                "AND lease_expires_at<=?",
                (fleet_id, now_value),
            )
            row = db.execute(
                "SELECT command_id,fleet_id,sender_ref,target_agent_ref,command_type,payload_json,"
                "created_at,attempt_count FROM outbox WHERE fleet_id=? AND "
                "(status='pending' OR (status='retry' AND next_attempt_at<=?)) "
                "AND (command_type='context.sync' OR EXISTS ("
                "SELECT 1 FROM member_context_state c WHERE c.fleet_id=outbox.fleet_id "
                "AND c.agent_ref=outbox.target_agent_ref "
                "AND c.confirmed_revision=c.context_revision)) "
                "ORDER BY CASE command_type "
                "WHEN 'task.report' THEN 0 WHEN 'context.sync' THEN 1 ELSE 2 END,"
                "created_at,command_id LIMIT 1",
                (fleet_id, now_value),
            ).fetchone()
            if row is None:
                return None
            updated = db.execute(
                "UPDATE outbox SET status='processing',lease_owner=?,lease_token=?,"
                "lease_expires_at=?,attempt_count=attempt_count+1 WHERE fleet_id=? "
                "AND command_id=? AND status IN ('pending','retry')",
                (
                    delivery_worker_id,
                    lease_token,
                    lease_expires_at,
                    fleet_id,
                    row["command_id"],
                ),
            )
            if updated.rowcount != 1:
                raise FleetError("delivery command was claimed by another worker")
            self._append_event(
                db,
                fleet_id,
                "system",
                row["command_id"],
                "delivery.claimed",
                {
                    "delivery_worker_id": delivery_worker_id,
                    "lease_expires_at": lease_expires_at,
                    "attempt_count": int(row["attempt_count"]) + 1,
                },
            )
            command_document = self._command_document(db, row)
        return {
            "command": command_document,
            "delivery": {
                "status": "processing",
                "lease_owner": delivery_worker_id,
                "lease_token": lease_token,
                "lease_expires_at": lease_expires_at,
                "attempt_count": int(row["attempt_count"]) + 1,
            },
        }

    def begin_delivery(
        self,
        fleet_id: str,
        command_id: str,
        lease_token: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Fence the point immediately before the external Herdr send begins."""

        with self.connect() as db:
            command = db.execute(
                "SELECT status,lease_token,lease_expires_at,attempt_count FROM outbox "
                "WHERE fleet_id=? AND command_id=?",
                (fleet_id, command_id),
            ).fetchone()
            if command is None:
                raise FleetError(f"unknown command: {command_id}")
            if command["status"] != "processing" or command["lease_token"] != lease_token:
                raise FleetError("delivery lease token is stale or invalid")
            current_time = self._parse_timestamp(now or utc_now(), "now")
            expires_at = self._parse_timestamp(command["lease_expires_at"], "lease_expires_at")
            if expires_at <= current_time:
                raise FleetError("delivery lease has expired")
            db.execute(
                "UPDATE outbox SET status='sending' WHERE fleet_id=? AND command_id=?",
                (fleet_id, command_id),
            )
            self._append_event(
                db,
                fleet_id,
                "system",
                command_id,
                "delivery.started",
                {"attempt_count": command["attempt_count"]},
            )
        return {
            "command_id": command_id,
            "status": "sending",
            "attempt_count": command["attempt_count"],
        }

    def record_delivery_result(
        self,
        fleet_id: str,
        command_id: str,
        lease_token: str,
        result: str,
        detail: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        if result not in {"delivered", "unknown", "retry", "abandoned"}:
            raise FleetError(f"unsupported delivery result: {result}")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            command = db.execute(
                "SELECT status,lease_token,lease_expires_at,attempt_count,"
                "activation_consumed_at FROM outbox "
                "WHERE fleet_id=? AND command_id=?",
                (fleet_id, command_id),
            ).fetchone()
            if command is None:
                raise FleetError(f"unknown command: {command_id}")
            if command["status"] == "delivered" and command["activation_consumed_at"]:
                return {
                    "command_id": command_id,
                    "status": "delivered",
                    "attempt_count": command["attempt_count"],
                    "idempotent": True,
                }
            if command["status"] == "processing" and command["lease_token"] == lease_token:
                raise FleetError("delivery has not started")
            if command["status"] != "sending" or command["lease_token"] != lease_token:
                raise FleetError("delivery lease token is stale or invalid")
            current_time = self._parse_timestamp(now or utc_now(), "now")
            completed_at = current_time.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            )
            expires_at = self._parse_timestamp(command["lease_expires_at"], "lease_expires_at")
            if expires_at <= current_time:
                raise FleetError("delivery lease has expired")
            if result == "delivered" and not command["activation_consumed_at"]:
                raise FleetError(
                    "delivery cannot be confirmed without an Agent Fleet hook receipt"
                )
            next_attempt_at = completed_at if result == "retry" else None
            db.execute(
                "UPDATE outbox SET status=?,result_detail=?,delivered_at=?,next_attempt_at=?,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL "
                "WHERE fleet_id=? AND command_id=?",
                (
                    result,
                    detail,
                    completed_at if result == "delivered" else None,
                    next_attempt_at,
                    fleet_id,
                    command_id,
                ),
            )
            self._append_event(
                db,
                fleet_id,
                "system",
                command_id,
                f"delivery.{result}",
                {"detail": detail, "attempt_count": command["attempt_count"]},
            )
        return {
            "command_id": command_id,
            "status": result,
            "attempt_count": command["attempt_count"],
        }

    def _task_status_rows(
        self, db: sqlite3.Connection, fleet_id: str
    ) -> list[dict[str, Any]]:
        tasks = [dict(row) for row in db.execute(
                "SELECT t.task_id,t.title,t.status,t.assignee_ref,t.updated_at,"
                "c.expected_output,c.completion_criteria_json "
                "FROM tasks t JOIN task_contexts c ON c.fleet_id=t.fleet_id "
                "AND c.task_id=t.task_id WHERE t.fleet_id=? ORDER BY t.task_id",
                (fleet_id,),
            )]
        for task in tasks:
            task["completion_criteria"] = json.loads(
                task.pop("completion_criteria_json")
            )
            latest_state_event = db.execute(
                "SELECT payload_json,created_at FROM events "
                "WHERE fleet_id=? AND entity_type='task' AND entity_id=? "
                "AND event_type='task.reported' ORDER BY event_id DESC LIMIT 1",
                (fleet_id, task["task_id"]),
            ).fetchone()
            if latest_state_event is None:
                task["latest_state_report"] = None
            else:
                state_payload = json.loads(latest_state_event["payload_json"])
                task["latest_state_report"] = {
                    "status": state_payload["to"],
                    "reporter_ref": task["assignee_ref"],
                    "report": state_payload.get("report", {}),
                    "created_at": latest_state_event["created_at"],
                }
            latest_report = db.execute(
                "SELECT report_id,reporter_ref,report_json,next_report_at,created_at "
                "FROM task_reports WHERE fleet_id=? AND task_id=? "
                "ORDER BY created_at DESC,report_id DESC LIMIT 1",
                (fleet_id, task["task_id"]),
            ).fetchone()
            if latest_report is None:
                task["latest_report"] = None
                task["next_report_at"] = None
            else:
                task["latest_report"] = {
                    "report_id": latest_report["report_id"],
                    "reporter_ref": latest_report["reporter_ref"],
                    "report": json.loads(latest_report["report_json"]),
                    "created_at": latest_report["created_at"],
                }
                task["next_report_at"] = latest_report["next_report_at"]
            deadline = db.execute(
                "SELECT consecutive_missed_deadlines,requires_user_decision,checked_at "
                "FROM report_deadline_state WHERE fleet_id=? AND task_id=?",
                (fleet_id, task["task_id"]),
            ).fetchone()
            task["consecutive_missed_deadlines"] = (
                int(deadline["consecutive_missed_deadlines"])
                if deadline is not None
                else 0
            )
            task["requires_user_decision"] = (
                bool(deadline["requires_user_decision"])
                if deadline is not None
                else False
            )
            task["deadline_checked_at"] = (
                deadline["checked_at"] if deadline is not None else None
            )
        return tasks

    def task_list(self, fleet_id: str) -> dict[str, Any]:
        with self.connect() as db:
            fleet = db.execute(
                "SELECT 1 FROM fleets WHERE fleet_id=?", (fleet_id,)
            ).fetchone()
            if fleet is None:
                raise FleetError(f"unknown fleet: {fleet_id}")
            return {"fleet_id": fleet_id, "tasks": self._task_status_rows(db, fleet_id)}

    def status(self, fleet_id: str) -> dict[str, Any]:
        with self.connect() as db:
            fleet = db.execute("SELECT * FROM fleets WHERE fleet_id=?", (fleet_id,)).fetchone()
            if fleet is None:
                raise FleetError(f"unknown fleet: {fleet_id}")
            members = [dict(row) for row in db.execute(
                "SELECT m.agent_ref,m.role_ref,m.is_manager,m.status,"
                "c.context_revision,c.confirmed_revision FROM members m "
                "JOIN member_context_state c ON c.fleet_id=m.fleet_id "
                "AND c.agent_ref=m.agent_ref WHERE m.fleet_id=? ORDER BY m.agent_ref",
                (fleet_id,),
            )]
            tasks = self._task_status_rows(db, fleet_id)
            pending = []
            for row in db.execute(
                "SELECT command_id,fleet_id,sender_ref,target_agent_ref,command_type,payload_json,created_at "
                "FROM outbox WHERE fleet_id=? AND status='pending' ORDER BY created_at,command_id",
                (fleet_id,),
            ):
                pending.append(self._command_document(db, row))
            delivery_counts = {
                row["status"]: row["count"]
                for row in db.execute(
                    "SELECT status,count(*) AS count FROM outbox WHERE fleet_id=? GROUP BY status",
                    (fleet_id,),
                )
            }
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
            "delivery_counts": delivery_counts,
            "events": events,
        }

    def remove_fleet(self, fleet_id: str, confirmation: str) -> dict[str, Any]:
        if confirmation != fleet_id:
            raise FleetError("fleet removal requires an exact --confirm-fleet value")
        with self.connect() as db:
            exists = db.execute(
                "SELECT 1 FROM fleets WHERE fleet_id=?", (fleet_id,)
            ).fetchone()
            if exists is None:
                return {"fleet_id": fleet_id, "status": "absent", "idempotent": True}
            for table in (
                "outbox",
                "events",
                "report_deadline_state",
                "task_reports",
                "task_dependencies",
                "task_contexts",
                "operations",
                "tasks",
                "member_context_state",
                "fleet_contexts",
                "members",
                "fleets",
            ):
                db.execute(f"DELETE FROM {table} WHERE fleet_id=?", (fleet_id,))
        return {"fleet_id": fleet_id, "status": "removed"}


def _json_object(value: str) -> Mapping[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-control")
    parser.add_argument("--db", type=Path, required=True, help="SQLite state path")
    sub = parser.add_subparsers(dest="action", required=True)
    validate = sub.add_parser("spec.validate")
    validate.add_argument("--config", type=Path, required=True)
    validate.add_argument("--role-catalog", type=Path, required=True)
    init = sub.add_parser("init", aliases=["fleet.provision"])
    init.add_argument("--config", type=Path, required=True)
    init.add_argument("--role-catalog", type=Path, required=True)
    status = sub.add_parser("status", aliases=["fleet.reconcile"])
    status.add_argument("--fleet", required=True)
    task_list = sub.add_parser("task.list")
    task_list.add_argument("--fleet", required=True)
    remove = sub.add_parser("fleet.remove")
    remove.add_argument("--fleet", required=True)
    remove.add_argument("--confirm-fleet", required=True)
    assign = sub.add_parser("assign", aliases=["task.assign"])
    assign.add_argument("--fleet", required=True)
    assign.add_argument("--task", required=True)
    assign.add_argument("--agent-ref", required=True)
    assign.add_argument("--manager-ref", required=True)
    assign.add_argument("--command-id")
    transition = sub.add_parser("task-report", aliases=["task.report"])
    transition.add_argument("--fleet", required=True)
    transition.add_argument("--task", required=True)
    transition.add_argument("--status", required=True, choices=sorted(TASK_TRANSITIONS))
    transition.add_argument("--agent-ref", required=True)
    transition.add_argument("--report", type=_json_object)
    transition.add_argument("--operation-id")
    progress = sub.add_parser("progress-report", aliases=["task.progress"])
    progress.add_argument("--fleet", required=True)
    progress.add_argument("--task", required=True)
    progress.add_argument("--agent-ref", required=True)
    progress.add_argument("--report-id", required=True)
    progress.add_argument("--report", type=_json_object, required=True)
    progress.add_argument("--next-report-at", required=True)
    progress_check = sub.add_parser("progress.check")
    progress_check.add_argument("--fleet", required=True)
    progress_check.add_argument("--now")
    accept = sub.add_parser("task.accept")
    accept.add_argument("--fleet", required=True)
    accept.add_argument("--task", required=True)
    accept.add_argument("--manager-ref", required=True)
    accept.add_argument("--operation-id")
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
    context_confirm = sub.add_parser("context.confirm")
    context_confirm.add_argument("--fleet", required=True)
    context_confirm.add_argument("--agent-ref", required=True)
    context_confirm.add_argument("--revision", required=True, type=int)
    context_current = sub.add_parser("context.current")
    context_current.add_argument("--fleet", required=True)
    context_current.add_argument("--agent-ref", required=True)
    context_current.add_argument("--session-id", required=True)
    context_current.add_argument(
        "--runtime-product", required=True, choices=["claude", "codex"]
    )
    context_invalidate = sub.add_parser("context.invalidate")
    context_invalidate.add_argument("--fleet", required=True)
    context_consume = sub.add_parser("context.consume")
    context_consume.add_argument("--fleet", required=True)
    context_consume.add_argument("--command-id", required=True)
    context_consume.add_argument("--activation-token", required=True)
    context_consume.add_argument("--session-id", required=True)
    context_consume.add_argument(
        "--runtime-product", required=True, choices=["claude", "codex"]
    )
    command_consume = sub.add_parser("command.consume")
    command_consume.add_argument("--fleet", required=True)
    command_consume.add_argument("--command-id", required=True)
    command_consume.add_argument("--command-json", type=_json_object, required=True)
    command_consume.add_argument("--session-id", required=True)
    command_consume.add_argument(
        "--runtime-product", required=True, choices=["claude", "codex"]
    )
    command_prepare = sub.add_parser("command.prepare")
    command_prepare.add_argument("--fleet", required=True)
    command_prepare.add_argument("--command-id", required=True)
    command_prepare.add_argument("--command-json", type=_json_object, required=True)
    command_prepare.add_argument("--session-id", required=True)
    command_prepare.add_argument(
        "--runtime-product", required=True, choices=["claude", "codex"]
    )
    claim = sub.add_parser("delivery.claim")
    claim.add_argument("--fleet", required=True)
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--now")
    claim.add_argument("--lease-seconds", type=int, default=30)
    delivery_begin = sub.add_parser("delivery.begin")
    delivery_begin.add_argument("--fleet", required=True)
    delivery_begin.add_argument("--command-id", required=True)
    delivery_begin.add_argument("--lease-token", required=True)
    delivery_begin.add_argument("--now")
    delivery_result = sub.add_parser("delivery.result")
    delivery_result.add_argument("--fleet", required=True)
    delivery_result.add_argument("--command-id", required=True)
    delivery_result.add_argument("--lease-token", required=True)
    delivery_result.add_argument(
        "--result", required=True, choices=["delivered", "unknown", "retry", "abandoned"]
    )
    delivery_result.add_argument("--detail")
    delivery_result.add_argument("--now")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "spec.validate":
            result = load_fleet_config(args.config, role_catalog=args.role_catalog)
            print(json.dumps({"ok": True, "result": result}, sort_keys=True))
            return 0
        store = FleetStore(args.db)
        if args.action in {"init", "fleet.provision"}:
            result = store.initialize(
                load_fleet_config(args.config, role_catalog=args.role_catalog)
            )
        elif args.action in {"status", "fleet.reconcile"}:
            result = store.status(args.fleet)
        elif args.action == "task.list":
            result = store.task_list(args.fleet)
        elif args.action == "fleet.remove":
            result = store.remove_fleet(args.fleet, args.confirm_fleet)
        elif args.action in {"assign", "task.assign"}:
            result = store.assign(
                args.fleet,
                args.task,
                args.agent_ref,
                args.manager_ref,
                args.command_id,
            )
        elif args.action in {"task-report", "task.report"}:
            result = store.transition_task(
                args.fleet,
                args.task,
                args.status,
                args.agent_ref,
                args.report,
                args.operation_id,
            )
        elif args.action in {"progress-report", "task.progress"}:
            result = store.report_progress(
                args.fleet,
                args.task,
                args.agent_ref,
                args.report_id,
                args.report,
                args.next_report_at,
            )
        elif args.action == "progress.check":
            result = store.check_report_deadlines(args.fleet, args.now)
        elif args.action == "task.accept":
            result = store.accept_task(
                args.fleet, args.task, args.manager_ref, args.operation_id
            )
        elif args.action == "context.confirm":
            result = store.confirm_context(args.fleet, args.agent_ref, args.revision)
        elif args.action == "context.current":
            result = store.current_session_context(
                args.fleet, args.agent_ref, args.session_id, args.runtime_product
            )
        elif args.action == "context.invalidate":
            result = store.invalidate_contexts(args.fleet)
        elif args.action == "context.consume":
            result = store.consume_context_activation(
                args.fleet,
                args.command_id,
                args.activation_token,
                args.session_id,
                args.runtime_product,
            )
        elif args.action == "command.consume":
            result = store.consume_command(
                args.fleet,
                args.command_id,
                args.command_json,
                args.session_id,
                args.runtime_product,
            )
        elif args.action == "command.prepare":
            result = store.prepare_command(
                args.fleet,
                args.command_id,
                args.command_json,
                args.session_id,
                args.runtime_product,
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
        elif args.action == "delivery.claim":
            result = store.claim_delivery(
                args.fleet, args.worker_id, args.now, args.lease_seconds
            )
        elif args.action == "delivery.begin":
            result = store.begin_delivery(
                args.fleet, args.command_id, args.lease_token, args.now
            )
        elif args.action == "delivery.result":
            result = store.record_delivery_result(
                args.fleet,
                args.command_id,
                args.lease_token,
                args.result,
                args.detail,
                args.now,
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
