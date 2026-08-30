#!/usr/bin/env python3
"""Herdr 0.8 adapter for Agent Fleet.

RuntimeBinding and ViewPlacement are adapter state.  They never leak into the
Core SQLite schema.  Commands are always built as argv and execution is opt-in.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


COMMAND_TYPES = frozenset(
    {"fleet.provision", "task.assign", "message.send", "task.report", "fleet.reconcile"}
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
    agent_ref TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    tab_id TEXT NOT NULL,
    pane_id TEXT NOT NULL,
    herdr_agent TEXT,
    status TEXT NOT NULL CHECK (status IN ('bound','lost')),
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_bindings_pane ON runtime_bindings(pane_id);
CREATE TABLE IF NOT EXISTS view_placements (
    agent_ref TEXT PRIMARY KEY,
    workspace_slot TEXT NOT NULL,
    tab_slot TEXT NOT NULL,
    pane_slot TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
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
    manager_ref: str
    worker_refs: tuple[str, ...]
    operations: tuple[Mapping[str, Any], ...]
    placements: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fleet_id": self.fleet_id,
            "profile": "command-deck",
            "manager_ref": self.manager_ref,
            "worker_refs": list(self.worker_refs),
            "operations": [dict(operation) for operation in self.operations],
            "placements": [dict(placement) for placement in self.placements],
        }


class AdapterState:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(ADAPTER_SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 5000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def bind(
        self,
        agent_ref: str,
        workspace_id: str,
        tab_id: str,
        pane_id: str,
        herdr_agent: str | None = None,
    ) -> RuntimeBinding:
        values = [
            safe_token(agent_ref, "agent_ref"),
            safe_token(workspace_id, "workspace_id"),
            safe_token(tab_id, "tab_id"),
            safe_token(pane_id, "pane_id"),
        ]
        if herdr_agent is not None:
            safe_token(herdr_agent, "herdr_agent")
        with self.connect() as db:
            db.execute(
                "INSERT INTO runtime_bindings(agent_ref,workspace_id,tab_id,pane_id,herdr_agent,status,updated_at) "
                "VALUES(?,?,?,?,?,'bound',?) ON CONFLICT(agent_ref) DO UPDATE SET "
                "workspace_id=excluded.workspace_id,tab_id=excluded.tab_id,pane_id=excluded.pane_id,"
                "herdr_agent=excluded.herdr_agent,status='bound',updated_at=excluded.updated_at",
                (*values, herdr_agent, utc_now()),
            )
        return self.resolve(agent_ref)

    def resolve(self, agent_ref: str) -> RuntimeBinding:
        with self.connect() as db:
            row = db.execute(
                "SELECT agent_ref,workspace_id,tab_id,pane_id,herdr_agent,status "
                "FROM runtime_bindings WHERE agent_ref=?",
                (agent_ref,),
            ).fetchone()
        if row is None:
            raise HerdrAdapterError(f"agent_ref {agent_ref!r} is not bound; run bind/rebind")
        binding = RuntimeBinding(**dict(row))
        if binding.status == "lost":
            raise HerdrAdapterError(f"pane for agent_ref {agent_ref!r} is lost; run bind/rebind")
        return binding

    def mark_lost(self, agent_ref: str) -> None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runtime_bindings SET status='lost',updated_at=? WHERE agent_ref=?",
                (utc_now(), agent_ref),
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
    ) -> dict[str, Any]:
        values = (
            safe_token(agent_ref, "agent_ref"),
            safe_token(workspace_slot, "workspace_slot"),
            safe_token(tab_slot, "tab_slot"),
            safe_token(pane_slot, "pane_slot"),
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO view_placements(agent_ref,workspace_slot,tab_slot,pane_slot,metadata_json,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(agent_ref) DO UPDATE SET "
                "workspace_slot=excluded.workspace_slot,tab_slot=excluded.tab_slot,"
                "pane_slot=excluded.pane_slot,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                (*values, json.dumps(metadata or {}, sort_keys=True), utc_now()),
            )
        return {
            "agent_ref": agent_ref,
            "workspace_slot": workspace_slot,
            "tab_slot": tab_slot,
            "pane_slot": pane_slot,
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
                    "INSERT INTO runtime_bindings(agent_ref,workspace_id,tab_id,pane_id,herdr_agent,status,updated_at) "
                    "VALUES(?,?,?,?,?,'bound',?) ON CONFLICT(agent_ref) DO UPDATE SET "
                    "workspace_id=excluded.workspace_id,tab_id=excluded.tab_id,pane_id=excluded.pane_id,"
                    "herdr_agent=excluded.herdr_agent,status='bound',updated_at=excluded.updated_at",
                    (
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
                    "INSERT INTO view_placements(agent_ref,workspace_slot,tab_slot,pane_slot,metadata_json,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(agent_ref) DO UPDATE SET "
                    "workspace_slot=excluded.workspace_slot,tab_slot=excluded.tab_slot,"
                    "pane_slot=excluded.pane_slot,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (
                        placement["agent_ref"],
                        placement["workspace_slot"],
                        placement["tab_slot"],
                        placement["pane_slot"],
                        json.dumps(placement.get("metadata", {}), sort_keys=True),
                        now,
                    ),
                )


class Herdr08Commands:
    """Safe argv builders for the Herdr 0.8 CLI surface."""

    def __init__(self, binary: str = "herdr"):
        self.binary = safe_token(binary, "Herdr binary")

    def workspace_create(self, cwd: str, label: str) -> list[str]:
        return [
            self.binary,
            "workspace",
            "create",
            "--cwd",
            safe_token(cwd, "cwd"),
            "--label",
            safe_token(label, "label"),
            "--no-focus",
        ]

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
        self, pane_id: str, direction: str, cwd: str, ratio: float | None = None
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

    def agent_prompt(self, pane_id: str, prompt: str, timeout_ms: int) -> list[str]:
        if timeout_ms <= 0:
            raise HerdrAdapterError("timeout_ms must be positive")
        return [
            self.binary,
            "agent",
            "prompt",
            safe_token(pane_id, "pane_id"),
            safe_token(prompt, "prompt"),
            "--wait",
            "--timeout",
            str(timeout_ms),
        ]


Runner = Callable[..., subprocess.CompletedProcess[str]]


class HerdrAdapter:
    def __init__(
        self,
        state: AdapterState,
        commands: Herdr08Commands | None = None,
        runner: Runner = subprocess.run,
    ):
        self.state = state
        self.commands = commands or Herdr08Commands()
        self.runner = runner

    @staticmethod
    def _fleet_parts(fleet: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
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
        if not isinstance(view, Mapping) or view.get("profile") != "command-deck":
            raise HerdrAdapterError("Fleet spec.view.profile must be command-deck")
        manager_ref = collaboration.get("manager")
        if not isinstance(manager_ref, str) or not manager_ref:
            raise HerdrAdapterError("Fleet spec.collaboration.manager must be an agent_ref")

        refs: list[str] = []
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
        if refs.count(manager_ref) != 1:
            raise HerdrAdapterError("Fleet must contain exactly one declared manager member")
        workers = tuple(agent_ref for agent_ref in refs if agent_ref != manager_ref)
        if not 1 <= len(workers) <= 4:
            raise HerdrAdapterError("command-deck requires one to four worker members")
        return fleet_id, manager_ref, workers

    def plan_provision(
        self,
        fleet: Mapping[str, Any],
        cwd: str,
        agent_kind: str,
    ) -> ProvisionPlan:
        fleet_id, manager_ref, workers = self._fleet_parts(fleet)
        safe_token(cwd, "cwd")
        safe_token(agent_kind, "agent_kind")
        root_pane = "$workspace.root_pane"
        operations: list[Mapping[str, Any]] = [
            {
                "id": "workspace.create",
                "argv": self.commands.workspace_create(cwd, fleet_id),
                "produces": ["workspace_id", "tab_id", "root_pane_id"],
            },
            {
                "id": f"agent.start:{manager_ref}",
                "argv": self.commands.agent_start(manager_ref, agent_kind, root_pane),
            },
        ]
        placements: list[Mapping[str, Any]] = [
            {
                "agent_ref": manager_ref,
                "workspace_slot": fleet_id,
                "tab_slot": "command-deck",
                "pane_slot": "left",
                "metadata": {"profile": "command-deck", "fraction": 0.32, "order": 0},
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
                        "right" if index == 1 else "down",
                        cwd,
                        0.68 if index == 1 else 0.5,
                    ),
                    "produces": [pane_ref],
                }
            )
            operations.append(
                {
                    "id": f"agent.start:{worker_ref}",
                    "argv": self.commands.agent_start(worker_ref, agent_kind, pane_ref),
                }
            )
            placements.append(
                {
                    "agent_ref": worker_ref,
                    "workspace_slot": fleet_id,
                    "tab_slot": "command-deck",
                    "pane_slot": f"right.{index}",
                    "metadata": {"profile": "command-deck", "order": index},
                }
            )
            split_target = pane_ref
        return ProvisionPlan(
            fleet_id, manager_ref, workers, tuple(operations), tuple(placements)
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

    def _execute_argv(self, argv: Sequence[str], context: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = self.runner(
                list(argv), capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired as exc:
            raise HerdrAdapterError(f"{context} timed out; bindings were not saved") from exc
        if completed.returncode != 0:
            raise HerdrAdapterError(
                f"{context} failed: {completed.stderr.strip() or 'unknown Herdr error'}; "
                "bindings were not saved"
            )
        return completed

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
        *,
        execute: bool = False,
    ) -> dict[str, Any]:
        plan = self.plan_provision(fleet, cwd, agent_kind)
        if not execute:
            return {"mode": "dry-run", "status": "planned", "plan": plan.as_dict()}

        workspace_op = plan.operations[0]
        workspace_result = self._execute_argv(
            workspace_op["argv"], "herdr workspace create"
        )
        workspace_id, tab_id, root_pane_id = self._workspace_ids(workspace_result.stdout)
        pane_ids: dict[str, str] = {"$workspace.root_pane": root_pane_id}
        binding_panes: dict[str, str] = {plan.manager_ref: root_pane_id}

        for operation in plan.operations[1:]:
            operation_id = str(operation["id"])
            argv = self._resolve_argv(operation["argv"], pane_ids)
            completed = self._execute_argv(argv, f"herdr {operation_id}")
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

    def plan_command(
        self,
        agent_ref: str,
        command_id: str,
        command_type: str,
        payload: Mapping[str, Any],
        timeout_ms: int = 30_000,
        request_envelope: Mapping[str, Any] | None = None,
    ) -> CommandPlan:
        if command_type not in COMMAND_TYPES:
            raise HerdrAdapterError(f"unsupported command type: {command_type}")
        binding = self.state.resolve(agent_ref)
        envelope = json.dumps(
            request_envelope
            or {"command_id": command_id, "type": command_type, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt = f"Agent Fleet command (reply through an explicit task.report when applicable):\n{envelope}"
        return CommandPlan(
            tuple(self.commands.pane_get(binding.pane_id)),
            tuple(self.commands.agent_prompt(binding.pane_id, prompt, timeout_ms)),
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
    ) -> dict[str, Any]:
        plan = self.plan_command(
            agent_ref, command_id, command_type, payload, timeout_ms, request_envelope
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
                self.state.mark_lost(agent_ref)
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
                timeout=max(10, timeout_ms / 1000 + 5),
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
    provision.add_argument("--cwd", required=True)
    provision.add_argument("--agent-kind", required=True)
    provision.add_argument("--execute", action="store_true")
    bind = sub.add_parser("bind", aliases=["rebind"])
    bind.add_argument("--agent-ref", required=True)
    bind.add_argument("--workspace", required=True)
    bind.add_argument("--tab", required=True)
    bind.add_argument("--pane", required=True)
    bind.add_argument("--herdr-agent")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--agent-ref", required=True)
    lost = sub.add_parser("mark-lost")
    lost.add_argument("--agent-ref", required=True)
    place = sub.add_parser("view-place")
    place.add_argument("--agent-ref", required=True)
    place.add_argument("--workspace-slot", required=True)
    place.add_argument("--tab-slot", required=True)
    place.add_argument("--pane-slot", required=True)
    place.add_argument("--metadata", type=json_object, default={})
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("--request-json", type=json_object)
    dispatch.add_argument("--agent-ref")
    dispatch.add_argument("--command-id")
    dispatch.add_argument("--type", choices=sorted(COMMAND_TYPES))
    dispatch.add_argument("--payload", type=json_object)
    dispatch.add_argument("--timeout-ms", type=int, default=30_000)
    dispatch.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state = AdapterState(args.state_db)
    adapter = HerdrAdapter(state, Herdr08Commands(args.herdr_binary))
    try:
        if args.action == "provision":
            result = adapter.provision(
                args.fleet_json,
                args.cwd,
                args.agent_kind,
                execute=args.execute,
            )
        elif args.action in {"bind", "rebind"}:
            result = state.bind(
                args.agent_ref, args.workspace, args.tab, args.pane, args.herdr_agent
            ).__dict__
        elif args.action == "resolve":
            result = state.resolve(args.agent_ref).__dict__
        elif args.action == "mark-lost":
            state.mark_lost(args.agent_ref)
            result = {"agent_ref": args.agent_ref, "status": "lost"}
        elif args.action == "view-place":
            result = state.place_view(
                args.agent_ref,
                args.workspace_slot,
                args.tab_slot,
                args.pane_slot,
                args.metadata,
            )
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
            else:
                agent_ref = args.agent_ref or request.get("target_agent_ref") or request.get("agent_ref")
                command_id = args.command_id or request.get("command_id")
                command_type = args.type or request.get("type") or request.get("command_type")
                payload = args.payload if args.payload is not None else request.get("payload", {})
            if not isinstance(agent_ref, str) or not agent_ref:
                raise HerdrAdapterError("dispatch requires agent_ref or request target_agent_ref")
            if not isinstance(command_id, str) or not command_id:
                raise HerdrAdapterError("dispatch requires command_id")
            if not isinstance(command_type, str) or not command_type:
                raise HerdrAdapterError("dispatch requires command type")
            if not isinstance(payload, Mapping):
                raise HerdrAdapterError("dispatch payload must be a JSON object")
            result = adapter.dispatch(
                agent_ref,
                command_id,
                command_type,
                payload,
                timeout_ms=args.timeout_ms,
                execute=args.execute,
                request_envelope=request_envelope,
            )
    except (HerdrAdapterError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
