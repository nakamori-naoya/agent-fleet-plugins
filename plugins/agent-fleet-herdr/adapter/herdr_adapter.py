#!/usr/bin/env python3
"""Herdr 0.8 adapter for Agent Fleet.

RuntimeBinding and ViewPlacement are adapter state.  They never leak into the
Core SQLite schema.  Commands are always built as argv and execution is opt-in.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from view_profiles import ViewProfileError, profile_identity, validate_document


COMMAND_TYPES = frozenset(
    {
        "fleet.provision",
        "task.assign",
        "message.send",
        "task.report",
        "fleet.reconcile",
        "context.sync",
    }
)
NEW_PANE_START_ATTEMPTS = 3
NEW_PANE_START_RETRY_DELAY_SECONDS = 1.0
SESSION_HOOK_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "session-hooks-plugin"
CODEX_SESSION_HOOK_PLUGIN_CONFIG = (
    "plugins.agent-fleet-session-hooks@agent-fleet.enabled=true"
)


class HerdrAdapterError(RuntimeError):
    """An adapter error that tells the operator how to recover."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_token(value: str, label: str) -> str:
    if not value or "\x00" in value:
        raise HerdrAdapterError(f"{label} must be a non-empty string without NUL")
    return value


ADAPTER_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runtime_bindings (
    fleet_id TEXT NOT NULL,
    agent_ref TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    tab_id TEXT NOT NULL,
    pane_id TEXT NOT NULL,
    herdr_agent TEXT,
    status TEXT NOT NULL CHECK (status IN ('bound','lost')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (fleet_id, agent_ref)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_bindings_pane ON runtime_bindings(pane_id);
CREATE TABLE IF NOT EXISTS view_placements (
    fleet_id TEXT NOT NULL,
    agent_ref TEXT NOT NULL,
    workspace_slot TEXT NOT NULL,
    tab_slot TEXT NOT NULL,
    pane_slot TEXT NOT NULL,
    profile_ref TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (fleet_id, agent_ref)
);
CREATE TABLE IF NOT EXISTS provisioning_journal (
    fleet_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    tab_id TEXT NOT NULL,
    root_pane_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('workspace_created','complete')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_intents (
    fleet_id TEXT PRIMARY KEY,
    workspace_label TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class RuntimeBinding:
    agent_ref: str
    workspace_id: str
    tab_id: str
    pane_id: str
    herdr_agent: str | None
    status: str
    fleet_id: str = "default"


@dataclass(frozen=True)
class CommandPlan:
    check_argv: tuple[str, ...]
    command_argv: tuple[str, ...]
    agent_ref: str
    pane_id: str
    command_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_ref": self.agent_ref,
            "pane_id": self.pane_id,
            "command_id": self.command_id,
            "check_argv": list(self.check_argv),
            "command_argv": list(self.command_argv),
        }


@dataclass(frozen=True)
class ProvisionPlan:
    fleet_id: str
    profile_ref: str
    manager_ref: str
    worker_refs: tuple[str, ...]
    operations: tuple[Mapping[str, Any], ...]
    placements: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fleet_id": self.fleet_id,
            "profile_ref": self.profile_ref,
            "manager_ref": self.manager_ref,
            "worker_refs": list(self.worker_refs),
            "operations": [dict(operation) for operation in self.operations],
            "placements": [dict(placement) for placement in self.placements],
        }


class AdapterState:
    def __init__(self, db_path: Path | str, *, initialize: bool = True):
        self.db_path = str(db_path)
        self.read_only = not initialize
        if not initialize:
            if self.db_path == ":memory:" or not Path(self.db_path).is_file():
                raise HerdrAdapterError(f"Herdr adapter state database not found: {self.db_path}")
            return
        if self.db_path != ":memory:":
            if Path(self.db_path).is_symlink():
                raise HerdrAdapterError(
                    "Herdr adapter state database must not be a symbolic link"
                )
            parent = Path(self.db_path).parent
            parent_existed = parent.exists()
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not parent_existed:
                parent.chmod(0o700)
        with self.connect() as db:
            db.executescript(ADAPTER_SCHEMA)
            placement_columns = {
                row[1] for row in db.execute("PRAGMA table_info(view_placements)")
            }
            if "profile_ref" not in placement_columns:
                db.execute(
                    "ALTER TABLE view_placements ADD COLUMN profile_ref TEXT NOT NULL "
                    "DEFAULT 'legacy/unversioned@1'"
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            database_uri = f"file:{Path(self.db_path).resolve()}?mode=ro"
            db = sqlite3.connect(database_uri, uri=True)
        else:
            db = sqlite3.connect(self.db_path)
            if self.db_path != ":memory:":
                try:
                    os.chmod(self.db_path, 0o600)
                except OSError:
                    pass
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 5000")
        if not self.read_only:
            db.execute("PRAGMA journal_mode = WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def fleet_lock(self, fleet_id: str) -> Iterator[None]:
        """同じadapter stateに対する一艦隊一操作をプロセス間で保証する。"""

        safe_token(fleet_id, "fleet_id")
        if self.db_path == ":memory:":
            yield
            return
        lock_dir = Path(self.db_path).parent / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_dir.chmod(0o700)
        lock_name = hashlib.sha256(fleet_id.encode("utf-8")).hexdigest() + ".lock"
        lock_path = lock_dir / lock_name
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def bind(
        self,
        agent_ref: str,
        workspace_id: str,
        tab_id: str,
        pane_id: str,
        herdr_agent: str | None = None,
        *,
        fleet_id: str = "default",
    ) -> RuntimeBinding:
        values = [
            safe_token(fleet_id, "fleet_id"),
            safe_token(agent_ref, "agent_ref"),
            safe_token(workspace_id, "workspace_id"),
            safe_token(tab_id, "tab_id"),
            safe_token(pane_id, "pane_id"),
        ]
        if herdr_agent is not None:
            safe_token(herdr_agent, "herdr_agent")
        with self.connect() as db:
            db.execute(
                "INSERT INTO runtime_bindings(fleet_id,agent_ref,workspace_id,tab_id,pane_id,herdr_agent,status,updated_at) "
                "VALUES(?,?,?,?,?,?,'bound',?) ON CONFLICT(fleet_id,agent_ref) DO UPDATE SET "
                "workspace_id=excluded.workspace_id,tab_id=excluded.tab_id,pane_id=excluded.pane_id,"
                "herdr_agent=excluded.herdr_agent,status='bound',updated_at=excluded.updated_at",
                (*values, herdr_agent, utc_now()),
            )
        return self.resolve(agent_ref, fleet_id)

    def resolve(self, agent_ref: str, fleet_id: str = "default") -> RuntimeBinding:
        with self.connect() as db:
            row = db.execute(
                "SELECT agent_ref,workspace_id,tab_id,pane_id,herdr_agent,status,fleet_id "
                "FROM runtime_bindings WHERE fleet_id=? AND agent_ref=?",
                (fleet_id, agent_ref),
            ).fetchone()
        if row is None:
            raise HerdrAdapterError(f"agent_ref {agent_ref!r} is not bound; run bind/rebind")
        binding = RuntimeBinding(**dict(row))
        if binding.status == "lost":
            raise HerdrAdapterError(f"pane for agent_ref {agent_ref!r} is lost; run bind/rebind")
        return binding

    def mark_lost(self, agent_ref: str, fleet_id: str = "default") -> None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runtime_bindings SET status='lost',updated_at=? "
                "WHERE fleet_id=? AND agent_ref=?",
                (utc_now(), fleet_id, agent_ref),
            ).rowcount
        if changed == 0:
            raise HerdrAdapterError(f"agent_ref {agent_ref!r} is not bound")

    def place_view(
        self,
        agent_ref: str,
        workspace_slot: str,
        tab_slot: str,
        pane_slot: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        fleet_id: str = "default",
        profile_ref: str,
    ) -> dict[str, Any]:
        values = (
            safe_token(fleet_id, "fleet_id"),
            safe_token(agent_ref, "agent_ref"),
            safe_token(workspace_slot, "workspace_slot"),
            safe_token(tab_slot, "tab_slot"),
            safe_token(pane_slot, "pane_slot"),
            safe_token(profile_ref, "profile_ref"),
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO view_placements(fleet_id,agent_ref,workspace_slot,tab_slot,pane_slot,profile_ref,metadata_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(fleet_id,agent_ref) DO UPDATE SET "
                "workspace_slot=excluded.workspace_slot,tab_slot=excluded.tab_slot,"
                "pane_slot=excluded.pane_slot,profile_ref=excluded.profile_ref,"
                "metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                (*values, json.dumps(metadata or {}, sort_keys=True), utc_now()),
            )
        return {
            "fleet_id": fleet_id,
            "agent_ref": agent_ref,
            "workspace_slot": workspace_slot,
            "tab_slot": tab_slot,
            "pane_slot": pane_slot,
            "profile_ref": profile_ref,
        }

    def status(self, fleet_id: str) -> dict[str, Any]:
        """Return adapter-owned observed state without probing Herdr or mutating SQLite."""

        safe_token(fleet_id, "fleet_id")
        with self.connect() as db:
            bindings = [
                dict(row)
                for row in db.execute(
                    "SELECT fleet_id,agent_ref,workspace_id,tab_id,pane_id,herdr_agent,status,updated_at "
                    "FROM runtime_bindings WHERE fleet_id=? ORDER BY agent_ref",
                    (fleet_id,),
                )
            ]
            placements = []
            for row in db.execute(
                "SELECT fleet_id,agent_ref,workspace_slot,tab_slot,pane_slot,profile_ref,"
                "metadata_json,updated_at FROM view_placements WHERE fleet_id=? ORDER BY agent_ref",
                (fleet_id,),
            ):
                placement = dict(row)
                placement["metadata"] = json.loads(placement.pop("metadata_json"))
                placements.append(placement)
        profile_refs = sorted({item["profile_ref"] for item in placements})
        return {
            "fleet_id": fleet_id,
            "profile_ref": profile_refs[0] if len(profile_refs) == 1 else None,
            "bindings": bindings,
            "placements": placements,
        }

    def save_provision(
        self,
        bindings: Sequence[RuntimeBinding],
        placements: Sequence[Mapping[str, Any]],
    ) -> None:
        """Persist a completed provision atomically after all Herdr calls succeed."""

        now = utc_now()
        with self.connect() as db:
            for binding in bindings:
                db.execute(
                    "INSERT INTO runtime_bindings(fleet_id,agent_ref,workspace_id,tab_id,pane_id,herdr_agent,status,updated_at) "
                    "VALUES(?,?,?,?,?,?,'bound',?) ON CONFLICT(fleet_id,agent_ref) DO UPDATE SET "
                    "workspace_id=excluded.workspace_id,tab_id=excluded.tab_id,pane_id=excluded.pane_id,"
                    "herdr_agent=excluded.herdr_agent,status='bound',updated_at=excluded.updated_at",
                    (
                        binding.fleet_id,
                        binding.agent_ref,
                        binding.workspace_id,
                        binding.tab_id,
                        binding.pane_id,
                        binding.herdr_agent,
                        now,
                    ),
                )
            for placement in placements:
                db.execute(
                    "INSERT INTO view_placements(fleet_id,agent_ref,workspace_slot,tab_slot,pane_slot,profile_ref,metadata_json,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(fleet_id,agent_ref) DO UPDATE SET "
                    "workspace_slot=excluded.workspace_slot,tab_slot=excluded.tab_slot,"
                    "pane_slot=excluded.pane_slot,profile_ref=excluded.profile_ref,"
                    "metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (
                        bindings[0].fleet_id,
                        placement["agent_ref"],
                        placement["workspace_slot"],
                        placement["tab_slot"],
                        placement["pane_slot"],
                        placement["profile_ref"],
                        json.dumps(placement.get("metadata", {}), sort_keys=True),
                        now,
                    ),
                )
            db.execute(
                "UPDATE provisioning_journal SET state='complete',updated_at=? "
                "WHERE fleet_id=?",
                (now, bindings[0].fleet_id),
            )

    def save_workspace_journal(
        self,
        fleet_id: str,
        workspace_id: str,
        tab_id: str,
        root_pane_id: str,
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO provisioning_journal(fleet_id,workspace_id,tab_id,root_pane_id,"
                "state,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(fleet_id) DO UPDATE SET workspace_id=excluded.workspace_id,"
                "tab_id=excluded.tab_id,root_pane_id=excluded.root_pane_id,"
                "state=excluded.state,updated_at=excluded.updated_at",
                (
                    fleet_id,
                    workspace_id,
                    tab_id,
                    root_pane_id,
                    "workspace_created",
                    utc_now(),
                ),
            )
            db.execute("DELETE FROM workspace_intents WHERE fleet_id=?", (fleet_id,))

    def save_workspace_intent(self, fleet_id: str, workspace_label: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO workspace_intents(fleet_id,workspace_label,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(fleet_id) DO UPDATE SET "
                "workspace_label=excluded.workspace_label,updated_at=excluded.updated_at",
                (
                    safe_token(fleet_id, "fleet_id"),
                    safe_token(workspace_label, "workspace_label"),
                    utc_now(),
                ),
            )

    def provisioning_intent(self, fleet_id: str) -> dict[str, Any] | None:
        try:
            with self.connect() as db:
                row = db.execute(
                    "SELECT fleet_id,workspace_label,updated_at FROM workspace_intents "
                    "WHERE fleet_id=?",
                    (fleet_id,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return None
        return dict(row) if row is not None else None

    def clear_workspace_intent(self, fleet_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM workspace_intents WHERE fleet_id=?", (fleet_id,))

    def provisioning_journal(self, fleet_id: str) -> dict[str, Any] | None:
        try:
            with self.connect() as db:
                row = db.execute(
                    "SELECT fleet_id,workspace_id,tab_id,root_pane_id,state,updated_at "
                    "FROM provisioning_journal WHERE fleet_id=?",
                    (fleet_id,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return None
        return dict(row) if row is not None else None

    def clear_fleet(self, fleet_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM runtime_bindings WHERE fleet_id=?", (fleet_id,))
            db.execute("DELETE FROM view_placements WHERE fleet_id=?", (fleet_id,))
            db.execute("DELETE FROM provisioning_journal WHERE fleet_id=?", (fleet_id,))
            db.execute("DELETE FROM workspace_intents WHERE fleet_id=?", (fleet_id,))


class Herdr08Commands:
    """Safe argv builders for the Herdr 0.8 CLI surface."""

    def __init__(self, binary: str = "herdr"):
        self.binary = safe_token(binary, "Herdr binary")

    @staticmethod
    def _append_environment(argv: list[str], environment: Mapping[str, str]) -> None:
        allowed = {
            "AGENT_FLEET_CODEX_HOOK_TRUST",
            "AGENT_FLEET_CORE_COMMAND",
            "AGENT_FLEET_CORE_DB",
            "AGENT_FLEET_HOOK_RUNTIME",
        }
        unknown = set(environment) - allowed
        if unknown:
            raise HerdrAdapterError(
                "unsupported agent environment: " + ", ".join(sorted(unknown))
            )
        for key in sorted(environment):
            argv.extend(
                ["--env", safe_token(f"{key}={environment[key]}", "agent environment")]
            )

    def workspace_create(
        self, cwd: str, label: str, environment: Mapping[str, str] | None = None
    ) -> list[str]:
        argv = [
            self.binary,
            "workspace",
            "create",
            "--cwd",
            safe_token(cwd, "cwd"),
            "--label",
            safe_token(label, "label"),
        ]
        self._append_environment(argv, environment or {})
        argv.append("--no-focus")
        return argv

    def workspace_close(self, workspace_id: str) -> list[str]:
        return [
            self.binary,
            "workspace",
            "close",
            safe_token(workspace_id, "workspace_id"),
        ]

    def workspace_list(self) -> list[str]:
        return [self.binary, "workspace", "list"]

    def tab_create(self, workspace_id: str, cwd: str, label: str) -> list[str]:
        return [
            self.binary,
            "tab",
            "create",
            "--workspace",
            safe_token(workspace_id, "workspace_id"),
            "--cwd",
            safe_token(cwd, "cwd"),
            "--label",
            safe_token(label, "label"),
            "--no-focus",
        ]

    def pane_split(
        self,
        pane_id: str,
        direction: str,
        cwd: str,
        ratio: float | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> list[str]:
        if direction not in {"right", "down"}:
            raise HerdrAdapterError("pane split direction must be right or down")
        argv = [
            self.binary,
            "pane",
            "split",
            safe_token(pane_id, "pane_id"),
            "--direction",
            direction,
            "--cwd",
            safe_token(cwd, "cwd"),
        ]
        if ratio is not None:
            if not 0 < ratio < 1:
                raise HerdrAdapterError("pane split ratio must be between zero and one")
            argv.extend(["--ratio", str(ratio)])
        self._append_environment(argv, environment or {})
        argv.append("--no-focus")
        return argv

    def agent_start(self, name: str, kind: str, pane_id: str, agent_args: Sequence[str] = ()) -> list[str]:
        argv = [
            self.binary,
            "agent",
            "start",
            safe_token(name, "agent name"),
            "--kind",
            safe_token(kind, "agent kind"),
            "--pane",
            safe_token(pane_id, "pane_id"),
        ]
        if agent_args:
            argv.append("--")
            argv.extend(safe_token(arg, "agent arg") for arg in agent_args)
        return argv

    def pane_get(self, pane_id: str) -> list[str]:
        return [self.binary, "pane", "get", safe_token(pane_id, "pane_id")]

    def agent_prompt(
        self,
        pane_id: str,
        prompt: str,
        timeout_ms: int,
        *,
        wait: bool = True,
        until_started: bool = False,
    ) -> list[str]:
        if timeout_ms <= 0:
            raise HerdrAdapterError("timeout_ms must be positive")
        argv = [
            self.binary,
            "agent",
            "prompt",
            safe_token(pane_id, "pane_id"),
            safe_token(prompt, "prompt"),
        ]
        if wait:
            argv.append("--wait")
            if until_started:
                argv.extend(
                    [
                        "--until",
                        "working",
                        "--until",
                        "done",
                        "--until",
                        "blocked",
                    ]
                )
            argv.extend(["--timeout", str(timeout_ms)])
        return argv


Runner = Callable[..., subprocess.CompletedProcess[str]]


class HerdrAdapter:
    def __init__(
        self,
        state: AdapterState,
        commands: Herdr08Commands | None = None,
        runner: Runner = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.state = state
        self.commands = commands or Herdr08Commands()
        self.runner = runner
        self.sleeper = sleeper

    @staticmethod
    def _fleet_parts(
        fleet: Mapping[str, Any]
    ) -> tuple[str, str, tuple[str, ...], str, Mapping[str, str]]:
        if fleet.get("apiVersion") != "fleet.harness/v1" or fleet.get("kind") != "Fleet":
            raise HerdrAdapterError("provision requires a fleet.harness/v1 Fleet document")
        metadata = fleet.get("metadata")
        fleet_spec = fleet.get("spec")
        if not isinstance(metadata, Mapping) or not isinstance(fleet_spec, Mapping):
            raise HerdrAdapterError("Fleet metadata and spec must be JSON objects")
        fleet_id = metadata.get("id")
        members = fleet_spec.get("members")
        collaboration = fleet_spec.get("collaboration")
        runtime = fleet_spec.get("runtime")
        view = fleet_spec.get("view")
        if not isinstance(fleet_id, str) or not fleet_id:
            raise HerdrAdapterError("Fleet metadata.id must be a non-empty string")
        if not isinstance(members, list):
            raise HerdrAdapterError("Fleet spec.members must be a list")
        if not isinstance(collaboration, Mapping):
            raise HerdrAdapterError("Fleet spec.collaboration must be a JSON object")
        if not isinstance(runtime, Mapping) or runtime.get("provider") != "herdr":
            raise HerdrAdapterError("Fleet spec.runtime.provider must be herdr")
        profile_ref = view.get("profile_ref") if isinstance(view, Mapping) else None
        if not isinstance(profile_ref, str) or not profile_ref:
            raise HerdrAdapterError("Fleet spec.view.profile_ref must be versioned")
        manager_ref = collaboration.get("manager")
        if not isinstance(manager_ref, str) or not manager_ref:
            raise HerdrAdapterError("Fleet spec.collaboration.manager must be an agent_ref")

        refs: list[str] = []
        models: dict[str, str] = {}
        for index, member in enumerate(members):
            if not isinstance(member, Mapping):
                raise HerdrAdapterError(f"Fleet spec.members[{index}] must be a JSON object")
            agent_ref = member.get("agent_ref")
            role_ref = member.get("role_ref")
            if not isinstance(agent_ref, str) or not agent_ref:
                raise HerdrAdapterError(f"Fleet spec.members[{index}].agent_ref is required")
            if not isinstance(role_ref, str) or not role_ref:
                raise HerdrAdapterError(f"Fleet spec.members[{index}].role_ref is required")
            if agent_ref in refs:
                raise HerdrAdapterError(f"duplicate Fleet member agent_ref: {agent_ref}")
            refs.append(agent_ref)
            model = member.get("model")
            if model is not None:
                if not isinstance(model, str) or not model.strip():
                    raise HerdrAdapterError(
                        f"Fleet spec.members[{index}].model must be a non-empty string"
                    )
                models[agent_ref] = model
        if refs.count(manager_ref) != 1:
            raise HerdrAdapterError("Fleet must contain exactly one declared manager member")
        workers = tuple(agent_ref for agent_ref in refs if agent_ref != manager_ref)
        return fleet_id, manager_ref, workers, profile_ref, models

    def plan_provision(
        self,
        fleet: Mapping[str, Any],
        cwd: str,
        agent_kind: str,
        view_profile: Mapping[str, Any],
        agent_environment: Mapping[str, str] | None = None,
    ) -> ProvisionPlan:
        fleet_id, manager_ref, workers, profile_ref, models = self._fleet_parts(fleet)
        if models and agent_kind not in {"claude", "codex"}:
            raise HerdrAdapterError(
                f"per-member model selection is not supported for agent kind {agent_kind!r}"
            )

        spec = fleet.get("spec")
        runtime = spec.get("runtime") if isinstance(spec, Mapping) else None
        hook_trust = (
            runtime.get("codex_hook_trust", "review")
            if isinstance(runtime, Mapping)
            else "review"
        )
        if hook_trust not in {"preapproved", "review"}:
            raise HerdrAdapterError(
                "Fleet spec.runtime.codex_hook_trust must be preapproved or review"
            )
        provision_environment = dict(agent_environment or {})
        if agent_kind == "codex":
            provision_environment["AGENT_FLEET_CODEX_HOOK_TRUST"] = hook_trust

        def session_args(agent_ref: str) -> tuple[str, ...]:
            args: list[str] = []
            if agent_kind == "codex":
                args.extend(["--config", CODEX_SESSION_HOOK_PLUGIN_CONFIG])
                if hook_trust == "preapproved":
                    args.append("--dangerously-bypass-hook-trust")
            elif agent_kind == "claude":
                args.extend(["--plugin-dir", str(SESSION_HOOK_PLUGIN_ROOT)])
            model = models.get(agent_ref)
            if model:
                args.extend(["--model", model])
            return tuple(args)
        profile_errors = validate_document(view_profile)
        if profile_errors:
            raise HerdrAdapterError("invalid View Profile: " + "; ".join(profile_errors))
        try:
            resolved_profile_ref = profile_identity(view_profile)
        except ViewProfileError as exc:
            raise HerdrAdapterError(str(exc)) from exc
        if resolved_profile_ref != profile_ref:
            raise HerdrAdapterError(
                f"View Profile {resolved_profile_ref!r} does not match Fleet reference {profile_ref!r}"
            )
        profile_spec = view_profile["spec"]
        constraints = profile_spec["constraints"]
        member_count = len(workers) + 1
        if not constraints["min_members"] <= member_count <= constraints["max_members"]:
            raise HerdrAdapterError(
                f"View Profile {profile_ref!r} does not support {member_count} members"
            )
        if not workers:
            raise HerdrAdapterError("View Profile non-manager stack requires at least one member")
        layout = profile_spec["layout"]
        manager_slot, member_stack = layout["children"]
        split_direction = {"horizontal": "right", "vertical": "down"}
        total_weight = manager_slot["weight"] + member_stack["weight"]
        member_fraction = member_stack["weight"] / total_weight
        safe_token(cwd, "cwd")
        safe_token(agent_kind, "agent_kind")
        root_pane = "$workspace.root_pane"
        workspace_label = f"agent-fleet:{fleet_id}:<operation-id>"
        operations: list[Mapping[str, Any]] = [
            {
                "id": "workspace.create",
                "argv": self.commands.workspace_create(
                    cwd, workspace_label, provision_environment
                ),
                "produces": ["workspace_id", "tab_id", "root_pane_id"],
            },
            {
                "id": f"agent.start:{manager_ref}",
                "argv": self.commands.agent_start(
                    manager_ref, agent_kind, root_pane, session_args(manager_ref)
                ),
            },
        ]
        placements: list[Mapping[str, Any]] = [
            {
                "agent_ref": manager_ref,
                "workspace_slot": fleet_id,
                "tab_slot": profile_ref,
                "pane_slot": manager_slot.get("pane_slot", "manager"),
                "profile_ref": profile_ref,
                "metadata": {
                    "profile_ref": profile_ref,
                    "fraction": manager_slot["weight"] / total_weight,
                    "order": 0,
                },
            }
        ]
        split_target = root_pane
        for index, worker_ref in enumerate(workers, 1):
            pane_ref = f"$pane:{worker_ref}"
            operations.append(
                {
                    "id": f"pane.split:{worker_ref}",
                    "argv": self.commands.pane_split(
                        split_target,
                        split_direction[
                            layout["direction"]
                            if index == 1
                            else member_stack["direction"]
                        ],
                        cwd,
                        (
                            member_fraction
                            if index == 1
                            else (len(workers) - index + 1) / (len(workers) - index + 2)
                        ),
                        provision_environment,
                    ),
                    "produces": [pane_ref],
                }
            )
            operations.append(
                {
                    "id": f"agent.start:{worker_ref}",
                    "argv": self.commands.agent_start(
                        worker_ref, agent_kind, pane_ref, session_args(worker_ref)
                    ),
                }
            )
            placements.append(
                {
                    "agent_ref": worker_ref,
                    "workspace_slot": fleet_id,
                    "tab_slot": profile_ref,
                    "pane_slot": f"{member_stack.get('pane_slot_prefix', 'members')}.{index}",
                    "profile_ref": profile_ref,
                    "metadata": {"profile_ref": profile_ref, "order": index},
                }
            )
            split_target = pane_ref
        return ProvisionPlan(
            fleet_id,
            profile_ref,
            manager_ref,
            workers,
            tuple(operations),
            tuple(placements),
        )

    @staticmethod
    def _json_value(stdout: str, context: str) -> Any:
        text = stdout.strip()
        if not text:
            raise HerdrAdapterError(f"{context} returned empty output; bindings were not saved")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if "\n" not in text and "\x00" not in text:
                return text
            raise HerdrAdapterError(
                f"{context} returned unrecognized output; bindings were not saved"
            )

    @staticmethod
    def _nested(value: Any, *paths: Sequence[str]) -> str | None:
        for path in paths:
            current = value
            for key in path:
                if not isinstance(current, Mapping) or key not in current:
                    break
                current = current[key]
            else:
                if isinstance(current, str) and current:
                    return current
        return None

    def _workspace_ids(self, stdout: str) -> tuple[str, str, str]:
        value = self._json_value(stdout, "herdr workspace create")
        workspace_id = self._nested(
            value,
            ("result", "workspace", "workspace_id"),
            ("workspace", "workspace_id"),
            ("workspace_id",),
        )
        tab_id = self._nested(
            value,
            ("result", "tab", "tab_id"),
            ("tab", "tab_id"),
            ("tab_id",),
        )
        pane_id = self._nested(
            value,
            ("result", "root_pane", "pane_id"),
            ("root_pane", "pane_id"),
            ("root_pane_id",),
            ("pane_id",),
        )
        if not workspace_id or not tab_id or not pane_id:
            raise HerdrAdapterError(
                "herdr workspace create output did not contain workspace/tab/root pane IDs; "
                "bindings were not saved"
            )
        return (
            safe_token(workspace_id, "workspace_id"),
            safe_token(tab_id, "tab_id"),
            safe_token(pane_id, "root pane_id"),
        )

    def _workspace_ids_for_label(self, stdout: str, label: str) -> list[str]:
        value = self._json_value(stdout, "herdr workspace list")
        workspaces = value
        if isinstance(value, Mapping):
            result = value.get("result")
            if isinstance(result, Mapping):
                workspaces = result.get("workspaces")
            else:
                workspaces = value.get("workspaces")
        if not isinstance(workspaces, list):
            raise HerdrAdapterError(
                "herdr workspace list output did not contain a workspace list"
            )
        matches: list[str] = []
        for workspace in workspaces:
            if not isinstance(workspace, Mapping) or workspace.get("label") != label:
                continue
            workspace_id = workspace.get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id:
                raise HerdrAdapterError(
                    "matching Herdr workspace did not contain a workspace_id"
                )
            matches.append(safe_token(workspace_id, "workspace_id"))
        return matches

    def _recover_workspace_intent(self, fleet_id: str, label: str) -> str | None:
        listed = self._execute_argv(
            self.commands.workspace_list(), "list Herdr workspaces for recovery"
        )
        workspace_ids = self._workspace_ids_for_label(listed.stdout, label)
        if len(workspace_ids) > 1:
            raise HerdrAdapterError(
                f"multiple Herdr workspaces use recovery label {label!r}; "
                "manual recovery required"
            )
        if workspace_ids:
            self._execute_argv(
                self.commands.workspace_close(workspace_ids[0]),
                "close unrecorded Herdr workspace",
            )
        self.state.clear_workspace_intent(fleet_id)
        return workspace_ids[0] if workspace_ids else None

    def _split_pane_id(self, stdout: str) -> str:
        value = self._json_value(stdout, "herdr pane split")
        if isinstance(value, str):
            return safe_token(value, "pane split ID")
        pane_id = self._nested(
            value,
            ("result", "pane", "pane_id"),
            ("pane", "pane_id"),
            ("result", "pane_id"),
            ("pane_id",),
        )
        if not pane_id:
            raise HerdrAdapterError(
                "herdr pane split output did not contain a pane ID; bindings were not saved"
            )
        return safe_token(pane_id, "pane split ID")

    def _execute_argv(
        self,
        argv: Sequence[str],
        context: str,
        *,
        retry_new_pane_busy: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        attempts = NEW_PANE_START_ATTEMPTS if retry_new_pane_busy else 1
        for attempt in range(attempts):
            try:
                completed = self.runner(
                    list(argv), capture_output=True, text=True, timeout=30
                )
            except subprocess.TimeoutExpired as exc:
                raise HerdrAdapterError(
                    f"{context} timed out; bindings were not saved"
                ) from exc
            if completed.returncode == 0:
                return completed
            if (
                "agent_pane_busy" not in completed.stderr.lower()
                or attempt == attempts - 1
            ):
                break
            self.sleeper(NEW_PANE_START_RETRY_DELAY_SECONDS)
        raise HerdrAdapterError(
            f"{context} failed: {completed.stderr.strip() or 'unknown Herdr error'}; "
            "bindings were not saved"
        )

    @staticmethod
    def _resolve_argv(argv: Sequence[str], pane_ids: Mapping[str, str]) -> list[str]:
        resolved: list[str] = []
        for token in argv:
            if token.startswith("$"):
                if token not in pane_ids:
                    raise HerdrAdapterError(f"unresolved provision plan token: {token}")
                resolved.append(pane_ids[token])
            else:
                resolved.append(token)
        return resolved

    def provision(
        self,
        fleet: Mapping[str, Any],
        cwd: str,
        agent_kind: str,
        view_profile: Mapping[str, Any],
        *,
        execute: bool = False,
        agent_environment: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        plan = self.plan_provision(
            fleet, cwd, agent_kind, view_profile, agent_environment
        )
        with self.state.fleet_lock(plan.fleet_id):
            return self._provision_locked(plan, execute=execute)

    def _provision_locked(
        self, plan: ProvisionPlan, *, execute: bool
    ) -> dict[str, Any]:
        interrupted = (
            self.state.provisioning_journal(plan.fleet_id)
            if self.state.db_path != ":memory:"
            else None
        )
        if interrupted is not None and interrupted["state"] == "workspace_created":
            if not execute:
                raise HerdrAdapterError(
                    f"fleet {plan.fleet_id!r} has an interrupted provision; "
                    "run provision --execute to recover"
                )
            self._execute_argv(
                self.commands.workspace_close(interrupted["workspace_id"]),
                "close interrupted Herdr workspace",
            )
            self.state.clear_fleet(plan.fleet_id)
        intent = (
            self.state.provisioning_intent(plan.fleet_id)
            if self.state.db_path != ":memory:"
            else None
        )
        if intent is not None:
            if not execute:
                raise HerdrAdapterError(
                    f"fleet {plan.fleet_id!r} has an interrupted workspace creation; "
                    "run provision --execute to recover"
                )
            self._recover_workspace_intent(
                plan.fleet_id, intent["workspace_label"]
            )
        observed = (
            self.state.status(plan.fleet_id)
            if self.state.db_path != ":memory:"
            else {"bindings": [], "placements": [], "profile_ref": None}
        )
        if observed["bindings"] or observed["placements"]:
            expected_refs = {plan.manager_ref, *plan.worker_refs}
            bound_refs = {item["agent_ref"] for item in observed["bindings"]}
            placed_refs = {item["agent_ref"] for item in observed["placements"]}
            observed_profile_ref = observed["profile_ref"]
            if observed_profile_ref not in {None, plan.profile_ref}:
                raise HerdrAdapterError(
                    f"View Profile conflict for fleet {plan.fleet_id!r}: "
                    f"observed {observed_profile_ref!r}, requested {plan.profile_ref!r}"
                )
            if (
                observed_profile_ref == plan.profile_ref
                and bound_refs == expected_refs
                and placed_refs == expected_refs
                and all(item["status"] == "bound" for item in observed["bindings"])
            ):
                return {
                    "mode": "execute" if execute else "dry-run",
                    "status": "already_provisioned",
                    "fleet_id": plan.fleet_id,
                    "profile_ref": plan.profile_ref,
                    "bindings": observed["bindings"],
                    "placements": observed["placements"],
                    "plan": plan.as_dict(),
                }
            raise HerdrAdapterError(
                f"existing incomplete or incompatible placement for fleet {plan.fleet_id!r}; "
                "reconcile before provisioning"
            )
        if not execute:
            return {"mode": "dry-run", "status": "planned", "plan": plan.as_dict()}

        workspace_op = plan.operations[0]
        workspace_label = f"agent-fleet:{plan.fleet_id}:{uuid.uuid4().hex}"
        self.state.save_workspace_intent(plan.fleet_id, workspace_label)
        workspace_argv = list(workspace_op["argv"])
        workspace_argv[workspace_argv.index("--label") + 1] = workspace_label
        workspace_result = self._execute_argv(
            workspace_argv, "herdr workspace create"
        )
        workspace_id, tab_id, root_pane_id = self._workspace_ids(workspace_result.stdout)
        self.state.save_workspace_journal(
            plan.fleet_id, workspace_id, tab_id, root_pane_id
        )
        pane_ids: dict[str, str] = {"$workspace.root_pane": root_pane_id}
        binding_panes: dict[str, str] = {plan.manager_ref: root_pane_id}

        for operation in plan.operations[1:]:
            operation_id = str(operation["id"])
            argv = self._resolve_argv(operation["argv"], pane_ids)
            completed = self._execute_argv(
                argv,
                f"herdr {operation_id}",
                retry_new_pane_busy=operation_id.startswith("agent.start:"),
            )
            if operation_id.startswith("pane.split:"):
                worker_ref = operation_id.split(":", 1)[1]
                pane_id = self._split_pane_id(completed.stdout)
                pane_ids[f"$pane:{worker_ref}"] = pane_id
                binding_panes[worker_ref] = pane_id

        ordered_refs = (plan.manager_ref, *plan.worker_refs)
        bindings = [
            RuntimeBinding(
                agent_ref,
                workspace_id,
                tab_id,
                binding_panes[agent_ref],
                agent_ref,
                "bound",
                plan.fleet_id,
            )
            for agent_ref in ordered_refs
        ]
        self.state.save_provision(bindings, plan.placements)
        return {
            "mode": "execute",
            "status": "provisioned",
            "fleet_id": plan.fleet_id,
            "workspace_id": workspace_id,
            "tab_id": tab_id,
            "bindings": [binding.__dict__ for binding in bindings],
            "plan": plan.as_dict(),
        }

    def deprovision(self, fleet_id: str, *, execute: bool = False) -> dict[str, Any]:
        safe_token(fleet_id, "fleet_id")
        with self.state.fleet_lock(fleet_id):
            return self._deprovision_locked(fleet_id, execute=execute)

    def _deprovision_locked(
        self, fleet_id: str, *, execute: bool
    ) -> dict[str, Any]:
        observed = self.state.status(fleet_id)
        journal = self.state.provisioning_journal(fleet_id)
        intent = self.state.provisioning_intent(fleet_id)
        workspace_ids = {
            item["workspace_id"] for item in observed["bindings"]
        }
        if journal is not None:
            workspace_ids.add(journal["workspace_id"])
        if not workspace_ids and intent is not None:
            if not execute:
                return {
                    "mode": "dry-run",
                    "status": "planned",
                    "fleet_id": fleet_id,
                    "workspace_label": intent["workspace_label"],
                }
            recovered_workspace_id = self._recover_workspace_intent(
                fleet_id, intent["workspace_label"]
            )
            self.state.clear_fleet(fleet_id)
            return {
                "mode": "execute",
                "status": "deprovisioned",
                "fleet_id": fleet_id,
                "workspace_id": recovered_workspace_id,
            }
        if not workspace_ids:
            return {
                "mode": "execute" if execute else "dry-run",
                "status": "inactive",
                "fleet_id": fleet_id,
            }
        if len(workspace_ids) != 1:
            raise HerdrAdapterError(
                f"fleet {fleet_id!r} references multiple workspaces; manual recovery required"
            )
        workspace_id = next(iter(workspace_ids))
        if not execute:
            return {
                "mode": "dry-run",
                "status": "planned",
                "fleet_id": fleet_id,
                "workspace_id": workspace_id,
            }
        try:
            self._execute_argv(
                self.commands.workspace_close(workspace_id), "Herdr workspace close"
            )
        except HerdrAdapterError as exc:
            if "workspace_not_found" not in str(exc):
                raise
        self.state.clear_fleet(fleet_id)
        return {
            "mode": "execute",
            "status": "deprovisioned",
            "fleet_id": fleet_id,
            "workspace_id": workspace_id,
        }

    def plan_command(
        self,
        agent_ref: str,
        command_id: str,
        command_type: str,
        payload: Mapping[str, Any],
        timeout_ms: int = 30_000,
        request_envelope: Mapping[str, Any] | None = None,
        fleet_id: str = "default",
        wait_for_response: bool = True,
        wait_until_started: bool = False,
    ) -> CommandPlan:
        if command_type not in COMMAND_TYPES:
            raise HerdrAdapterError(f"unsupported command type: {command_type}")
        binding = self.state.resolve(agent_ref, fleet_id)
        envelope = json.dumps(
            request_envelope
            or {"command_id": command_id, "type": command_type, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt = f"AGENT_FLEET_COMMAND_V1\n{envelope}"
        return CommandPlan(
            tuple(self.commands.pane_get(binding.pane_id)),
            tuple(
                self.commands.agent_prompt(
                    binding.pane_id,
                    prompt,
                    timeout_ms,
                    wait=wait_for_response,
                    until_started=wait_until_started,
                )
            ),
            agent_ref,
            binding.pane_id,
            command_id,
        )

    def dispatch(
        self,
        agent_ref: str,
        command_id: str,
        command_type: str,
        payload: Mapping[str, Any],
        *,
        timeout_ms: int = 30_000,
        execute: bool = False,
        request_envelope: Mapping[str, Any] | None = None,
        fleet_id: str = "default",
        wait_for_response: bool = True,
        wait_until_started: bool = False,
    ) -> dict[str, Any]:
        plan = self.plan_command(
            agent_ref,
            command_id,
            command_type,
            payload,
            timeout_ms,
            request_envelope,
            fleet_id,
            wait_for_response,
            wait_until_started,
        )
        if not execute:
            return {"mode": "dry-run", "status": "planned", "plan": plan.as_dict()}

        try:
            check = self.runner(
                list(plan.check_argv), capture_output=True, text=True, timeout=10
            )
        except subprocess.TimeoutExpired as exc:
            raise HerdrAdapterError("pane presence check timed out; binding was not changed") from exc
        if check.returncode != 0:
            diagnostic = check.stderr.strip() or "pane not found"
            missing = any(
                marker in diagnostic.lower()
                for marker in ("not found", "not_found", "unknown pane", "no such pane")
            )
            if missing:
                self.state.mark_lost(agent_ref, fleet_id)
                raise HerdrAdapterError(
                    f"pane {plan.pane_id!r} for agent_ref {agent_ref!r} is unavailable: "
                    f"{diagnostic}; run bind/rebind"
                )
            raise HerdrAdapterError(
                f"pane presence check failed and binding was left unchanged: {diagnostic}"
            )

        # A timed-out prompt has unknown delivery/completion state. Never retry it.
        try:
            completed = self.runner(
                list(plan.command_argv),
                capture_output=True,
                text=True,
                timeout=max(10, timeout_ms / 1000 + 5) if wait_for_response else 10,
            )
        except subprocess.TimeoutExpired:
            return {
                "mode": "execute",
                "status": "unknown",
                "reason": "prompt timeout; command was not retried",
                "attempts": 1,
                "plan": plan.as_dict(),
            }
        if completed.returncode != 0 and any(
            marker in completed.stderr.lower()
            for marker in ("timeout", "timed out", "deadline exceeded")
        ):
            return {
                "mode": "execute",
                "status": "unknown",
                "reason": completed.stderr.strip() or "prompt timeout; command was not retried",
                "attempts": 1,
                "plan": plan.as_dict(),
            }
        if completed.returncode != 0:
            raise HerdrAdapterError(completed.stderr.strip() or "Herdr prompt failed")
        return {
            "mode": "execute",
            "status": "submitted",
            "attempts": 1,
            "stdout": completed.stdout,
            "plan": plan.as_dict(),
        }


def json_object(value: str) -> Mapping[str, Any]:
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-herdr")
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--herdr-binary", default="herdr")
    sub = parser.add_subparsers(dest="action", required=True)
    provision = sub.add_parser("provision")
    provision.add_argument("--fleet-json", type=json_object, required=True)
    provision.add_argument("--view-profile-json", type=json_object, required=True)
    provision.add_argument("--cwd", required=True)
    provision.add_argument("--agent-kind", required=True)
    provision.add_argument("--agent-core-command")
    provision.add_argument("--agent-core-db")
    provision.add_argument("--agent-hook-runtime")
    provision.add_argument("--execute", action="store_true")
    deprovision = sub.add_parser("deprovision")
    deprovision.add_argument("--fleet", required=True)
    deprovision.add_argument("--execute", action="store_true")
    bind = sub.add_parser("bind", aliases=["rebind"])
    bind.add_argument("--agent-ref", required=True)
    bind.add_argument("--fleet", default="default")
    bind.add_argument("--workspace", required=True)
    bind.add_argument("--tab", required=True)
    bind.add_argument("--pane", required=True)
    bind.add_argument("--herdr-agent")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--agent-ref", required=True)
    resolve.add_argument("--fleet", default="default")
    lost = sub.add_parser("mark-lost")
    lost.add_argument("--agent-ref", required=True)
    lost.add_argument("--fleet", default="default")
    place = sub.add_parser("view-place")
    place.add_argument("--agent-ref", required=True)
    place.add_argument("--fleet", default="default")
    place.add_argument("--workspace-slot", required=True)
    place.add_argument("--tab-slot", required=True)
    place.add_argument("--pane-slot", required=True)
    place.add_argument("--profile-ref", required=True)
    place.add_argument("--metadata", type=json_object, default={})
    status = sub.add_parser("status")
    status.add_argument("--fleet", required=True)
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("--request-json", type=json_object)
    dispatch.add_argument("--fleet")
    dispatch.add_argument("--agent-ref")
    dispatch.add_argument("--command-id")
    dispatch.add_argument("--type", choices=sorted(COMMAND_TYPES))
    dispatch.add_argument("--payload", type=json_object)
    dispatch.add_argument("--timeout-ms", type=int, default=30_000)
    dispatch.add_argument("--execute", action="store_true")
    dispatch.add_argument("--no-wait", action="store_true")
    dispatch.add_argument("--until-started", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action in {"provision", "deprovision"} and not args.execute:
            state = (
                AdapterState(args.state_db, initialize=False)
                if args.state_db.is_file()
                else AdapterState(":memory:")
            )
        elif args.action == "status":
            state = AdapterState(args.state_db, initialize=False)
        else:
            state = AdapterState(args.state_db)
        adapter = HerdrAdapter(state, Herdr08Commands(args.herdr_binary))
        if args.action == "provision":
            if bool(args.agent_core_command) != bool(args.agent_core_db):
                raise HerdrAdapterError(
                    "--agent-core-command and --agent-core-db must be provided together"
                )
            agent_environment = (
                {
                    "AGENT_FLEET_CORE_COMMAND": args.agent_core_command,
                    "AGENT_FLEET_CORE_DB": args.agent_core_db,
                }
                if args.agent_core_command
                else {}
            )
            if args.agent_hook_runtime:
                agent_environment["AGENT_FLEET_HOOK_RUNTIME"] = args.agent_hook_runtime
            result = adapter.provision(
                args.fleet_json,
                args.cwd,
                args.agent_kind,
                args.view_profile_json,
                execute=args.execute,
                agent_environment=agent_environment,
            )
        elif args.action == "deprovision":
            result = adapter.deprovision(args.fleet, execute=args.execute)
        elif args.action in {"bind", "rebind"}:
            result = state.bind(
                args.agent_ref,
                args.workspace,
                args.tab,
                args.pane,
                args.herdr_agent,
                fleet_id=args.fleet,
            ).__dict__
        elif args.action == "resolve":
            result = state.resolve(args.agent_ref, args.fleet).__dict__
        elif args.action == "mark-lost":
            state.mark_lost(args.agent_ref, args.fleet)
            result = {"agent_ref": args.agent_ref, "status": "lost"}
        elif args.action == "view-place":
            result = state.place_view(
                args.agent_ref,
                args.workspace_slot,
                args.tab_slot,
                args.pane_slot,
                args.metadata,
                fleet_id=args.fleet,
                profile_ref=args.profile_ref,
            )
        elif args.action == "status":
            result = state.status(args.fleet)
        else:
            request = args.request_json or {}
            request_envelope = None
            if request.get("apiVersion") or request.get("kind"):
                if request.get("apiVersion") != "fleet.harness/v1" or request.get("kind") != "Command":
                    raise HerdrAdapterError(
                        "Core request must be a fleet.harness/v1 Command envelope"
                    )
                metadata = request.get("metadata", {})
                request_spec = request.get("spec", {})
                if not isinstance(metadata, Mapping) or not isinstance(request_spec, Mapping):
                    raise HerdrAdapterError("Core request metadata and spec must be JSON objects")
                target = request_spec.get("target", {})
                if not isinstance(target, Mapping):
                    raise HerdrAdapterError("Core request spec.target must be a JSON object")
                agent_ref = args.agent_ref or target.get("ref")
                command_id = args.command_id or metadata.get("id")
                command_type = args.type or request_spec.get("type")
                payload = args.payload if args.payload is not None else request_spec.get("payload", {})
                request_envelope = request
                fleet_id = args.fleet or metadata.get("fleet_id")
            else:
                agent_ref = args.agent_ref or request.get("target_agent_ref") or request.get("agent_ref")
                command_id = args.command_id or request.get("command_id")
                command_type = args.type or request.get("type") or request.get("command_type")
                payload = args.payload if args.payload is not None else request.get("payload", {})
                fleet_id = args.fleet or request.get("fleet_id") or "default"
            if not isinstance(agent_ref, str) or not agent_ref:
                raise HerdrAdapterError("dispatch requires agent_ref or request target_agent_ref")
            if not isinstance(command_id, str) or not command_id:
                raise HerdrAdapterError("dispatch requires command_id")
            if not isinstance(command_type, str) or not command_type:
                raise HerdrAdapterError("dispatch requires command type")
            if not isinstance(payload, Mapping):
                raise HerdrAdapterError("dispatch payload must be a JSON object")
            if not isinstance(fleet_id, str) or not fleet_id:
                raise HerdrAdapterError("dispatch requires fleet_id")
            result = adapter.dispatch(
                agent_ref,
                command_id,
                command_type,
                payload,
                timeout_ms=args.timeout_ms,
                execute=args.execute,
                request_envelope=request_envelope,
                fleet_id=fleet_id,
                wait_for_response=not args.no_wait,
                wait_until_started=args.until_started,
            )
    except (HerdrAdapterError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
