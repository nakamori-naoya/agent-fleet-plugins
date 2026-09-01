#!/usr/bin/env python3
"""複数のFleet設定を解決し、Herdr艦隊を一つの入口から起動する。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


Runner = Callable[..., subprocess.CompletedProcess[str]]
DEFAULT_HOOK_SOURCE = Path(__file__).resolve().parents[1] / "hooks" / "role_context.py"


class FleetRuntimeError(RuntimeError):
    """設定解決または艦隊起動を安全に継続できない。"""


class ResolvedFleet:
    def __init__(
        self,
        fleet_id: str,
        fleet_path: Path,
        fleet: Mapping[str, Any],
        profile_ref: str,
        profile_path: Path,
        profile: Mapping[str, Any],
    ):
        self.fleet_id = fleet_id
        self.fleet_path = fleet_path
        self.fleet = fleet
        self.profile_ref = profile_ref
        self.profile_path = profile_path
        self.profile = profile

    @property
    def fleet_hash(self) -> str:
        return _content_hash(self.fleet)

    @property
    def profile_hash(self) -> str:
        return _content_hash(self.profile)


def _content_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_document(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FleetRuntimeError(f"cannot read config {path}: {exc}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            ruby = shutil.which("ruby")
            if ruby is None:
                raise FleetRuntimeError(
                    "YAML parser unavailable: install PyYAML or provide Ruby"
                )
            program = (
                "require 'yaml'; require 'json'; "
                "data=YAML.safe_load(STDIN.read, permitted_classes: [], "
                "permitted_symbols: [], aliases: false); STDOUT.write(JSON.generate(data))"
            )
            completed = subprocess.run(
                [ruby, "-e", program], input=text, capture_output=True, text=True
            )
            if completed.returncode != 0:
                raise FleetRuntimeError(
                    f"invalid YAML {path}: {completed.stderr.strip()}"
                )
            try:
                value = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise FleetRuntimeError(f"invalid YAML bridge output for {path}") from exc
        else:
            try:
                value = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise FleetRuntimeError(f"invalid YAML {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise FleetRuntimeError(f"config root must be an object: {path}")
    return value


def _config_paths(roots: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        found.extend(root.glob("*.yml"))
        found.extend(root.glob("*.yaml"))
        found.extend(root.glob("*.json"))
    return sorted({path.resolve() for path in found}, key=str)


class FleetRuntime:
    def __init__(
        self,
        core_command: Sequence[str],
        herdr_command: Sequence[str],
        controller_command: Sequence[str],
        *,
        runner: Runner = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
        hook_source: Path = DEFAULT_HOOK_SOURCE,
    ):
        self.core_command = tuple(core_command)
        self.herdr_command = tuple(herdr_command)
        self.controller_command = tuple(controller_command)
        self.runner = runner
        self.sleeper = sleeper
        self.hook_source = hook_source

    def _agent_core_command(self) -> str:
        argv = list(self.core_command)
        executable = Path(argv[0])
        if executable.is_file():
            argv[0] = str(executable.resolve())
        else:
            discovered = shutil.which(argv[0])
            if discovered:
                argv[0] = discovered
        return shlex.join(argv)

    def _run_json(
        self,
        argv: Sequence[str],
        context: str,
        *,
        timeout: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        completed = self.runner(
            list(argv), capture_output=True, text=True, timeout=timeout, env=env
        )
        if completed.returncode != 0:
            raise FleetRuntimeError(
                f"{context} failed: {completed.stderr.strip() or 'unknown error'}"
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FleetRuntimeError(f"{context} returned invalid JSON") from exc
        if not isinstance(value, Mapping) or value.get("ok") is not True:
            raise FleetRuntimeError(f"{context} did not return an ok result")
        result = value.get("result")
        if not isinstance(result, Mapping):
            raise FleetRuntimeError(f"{context} result must be an object")
        return result

    def _validated_fleets(
        self, fleet_dirs: Sequence[Path], validation_db: Path
    ) -> dict[str, tuple[Path, Mapping[str, Any]]]:
        catalog: dict[str, tuple[Path, Mapping[str, Any]]] = {}
        for path in _config_paths(fleet_dirs):
            fleet = self._run_json(
                [
                    *self.core_command,
                    "--db",
                    str(validation_db),
                    "spec.validate",
                    "--config",
                    str(path),
                ],
                f"Fleet validation ({path})",
            )
            metadata = fleet.get("metadata")
            fleet_id = metadata.get("id") if isinstance(metadata, Mapping) else None
            if not isinstance(fleet_id, str) or not fleet_id:
                raise FleetRuntimeError(f"validated Fleet has no metadata.id: {path}")
            if fleet_id in catalog:
                raise FleetRuntimeError(
                    f"duplicate Fleet identity {fleet_id}: {catalog[fleet_id][0]} and {path}"
                )
            catalog[fleet_id] = (path, fleet)
        return catalog

    @staticmethod
    def _profile_identity(profile: Mapping[str, Any], path: Path) -> str:
        if (
            profile.get("apiVersion") != "fleet.herdr.harness/v1"
            or profile.get("kind") != "ViewProfile"
        ):
            raise FleetRuntimeError(f"not a ViewProfile: {path}")
        metadata = profile.get("metadata")
        if not isinstance(metadata, Mapping):
            raise FleetRuntimeError(f"ViewProfile metadata is missing: {path}")
        profile_id = metadata.get("id")
        version = metadata.get("version")
        if not isinstance(profile_id, str) or not profile_id or not isinstance(version, int):
            raise FleetRuntimeError(f"ViewProfile identity is invalid: {path}")
        return f"{profile_id}@{version}"

    def _profiles(
        self, profile_dirs: Sequence[Path]
    ) -> dict[str, tuple[Path, Mapping[str, Any]]]:
        catalog: dict[str, tuple[Path, Mapping[str, Any]]] = {}
        for path in _config_paths(profile_dirs):
            profile = _load_document(path)
            identity = self._profile_identity(profile, path)
            if identity in catalog:
                raise FleetRuntimeError(
                    f"duplicate ViewProfile identity {identity}: "
                    f"{catalog[identity][0]} and {path}"
                )
            catalog[identity] = (path, profile)
        return catalog

    def resolve(
        self,
        fleet_name: str,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
        state_dir: Path,
    ) -> ResolvedFleet:
        fleets = self._validated_fleets(
            fleet_dirs, state_dir / ".validation-does-not-write.sqlite3"
        )
        selected = fleets.get(fleet_name)
        if selected is None:
            by_filename = [
                item
                for path, item in fleets.values()
                if path.name.removesuffix(".fleet.yml").removesuffix(".fleet.yaml")
                == fleet_name
            ]
            if len(by_filename) == 1:
                selected = next(
                    (path, fleet)
                    for path, fleet in fleets.values()
                    if fleet is by_filename[0]
                )
        if selected is None:
            raise FleetRuntimeError(f"unknown Fleet config: {fleet_name}")
        fleet_path, fleet = selected
        spec = fleet.get("spec")
        view = spec.get("view") if isinstance(spec, Mapping) else None
        profile_ref = view.get("profile_ref") if isinstance(view, Mapping) else None
        if not isinstance(profile_ref, str) or not profile_ref:
            raise FleetRuntimeError("Fleet spec.view.profile_ref is required")
        profiles = self._profiles(profile_dirs)
        resolved_profile = profiles.get(profile_ref)
        if resolved_profile is None:
            raise FleetRuntimeError(f"ViewProfile not found: {profile_ref}")
        profile_path, profile = resolved_profile
        metadata = fleet["metadata"]
        return ResolvedFleet(
            str(metadata["id"]),
            fleet_path,
            fleet,
            profile_ref,
            profile_path,
            profile,
        )

    def list_configs(
        self,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
        state_dir: Path,
    ) -> list[dict[str, Any]]:
        fleets = self._validated_fleets(
            fleet_dirs, state_dir / ".validation-does-not-write.sqlite3"
        )
        profiles = self._profiles(profile_dirs)
        rows: list[dict[str, Any]] = []
        for fleet_id, (path, fleet) in sorted(fleets.items()):
            spec = fleet["spec"]
            profile_ref = spec["view"]["profile_ref"]
            resolved_profile = profiles.get(profile_ref)
            if resolved_profile is not None:
                _, profile = resolved_profile
                self._run_json(
                    [
                        *self.herdr_command,
                        "--state-db",
                        str(state_dir / ".list-does-not-write.sqlite3"),
                        "provision",
                        "--fleet-json",
                        json.dumps(fleet, ensure_ascii=False, sort_keys=True),
                        "--view-profile-json",
                        json.dumps(profile, ensure_ascii=False, sort_keys=True),
                        "--cwd",
                        str(path.parent),
                        "--agent-kind",
                        "codex",
                    ],
                    f"ViewProfile validation ({profile_ref})",
                )
            rows.append(
                {
                    "fleet_id": fleet_id,
                    "path": str(path),
                    "objective": spec["objective"],
                    "members": len(spec["members"]),
                    "profile_ref": profile_ref,
                    "profile_resolved": resolved_profile is not None,
                    "start_command": f"fleet-runtime start {fleet_id} --execute",
                }
            )
        return rows

    def plan(
        self,
        fleet_name: str,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
        state_dir: Path,
        cwd: str,
        agent_kind: str,
    ) -> dict[str, Any]:
        resolved = self.resolve(fleet_name, fleet_dirs, profile_dirs, state_dir)
        herdr_plan = self._run_json(
            [
                *self.herdr_command,
                "--state-db",
                str(state_dir / "herdr.sqlite3"),
                "provision",
                "--fleet-json",
                json.dumps(resolved.fleet, ensure_ascii=False, sort_keys=True),
                "--view-profile-json",
                json.dumps(resolved.profile, ensure_ascii=False, sort_keys=True),
                "--cwd",
                cwd,
                "--agent-kind",
                agent_kind,
            ],
            "Herdr provision plan",
        )
        return {
            "status": "planned",
            "fleet_id": resolved.fleet_id,
            "fleet_path": str(resolved.fleet_path),
            "fleet_hash": resolved.fleet_hash,
            "profile_ref": resolved.profile_ref,
            "profile_path": str(resolved.profile_path),
            "profile_hash": resolved.profile_hash,
            "herdr": dict(herdr_plan),
        }

    @staticmethod
    def _manifest_path(state_dir: Path, fleet_id: str) -> Path:
        root = (state_dir / "runtimes").resolve()
        path = (root / f"{fleet_id}.json").resolve()
        if path.parent != root:
            raise FleetRuntimeError("Fleet identity escapes the runtime state directory")
        return path

    @staticmethod
    def _fleet_state_dir(state_dir: Path, fleet_id: str) -> Path:
        root = (state_dir / "fleets").resolve()
        path = (root / fleet_id).resolve()
        if path.parent != root:
            raise FleetRuntimeError("Fleet identity escapes the fleet state directory")
        return path

    @staticmethod
    def _write_manifest(path: Path, desired: Mapping[str, Any], phase: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if path.is_symlink():
            raise FleetRuntimeError("runtime manifest must not be a symbolic link")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {**desired, "phase": phase},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        path.chmod(0o600)

    def _materialize_hook_runtime(self, fleet_state_dir: Path) -> tuple[Path, str]:
        source = self.hook_source
        if source.is_symlink() or not source.is_file():
            raise FleetRuntimeError("hook runtime source must be a regular file")
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise FleetRuntimeError(f"cannot read hook runtime source: {exc}") from exc
        digest = hashlib.sha256(payload).hexdigest()
        root = (fleet_state_dir / "hook-runtimes").resolve()
        version_dir = (root / digest).resolve()
        if version_dir.parent != root:
            raise FleetRuntimeError("hook runtime identity escapes the Fleet state directory")
        version_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        version_dir.chmod(0o700)
        target = version_dir / "role_context.py"
        if target.is_symlink():
            raise FleetRuntimeError("hook runtime must not be a symbolic link")
        if target.exists():
            try:
                existing = target.read_bytes()
            except OSError as exc:
                raise FleetRuntimeError(f"cannot read materialized hook runtime: {exc}") from exc
            if existing != payload:
                raise FleetRuntimeError("materialized hook runtime content does not match its hash")
        else:
            temporary = version_dir / f".role_context-{uuid.uuid4().hex}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        target.chmod(0o600)
        return target, digest

    def _validate_hook_runtime(
        self, fleet_state_dir: Path, path: Path, expected_digest: str
    ) -> Path:
        root = (fleet_state_dir / "hook-runtimes").resolve()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise FleetRuntimeError("runtime manifest has an invalid hook hash")
        if path.is_symlink() or not path.is_file():
            raise FleetRuntimeError("materialized hook runtime is missing or unsafe")
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise FleetRuntimeError("materialized hook runtime escapes Fleet state")
        try:
            payload = resolved.read_bytes()
            metadata = resolved.stat()
        except OSError as exc:
            raise FleetRuntimeError(f"cannot validate materialized hook runtime: {exc}") from exc
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise FleetRuntimeError("materialized hook runtime has unsafe ownership or mode")
        if hashlib.sha256(payload).hexdigest() != expected_digest:
            raise FleetRuntimeError("materialized hook runtime content does not match its hash")
        return resolved

    def start(
        self,
        fleet_name: str,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
        state_dir: Path,
        cwd: str,
        agent_kind: str,
        *,
        execute: bool = False,
        once: bool = False,
        poll_seconds: float = 0.25,
    ) -> dict[str, Any]:
        if not execute:
            return self.plan(
                fleet_name, fleet_dirs, profile_dirs, state_dir, cwd, agent_kind
            )
        resolved = self.resolve(fleet_name, fleet_dirs, profile_dirs, state_dir)
        lock_root = state_dir / "locks"
        lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_root.chmod(0o700)
        lock_name = hashlib.sha256(resolved.fleet_id.encode("utf-8")).hexdigest()
        lock_path = lock_root / f"{lock_name}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            lock_path.chmod(0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FleetRuntimeError(
                    f"Fleet {fleet_name!r} already has an active runtime process"
                ) from exc
            return self._start_locked(
                fleet_name,
                fleet_dirs,
                profile_dirs,
                state_dir,
                cwd,
                agent_kind,
                once=once,
                poll_seconds=poll_seconds,
            )

    def _start_locked(
        self,
        fleet_name: str,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
        state_dir: Path,
        cwd: str,
        agent_kind: str,
        *,
        once: bool = False,
        poll_seconds: float = 0.25,
    ) -> dict[str, Any]:
        planned = self.plan(
            fleet_name, fleet_dirs, profile_dirs, state_dir, cwd, agent_kind
        )
        resolved = self.resolve(fleet_name, fleet_dirs, profile_dirs, state_dir)
        manifest_path = self._manifest_path(state_dir, resolved.fleet_id)
        desired = {
            "fleet_id": resolved.fleet_id,
            "fleet_path": str(resolved.fleet_path),
            "fleet_hash": resolved.fleet_hash,
            "profile_ref": resolved.profile_ref,
            "profile_path": str(resolved.profile_path),
            "profile_hash": resolved.profile_hash,
            "cwd": str(Path(cwd).resolve()),
            "agent_kind": agent_kind,
        }
        phase = "planned"
        runtime_generation = uuid.uuid4().hex
        restarting = False
        current: Mapping[str, Any] | None = None
        if manifest_path.exists():
            current = _load_document(manifest_path)
            if not all(current.get(key) == value for key, value in desired.items()):
                raise FleetRuntimeError(
                    "configuration conflict: stop the active Fleet before changing its config"
                )
            phase = str(current.get("phase") or "active")
            runtime_generation = str(
                current.get("runtime_generation") or runtime_generation
            )
            if phase == "stopped":
                phase = "core_provisioned"
                runtime_generation = uuid.uuid4().hex
                restarting = True
        fleet_state_dir = self._fleet_state_dir(state_dir, resolved.fleet_id)
        if current is not None and not restarting and current.get("hook_runtime"):
            hook_runtime = self._validate_hook_runtime(
                fleet_state_dir,
                Path(str(current["hook_runtime"])),
                str(current.get("hook_sha256") or ""),
            )
            hook_sha256 = str(current["hook_sha256"])
        elif current is not None and phase in {"active", "herdr_provisioned"}:
            raise FleetRuntimeError(
                "active Fleet predates stable hook runtimes; stop and restart it once"
            )
        else:
            hook_runtime, hook_sha256 = self._materialize_hook_runtime(fleet_state_dir)
        runtime_manifest = {
            **desired,
            "runtime_generation": runtime_generation,
            "hook_runtime": str(hook_runtime),
            "hook_sha256": hook_sha256,
        }
        if current is None:
            self._write_manifest(
                manifest_path,
                runtime_manifest,
                phase,
            )
        elif phase == "active":
            monitor = self.monitor(
                resolved.fleet_id,
                state_dir,
                once=once,
                poll_seconds=poll_seconds,
            )
            return {**runtime_manifest, "status": "resumed", "monitor": monitor}
        elif not current.get("hook_runtime"):
            self._write_manifest(manifest_path, runtime_manifest, phase)
        core_db = fleet_state_dir / "core.sqlite3"
        herdr_db = fleet_state_dir / "herdr.sqlite3"
        phases = ["planned", "core_provisioned", "herdr_provisioned", "active"]
        if phase not in phases:
            raise FleetRuntimeError(f"unknown runtime phase: {phase}")
        if phases.index(phase) < phases.index("core_provisioned"):
            self._run_json(
                [
                    *self.core_command,
                    "--db",
                    str(core_db),
                    "fleet.provision",
                    "--config",
                    str(resolved.fleet_path),
                ],
                "Core fleet provision",
            )
            phase = "core_provisioned"
            self._write_manifest(manifest_path, runtime_manifest, phase)
        if restarting:
            self._run_json(
                [
                    *self.core_command,
                    "--db",
                    str(core_db),
                    "context.invalidate",
                    "--fleet",
                    resolved.fleet_id,
                ],
                "Core context invalidation",
            )
        provisioned: Mapping[str, Any] = {"status": "already_provisioned"}
        if phases.index(phase) < phases.index("herdr_provisioned"):
            provisioned = self._run_json(
                [
                    *self.herdr_command,
                    "--state-db",
                    str(herdr_db),
                    "provision",
                    "--fleet-json",
                    json.dumps(resolved.fleet, ensure_ascii=False, sort_keys=True),
                    "--view-profile-json",
                    json.dumps(resolved.profile, ensure_ascii=False, sort_keys=True),
                    "--cwd",
                    cwd,
                    "--agent-kind",
                    agent_kind,
                    "--agent-core-command",
                    self._agent_core_command(),
                    "--agent-core-db",
                    str(core_db),
                    "--agent-hook-runtime",
                    str(hook_runtime),
                    "--execute",
                ],
                "Herdr fleet provision",
                timeout=180,
                env={
                    **os.environ,
                    "AGENT_FLEET_CORE_COMMAND": self._agent_core_command(),
                    "AGENT_FLEET_CORE_DB": str(core_db),
                },
            )
            phase = "herdr_provisioned"
            self._write_manifest(manifest_path, runtime_manifest, phase)
        spec = resolved.fleet["spec"]
        manager_ref = spec["collaboration"]["manager"]
        for task in spec["tasks"]:
            if task.get("depends_on"):
                continue
            self._run_json(
                [
                    *self.core_command,
                    "--db",
                    str(core_db),
                    "task.assign",
                    "--fleet",
                    resolved.fleet_id,
                    "--task",
                    task["id"],
                    "--agent-ref",
                    task["assignee"],
                    "--manager-ref",
                    manager_ref,
                    "--command-id",
                    f"task-assign:{resolved.fleet_id}:{task['id']}:1",
                ],
                f"Task assignment ({task['id']})",
            )
        for member in spec["members"]:
            agent_ref = member["agent_ref"]
            control = {
                "fleet_id": resolved.fleet_id,
                "core_command": self._agent_core_command(),
                "core_db": str(core_db),
                "reporting": {
                    "progress_action": "task.progress",
                    "state_action": "task.report",
                    "required_identity": agent_ref,
                    "manager_ref": manager_ref,
                },
            }
            self._run_json(
                [
                    *self.core_command,
                    "--db",
                    str(core_db),
                    "outbox",
                    "--fleet",
                    resolved.fleet_id,
                    "--sender-ref",
                    manager_ref,
                    "--target-agent-ref",
                    agent_ref,
                    "--type",
                    "context.sync",
                    "--command-id",
                    f"context-sync:{resolved.fleet_id}:{agent_ref}:{runtime_generation}",
                    "--payload",
                    json.dumps(
                        {"reason": "fleet_start", "control": control},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ],
                f"Context activation ({agent_ref})",
            )
        self._write_manifest(manifest_path, runtime_manifest, "active")
        monitor = self.monitor(
            resolved.fleet_id,
            state_dir,
            once=once,
            poll_seconds=poll_seconds,
        )
        return {
            **runtime_manifest,
            "status": "started",
            "herdr": dict(provisioned),
            "monitor": monitor,
        }

    def stop(
        self, fleet_id: str, state_dir: Path, *, execute: bool = False
    ) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, fleet_id)
        if not manifest_path.exists():
            return {"fleet_id": fleet_id, "status": "inactive"}
        manifest = _load_document(manifest_path)
        if not execute:
            return {
                "fleet_id": fleet_id,
                "status": "planned",
                "action": "stop",
            }
        self._write_manifest(manifest_path, manifest, "stopping")
        lock_root = state_dir / "locks"
        lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_name = hashlib.sha256(fleet_id.encode("utf-8")).hexdigest()
        lock_path = lock_root / f"{lock_name}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise FleetRuntimeError(
                            f"Fleet {fleet_id!r} controller did not stop within 30 seconds"
                        ) from exc
                    self.sleeper(0.05)
            fleet_state = self._fleet_state_dir(state_dir, fleet_id)
            core_db = fleet_state / "core.sqlite3"
            if core_db.exists():
                self._run_json(
                    [
                        *self.core_command,
                        "--db",
                        str(core_db),
                        "context.invalidate",
                        "--fleet",
                        fleet_id,
                    ],
                    "Core context invalidation",
                )
            herdr = self._run_json(
                [
                    *self.herdr_command,
                    "--state-db",
                    str(fleet_state / "herdr.sqlite3"),
                    "deprovision",
                    "--fleet",
                    fleet_id,
                    "--execute",
                ],
                "Herdr fleet deprovision",
            )
            self._write_manifest(manifest_path, manifest, "stopped")
        return {"fleet_id": fleet_id, "status": "stopped", "herdr": dict(herdr)}

    def remove(
        self, fleet_id: str, state_dir: Path, *, execute: bool = False
    ) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, fleet_id)
        if not execute:
            return {
                "fleet_id": fleet_id,
                "status": "planned",
                "action": "remove",
            }
        stopped = self.stop(fleet_id, state_dir, execute=True)
        core = self._run_json(
            [
                *self.core_command,
                "--db",
                str(self._fleet_state_dir(state_dir, fleet_id) / "core.sqlite3"),
                "fleet.remove",
                "--fleet",
                fleet_id,
                "--confirm-fleet",
                fleet_id,
            ],
            "Core fleet removal",
        )
        if manifest_path.exists():
            manifest_path.unlink()
        return {
            "fleet_id": fleet_id,
            "status": "removed",
            "stop": stopped,
            "core": dict(core),
        }

    def initialize_user_config(
        self,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
        state_dir: Path,
    ) -> dict[str, Any]:
        created: list[str] = []
        for path in [*fleet_dirs, *profile_dirs, state_dir]:
            path_existed = path.exists()
            if not path_existed:
                created.append(str(path))
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path_existed:
                path.chmod(0o700)
        return {"status": "initialized", "created": created}

    def doctor(
        self,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
        state_dir: Path,
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        for label, paths in (("fleet_dirs", fleet_dirs), ("profile_dirs", profile_dirs)):
            checks.append(
                {
                    "check": label,
                    "ok": all(path.is_dir() for path in paths),
                    "paths": [str(path) for path in paths],
                }
            )
        executable = self.core_command[0]
        core_found = Path(executable).is_file() or shutil.which(executable) is not None
        checks.append({"check": "fleet-control", "ok": core_found, "value": executable})
        herdr_found = shutil.which("herdr") is not None
        herdr_check: dict[str, Any] = {"check": "herdr", "ok": False}
        if herdr_found:
            try:
                completed = self.runner(
                    ["herdr", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                version = completed.stdout.strip()
                compatible = completed.returncode == 0 and bool(
                    re.fullmatch(r"herdr 0\.8\.\d+", version)
                )
                herdr_check.update({"ok": compatible, "version": version})
                if not compatible:
                    herdr_check["reason"] = "Herdr 0.8.x is required"
            except (OSError, subprocess.TimeoutExpired) as exc:
                herdr_check["reason"] = str(exc)
        else:
            herdr_check["reason"] = "herdr was not found on PATH"
        checks.append(herdr_check)
        checks.append(
            {
                "check": "state_dir",
                "ok": state_dir.is_dir(),
                "path": str(state_dir),
            }
        )
        return {"status": "healthy" if all(c["ok"] for c in checks) else "issues", "checks": checks}

    def monitor(
        self,
        fleet_id: str,
        state_dir: Path,
        *,
        once: bool,
        poll_seconds: float,
    ) -> dict[str, Any]:
        if poll_seconds <= 0:
            raise FleetRuntimeError("poll_seconds must be positive")
        processed = 0
        idle_rounds = 0
        transient_errors = 0
        last_error: str | None = None
        stopping = False

        def stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        previous_int = signal.signal(signal.SIGINT, stop)
        previous_term = signal.signal(signal.SIGTERM, stop)
        try:
            while not stopping:
                manifest_path = self._manifest_path(state_dir, fleet_id)
                if manifest_path.exists():
                    phase = _load_document(manifest_path).get("phase")
                    if phase in {"stopping", "stopped"}:
                        stopping = True
                        break
                try:
                    result = self._run_json(
                        [
                            *self.controller_command,
                            "--core-command",
                            self.core_command[0],
                            "--herdr-command",
                            self.herdr_command[0],
                            "--core-db",
                            str(self._fleet_state_dir(state_dir, fleet_id) / "core.sqlite3"),
                            "--herdr-db",
                            str(self._fleet_state_dir(state_dir, fleet_id) / "herdr.sqlite3"),
                            "--fleet",
                            fleet_id,
                            "--worker-id",
                            f"controller:{os.getpid()}",
                            "--execute",
                        ],
                        "Fleet controller",
                    )
                except FleetRuntimeError as exc:
                    if once:
                        raise
                    transient_errors += 1
                    last_error = str(exc)
                    self.sleeper(min(poll_seconds * (2 ** min(transient_errors, 5)), 5.0))
                    continue
                transient_errors = 0
                last_error = None
                status = result.get("status")
                if status == "idle":
                    if once:
                        break
                    idle_rounds += 1
                    self.sleeper(
                        min(poll_seconds * (2 ** min(idle_rounds - 1, 2)), 1.0)
                    )
                else:
                    processed += 1
                    idle_rounds = 0
                    if once:
                        break
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        return {
            "status": "stopped" if stopping else "idle",
            "processed": processed,
            "transient_errors": transient_errors,
            "last_error": last_error,
        }

    def status(self, fleet_id: str, state_dir: Path) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, fleet_id)
        if not manifest_path.exists():
            return {"fleet_id": fleet_id, "status": "inactive"}
        manifest = _load_document(manifest_path)
        if manifest.get("phase") == "stopped":
            return {
                "fleet_id": fleet_id,
                "status": "stopped",
                "configuration": dict(manifest),
            }
        drift = False
        for path_key, hash_key in (
            ("fleet_path", "fleet_hash"),
            ("profile_path", "profile_hash"),
        ):
            configured_path = manifest.get(path_key)
            if not isinstance(configured_path, str) or not Path(configured_path).is_file():
                drift = True
                continue
            drift = drift or _content_hash(_load_document(Path(configured_path))) != manifest.get(
                hash_key
            )
        core = self._run_json(
            [
                *self.core_command,
                "--db",
                str(self._fleet_state_dir(state_dir, fleet_id) / "core.sqlite3"),
                "status",
                "--fleet",
                fleet_id,
            ],
            "Core fleet status",
        )
        herdr = self._run_json(
            [
                *self.herdr_command,
                "--state-db",
                str(self._fleet_state_dir(state_dir, fleet_id) / "herdr.sqlite3"),
                "status",
                "--fleet",
                fleet_id,
            ],
            "Herdr fleet status",
        )
        return {
            "fleet_id": fleet_id,
            "status": "configuration_drift" if drift else "active",
            "configuration": dict(manifest),
            "core": dict(core),
            "herdr": dict(herdr),
        }


def _default_state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    return Path(root) / "agent-fleet" if root else Path.home() / ".local/state/agent-fleet"


def build_parser() -> argparse.ArgumentParser:
    adapter_root = Path(__file__).resolve().parent
    core_default = os.environ.get("AGENT_FLEET_CORE_COMMAND") or shutil.which(
        "fleet-control"
    ) or "fleet-control"
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fleet-dir", type=Path, action="append")
    common.add_argument("--profile-dir", type=Path, action="append", default=[])
    common.add_argument("--state-dir", type=Path, default=_default_state_dir())
    common.add_argument("--core-command", default=core_default)
    common.add_argument(
        "--herdr-command", default=str(adapter_root / "scripts" / "fleet-herdr")
    )
    common.add_argument(
        "--controller-command", default=str(adapter_root / "scripts" / "fleet-controller")
    )
    parser = argparse.ArgumentParser(prog="fleet-runtime")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init", parents=[common])
    sub.add_parser("doctor", parents=[common])
    sub.add_parser("list", parents=[common])
    plan = sub.add_parser("plan", parents=[common])
    plan.add_argument("fleet")
    plan.add_argument("--cwd", default=str(Path.cwd()))
    plan.add_argument("--agent-kind", choices=["codex", "claude"], default="codex")
    start = sub.add_parser("start", parents=[common])
    start.add_argument("fleet")
    start.add_argument("--cwd", default=str(Path.cwd()))
    start.add_argument("--agent-kind", choices=["codex", "claude"], default="codex")
    start.add_argument("--execute", action="store_true")
    start.add_argument("--once", action="store_true")
    start.add_argument("--poll-seconds", type=float, default=0.25)
    status = sub.add_parser("status", parents=[common])
    status.add_argument("fleet")
    stop = sub.add_parser("stop", parents=[common])
    stop.add_argument("fleet")
    stop.add_argument("--execute", action="store_true")
    remove = sub.add_parser("remove", parents=[common])
    remove.add_argument("fleet")
    remove.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_root = Path.home() / ".config" / "agent-fleet"
    fleet_dirs = args.fleet_dir or [config_root / "fleets"]
    profile_dirs = args.profile_dir or [config_root / "view-profiles"]
    runtime = FleetRuntime(
        [args.core_command], [args.herdr_command], [args.controller_command]
    )
    try:
        if args.action == "init":
            result = runtime.initialize_user_config(
                fleet_dirs, profile_dirs, args.state_dir
            )
        elif args.action == "doctor":
            result = runtime.doctor(fleet_dirs, profile_dirs, args.state_dir)
        elif args.action == "list":
            result: Any = runtime.list_configs(
                fleet_dirs, profile_dirs, args.state_dir
            )
        elif args.action == "plan":
            result = runtime.plan(
                args.fleet,
                fleet_dirs,
                profile_dirs,
                args.state_dir,
                args.cwd,
                args.agent_kind,
            )
        elif args.action == "start":
            result = runtime.start(
                args.fleet,
                fleet_dirs,
                profile_dirs,
                args.state_dir,
                args.cwd,
                args.agent_kind,
                execute=args.execute,
                once=args.once,
                poll_seconds=args.poll_seconds,
            )
        elif args.action == "status":
            result = runtime.status(args.fleet, args.state_dir)
        elif args.action == "stop":
            result = runtime.stop(args.fleet, args.state_dir, execute=args.execute)
        else:
            result = runtime.remove(args.fleet, args.state_dir, execute=args.execute)
    except (FleetRuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
