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
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TextIO

from runtime_models import (Runner, DEFAULT_HOOK_SOURCE, MANIFEST_FORMAT_VERSION, RUNTIME_PHASES,
                            FleetRuntimeError, ExecutionBundle, ResolvedFleet, _content_hash, _load_document)
from execution_identity import ExecutionIdentity

DEFAULT_VIEW_PROFILE = Path(__file__).resolve().parent / "config" / "default-view-profile.yml"


def _config_paths(roots: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        found.extend(root.glob("*.yml"))
        found.extend(root.glob("*.yaml"))
        found.extend(root.glob("*.json"))
    return sorted({path.resolve() for path in found}, key=str)


class FleetRuntime(ExecutionIdentity):
    def __init__(
        self,
        core_command: Sequence[str],
        herdr_command: Sequence[str],
        controller_command: Sequence[str],
        *,
        runner: Runner = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
        hook_source: Path = DEFAULT_HOOK_SOURCE,
        role_catalog: Path | None = None,
    ):
        self.core_command = tuple(core_command)
        self.herdr_command = tuple(herdr_command)
        self.controller_command = tuple(controller_command)
        self.runner = runner
        self.sleeper = sleeper
        self.hook_source = hook_source
        self.role_catalog = role_catalog

    @staticmethod
    def _requested_fleet_id(fleet_name: str) -> str:
        path = Path(fleet_name)
        if not path.is_absolute():
            return fleet_name
        document = _load_document(path)
        metadata = document.get("metadata")
        fleet_id = metadata.get("id") if isinstance(metadata, Mapping) else None
        if (
            not isinstance(fleet_id, str)
            or re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", fleet_id) is None
        ):
            raise FleetRuntimeError(
                f"Fleet file has no safe metadata.id: {path}"
            )
        return fleet_id

    def _role_catalog_args(self) -> list[str]:
        return (
            ["--role-catalog", str(self.role_catalog)]
            if self.role_catalog is not None
            else []
        )

    def _agent_core_command(self) -> str:
        argv = list(self.core_command)
        if len(argv) != 1:
            raise FleetRuntimeError(
                "agent Core command must be one executable path without arguments"
            )
        executable = Path(argv[0])
        if executable.is_file():
            argv[0] = str(executable.resolve())
        else:
            discovered = shutil.which(argv[0])
            if discovered:
                argv[0] = discovered
        return argv[0]

    @staticmethod
    def _interactive_shell_argv(
        command: str, arguments: Sequence[str]
    ) -> list[str]:
        shell_value = os.environ.get("SHELL") or shutil.which("zsh") or shutil.which("bash")
        if not shell_value:
            raise FleetRuntimeError(
                "an interactive shell is required to resolve Fleet member commands"
            )
        shell_name = Path(shell_value).name
        supported_shells = {
            "bash": (Path("/bin/bash"), Path("/usr/bin/bash")),
            "zsh": (Path("/bin/zsh"), Path("/usr/bin/zsh")),
        }
        candidates = supported_shells.get(shell_name, ())
        shell = next(
            (
                candidate
                for candidate in candidates
                if candidate.is_file() and os.access(candidate, os.X_OK)
            ),
            None,
        )
        if shell is None:
            raise FleetRuntimeError(
                "Fleet member command aliases require an executable /bin or /usr/bin "
                f"bash/zsh shell (configured shell={shell_value!r})"
            )
        return [str(shell), "-lic", shlex.join([command, *arguments])]

    def _with_execution_bundle(self, bundle: ExecutionBundle) -> FleetRuntime:
        runtime = FleetRuntime(
            bundle.command("core"),
            bundle.command("herdr"),
            bundle.command("controller"),
            runner=self.runner,
            sleeper=self.sleeper,
            hook_source=self.hook_source,
            role_catalog=self.role_catalog,
        )
        # Preserve explicit instance-level test/integration seams while changing
        # only the executable commands. Normal CLI instances have none of these.
        for name in (
            "_profiles",
            "_preflight_runtime",
            "_run_json",
            "_materialize_hook_runtime",
        ):
            if name in self.__dict__:
                setattr(runtime, name, self.__dict__[name])
        return runtime


    def _run_json(
        self,
        argv: Sequence[str],
        context: str,
        *,
        timeout: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        runtime_env = dict(os.environ)
        runtime_env["PYTHONDONTWRITEBYTECODE"] = "1"
        if env is not None:
            runtime_env.update(env)
        completed = self.runner(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=runtime_env,
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
        self,
        fleet_dirs: Sequence[Path],
        validation_db: Path,
        role_catalog: Mapping[str, Any] | None,
    ) -> dict[str, tuple[Path, Mapping[str, Any], Mapping[str, Any], str]]:
        catalog: dict[
            str, tuple[Path, Mapping[str, Any], Mapping[str, Any], str]
        ] = {}
        with tempfile.TemporaryDirectory(prefix="agent-fleet-validation-") as temporary:
            snapshot_root = Path(temporary)
            role_catalog_path: Path | None = None
            if role_catalog is not None:
                role_catalog_path = snapshot_root / "role-catalog.json"
                self._write_fixed_snapshot(role_catalog_path, role_catalog)
            for index, path in enumerate(_config_paths(fleet_dirs)):
                source = _load_document(path)
                source_hash = _content_hash(source)
                fleet_snapshot = snapshot_root / f"fleet-{index}.json"
                self._write_fixed_snapshot(fleet_snapshot, source)
                fleet = self._run_json(
                    [
                        *self.core_command,
                        "--db",
                        str(validation_db),
                        "spec.validate",
                        "--config",
                        str(fleet_snapshot),
                        *(
                            ["--role-catalog", str(role_catalog_path)]
                            if role_catalog_path is not None
                            else []
                        ),
                    ],
                    f"Fleet validation ({path})",
                )
                if _content_hash(_load_document(path)) != source_hash:
                    raise FleetRuntimeError(
                        f"configuration changed during Fleet validation: {path}"
                    )
                metadata = fleet.get("metadata")
                fleet_id = metadata.get("id") if isinstance(metadata, Mapping) else None
                if not isinstance(fleet_id, str) or not fleet_id:
                    raise FleetRuntimeError(f"validated Fleet has no metadata.id: {path}")
                if fleet_id in catalog:
                    raise FleetRuntimeError(
                        f"duplicate Fleet identity {fleet_id}: {catalog[fleet_id][0]} and {path}"
                    )
                catalog[fleet_id] = (path, source, fleet, source_hash)
        return catalog

    def _validated_fleet_path(
        self,
        path: Path,
        validation_db: Path,
        role_catalog: Mapping[str, Any] | None,
    ) -> tuple[Path, Mapping[str, Any], Mapping[str, Any], str]:
        if not path.is_absolute():
            raise FleetRuntimeError("Fleet file path must be absolute")
        if path.is_symlink() or not path.is_file():
            raise FleetRuntimeError(f"Fleet file is unavailable or unsafe: {path}")
        resolved_path = path.resolve()
        source = _load_document(resolved_path)
        source_hash = _content_hash(source)
        with tempfile.TemporaryDirectory(prefix="agent-fleet-validation-") as temporary:
            snapshot_root = Path(temporary)
            fleet_snapshot = snapshot_root / "fleet.json"
            self._write_fixed_snapshot(fleet_snapshot, source)
            role_catalog_path: Path | None = None
            if role_catalog is not None:
                role_catalog_path = snapshot_root / "role-catalog.json"
                self._write_fixed_snapshot(role_catalog_path, role_catalog)
            fleet = self._run_json(
                [
                    *self.core_command,
                    "--db",
                    str(validation_db),
                    "spec.validate",
                    "--config",
                    str(fleet_snapshot),
                    *(
                        ["--role-catalog", str(role_catalog_path)]
                        if role_catalog_path is not None
                        else []
                    ),
                ],
                f"Fleet validation ({resolved_path})",
            )
        if _content_hash(_load_document(resolved_path)) != source_hash:
            raise FleetRuntimeError(
                f"configuration changed during Fleet validation: {resolved_path}"
            )
        return resolved_path, source, fleet, source_hash

    def _resolve_direct_fleet(
        self,
        fleet_path: Path,
        state_dir: Path,
    ) -> ResolvedFleet:
        role_catalog = (
            _load_document(self.role_catalog)
            if self.role_catalog is not None
            else None
        )
        role_catalog_hash = (
            _content_hash(role_catalog) if role_catalog is not None else None
        )
        path, source, fleet, source_hash = self._validated_fleet_path(
            fleet_path,
            state_dir / ".validation-does-not-write.sqlite3",
            role_catalog,
        )
        if fleet.get("apiVersion") != "fleet.harness/v3":
            raise FleetRuntimeError("Fleet apiVersion must be fleet.harness/v3")
        spec = fleet.get("spec")
        metadata = fleet.get("metadata")
        if not isinstance(spec, Mapping) or not isinstance(metadata, Mapping):
            raise FleetRuntimeError("validated Fleet has invalid metadata or spec")
        fleet_id = str(metadata["id"])
        configured_profile = spec.get("view_profile")
        if configured_profile is None:
            profile_path = DEFAULT_VIEW_PROFILE
        else:
            candidate = Path(str(configured_profile))
            profile_path = (
                candidate if candidate.is_absolute() else path.parent / candidate
            )
        if profile_path.is_symlink() or not profile_path.is_file():
            raise FleetRuntimeError(
                f"ViewProfile file is unavailable or unsafe: {profile_path}"
            )
        profile_path = profile_path.resolve()
        profile = _load_document(profile_path)
        profile_ref = self._profile_identity(profile, profile_path)

        return ResolvedFleet(
            fleet_id,
            path,
            source,
            fleet,
            profile_ref,
            profile_path,
            profile,
            str(spec.get("codex_hook_trust", "review")),
            source_hash,
            role_catalog,
            role_catalog_hash,
        )

    @staticmethod
    def _profile_identity(profile: Mapping[str, Any], path: Path) -> str:
        if (
            profile.get("apiVersion") != "fleet.herdr.harness/v2"
            or profile.get("kind") != "ViewProfile"
        ):
            raise FleetRuntimeError(f"not a ViewProfile: {path}")
        metadata = profile.get("metadata")
        if not isinstance(metadata, Mapping):
            raise FleetRuntimeError(f"ViewProfile metadata is missing: {path}")
        profile_id = metadata.get("id")
        version = metadata.get("version")
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
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
        requested_path = Path(fleet_name)
        if not requested_path.is_absolute():
            raise FleetRuntimeError("Fleet file path must be absolute")
        return self._resolve_direct_fleet(requested_path, state_dir)

    def list_configs(
        self,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
        state_dir: Path,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        fleets = self._validated_fleets(
            fleet_dirs,
            state_dir / ".validation-does-not-write.sqlite3",
            (
                _load_document(self.role_catalog)
                if self.role_catalog is not None
                else None
            ),
        )
        for fleet_id, (fleet_path, _, _, _) in sorted(fleets.items()):
            resolved = self.resolve(str(fleet_path), fleet_dirs, profile_dirs, state_dir)
            spec = resolved.fleet["spec"]
            self._run_json(
                [
                    *self.herdr_command,
                    "--state-db",
                    str(state_dir / ".list-does-not-write.sqlite3"),
                    "provision",
                    "--fleet-json",
                    json.dumps(resolved.fleet, ensure_ascii=False, sort_keys=True),
                    "--view-profile-json",
                    json.dumps(resolved.profile, ensure_ascii=False, sort_keys=True),
                    "--cwd",
                    str(resolved.fleet_path.parent),
                    "--agent-kind",
                    "codex",
                ],
                f"Fleet composition validation ({fleet_id})",
            )
            rows.append(
                {
                    "fleet_id": resolved.fleet_id,
                    "path": str(resolved.fleet_path),
                    "objective": spec["objective"],
                    "members": len(spec["members"]),
                    "member_runtimes": {
                        member["agent_ref"]: dict(member["runtime"])
                        for member in spec["members"]
                    },
                    "profile_ref": resolved.profile_ref,
                    "profile_resolved": True,
                    "start_command": shlex.join(
                        [
                            "fleet-runtime",
                            "start",
                            str(resolved.fleet_path),
                            *self._role_catalog_args(),
                            "--execute",
                        ]
                    ),
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
        return self._plan_resolved(resolved, state_dir, cwd, agent_kind)

    def _plan_resolved(
        self,
        resolved: ResolvedFleet,
        state_dir: Path,
        cwd: str,
        agent_kind: str,
        hook_sha256: str | None = None,
    ) -> dict[str, Any]:
        fleet_state_dir = self._fleet_state_dir(state_dir, resolved.fleet_id)
        if hook_sha256 is None:
            hook_sha256 = hashlib.sha256(self._capture_hook_source()).hexdigest()
        planned_hook_runtime = (
            fleet_state_dir
            / "hook-runtimes"
            / hook_sha256
            / "role_context.py"
        )
        herdr_plan = self._run_json(
            [
                *self.herdr_command,
                "--state-db",
                str(fleet_state_dir / "herdr.sqlite3"),
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
                str(fleet_state_dir / "core.sqlite3"),
                "--agent-hook-runtime",
                str(planned_hook_runtime),
            ],
            "Herdr provision plan",
        )
        return {
            "status": "planned",
            "fleet_id": resolved.fleet_id,
            "fleet_path": str(resolved.fleet_path),
            "fleet_hash": resolved.fleet_hash,
            "fleet_source_hash": resolved.fleet_source_hash,
            "profile_ref": resolved.profile_ref,
            "profile_path": str(resolved.profile_path),
            "profile_hash": resolved.profile_hash,
            "composition_hash": resolved.composition_hash,
            "herdr": dict(herdr_plan),
        }

    def _validate_composition(
        self,
        resolved: ResolvedFleet,
        state_dir: Path,
        cwd: str,
        agent_kind: str,
    ) -> None:
        """Validate Fleet and ViewProfile composition without creating runtime state."""
        self._run_json(
            [
                *self.herdr_command,
                "--state-db",
                str(state_dir / ".composition-validation-does-not-write.sqlite3"),
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
            f"Fleet composition validation ({resolved.fleet_id})",
        )

    @staticmethod
    def _manifest_path(state_dir: Path, fleet_id: str) -> Path:
        return (FleetRuntime._fleet_state_dir(state_dir, fleet_id) / "manifest.json").resolve()

    @staticmethod
    def _fleet_state_dir(state_dir: Path, fleet_id: str) -> Path:
        root = (state_dir / "runs").resolve()
        path = (root / fleet_id).resolve()
        if path.parent != root:
            raise FleetRuntimeError("Fleet identity escapes the fleet state directory")
        return path

    @staticmethod
    def _operation_lock_path(
        state_dir: Path, namespace: str, identity_value: str
    ) -> Path:
        lock_root = Path(tempfile.gettempdir()) / f"agent-fleet-runtime-locks-{os.getuid()}"
        if lock_root.is_symlink():
            raise FleetRuntimeError("Fleet lock directory must not be a symbolic link")
        lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if lock_root.stat().st_uid != os.getuid():
            raise FleetRuntimeError("Fleet lock directory has unsafe ownership")
        lock_root.chmod(0o700)
        identity = (
            f"{state_dir.resolve()}\0{namespace}\0{identity_value}"
        ).encode("utf-8")
        lock_name = hashlib.sha256(identity).hexdigest()
        return lock_root / f"{lock_name}.lock"

    @staticmethod
    def _fleet_lock_path(state_dir: Path, fleet_id: str) -> Path:
        return FleetRuntime._operation_lock_path(state_dir, "fleet", fleet_id)

    @staticmethod
    def _process_lock_path(state_dir: Path, fleet_id: str) -> Path:
        return FleetRuntime._operation_lock_path(state_dir, "process", fleet_id)

    @staticmethod
    def _open_fleet_lock(path: Path) -> TextIO:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise FleetRuntimeError(f"cannot open Fleet lock safely: {exc}") from exc
        try:
            os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "a+", encoding="utf-8")
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _stop_request_dir(state_dir: Path, fleet_id: str) -> Path:
        root = (state_dir / "stop-requests").resolve()
        request_dir = (root / fleet_id).resolve()
        if request_dir.parent != root:
            raise FleetRuntimeError("Fleet identity escapes the stop request directory")
        return request_dir

    @contextmanager
    def _publish_stop_request(
        self, state_dir: Path, fleet_id: str
    ) -> Iterator[Path]:
        request_dir = self._stop_request_dir(state_dir, fleet_id)
        if request_dir.is_symlink():
            raise FleetRuntimeError("stop request directory must not be a symbolic link")
        request_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        request_dir.chmod(0o700)
        request_id = f"{os.getpid()}-{uuid.uuid4().hex}"
        temporary = request_dir / f".{request_id}.tmp"
        request_path = request_dir / f"{request_id}.request"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        completed = False
        try:
            os.write(descriptor, b"stop\n")
            os.fsync(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            temporary.replace(request_path)
            yield request_path
            completed = True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            temporary.unlink(missing_ok=True)
            if completed:
                self._clear_completed_stop_requests(request_dir, request_path)

    def _stop_requested(self, state_dir: Path, fleet_id: str) -> bool:
        request_dir = self._stop_request_dir(state_dir, fleet_id)
        if not request_dir.is_dir():
            return False
        for request_path in request_dir.glob("*.request"):
            return True
        try:
            request_dir.rmdir()
            request_dir.parent.rmdir()
        except OSError:
            pass
        return False

    def _raise_if_stop_requested(self, state_dir: Path, fleet_id: str) -> None:
        if self._stop_requested(state_dir, fleet_id):
            raise FleetRuntimeError(
                f"Fleet {fleet_id!r} was cancelled by a stop request"
            )

    def _clear_completed_stop_requests(
        self, request_dir: Path, completed_request: Path
    ) -> None:
        completed_request.unlink(missing_ok=True)
        for request_path in request_dir.glob("*.request"):
            if request_path.is_symlink():
                request_path.unlink(missing_ok=True)
                continue
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(request_path, flags)
            except OSError:
                continue
            locked = False
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError:
                    continue
                request_path.unlink(missing_ok=True)
            finally:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        try:
            request_dir.rmdir()
            request_dir.parent.rmdir()
        except OSError:
            pass

    @contextmanager
    def _hold_identity_lock(
        self,
        path: Path,
        *,
        timeout_seconds: float,
        timeout_message: str,
    ) -> Iterator[None]:
        lock = self._open_fleet_lock(path)
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise FleetRuntimeError(timeout_message) from exc
                    self.sleeper(0.05)
            yield
        finally:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()

    @contextmanager
    def _hold_process_lock(
        self,
        state_dir: Path,
        fleet_id: str,
        *,
        timeout_seconds: float,
        timeout_message: str,
    ) -> Iterator[None]:
        with self._hold_identity_lock(
            self._process_lock_path(state_dir, fleet_id),
            timeout_seconds=timeout_seconds,
            timeout_message=timeout_message,
        ):
            yield

    @contextmanager
    def _hold_fleet_lock(
        self,
        state_dir: Path,
        fleet_id: str,
        *,
        timeout_seconds: float,
        timeout_message: str,
    ) -> Iterator[None]:
        with self._hold_identity_lock(
            self._fleet_lock_path(state_dir, fleet_id),
            timeout_seconds=timeout_seconds,
            timeout_message=timeout_message,
        ):
            yield

    @staticmethod
    def _write_manifest(path: Path, desired: Mapping[str, Any], phase: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if path.is_symlink():
            raise FleetRuntimeError("runtime manifest must not be a symbolic link")
        payload = (
            json.dumps(
                {**desired, "phase": phase},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        path.chmod(0o600)
        FleetRuntime._index_manifest(path, {**desired, "phase": phase})

    @staticmethod
    def _index_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
        run_id = manifest.get("run_id")
        definition_id = manifest.get("definition_id")
        if not isinstance(run_id, str) or not isinstance(definition_id, str):
            return
        registry = path.parents[2] / "registry.sqlite3"
        with closing(sqlite3.connect(registry)) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    definition_id TEXT NOT NULL,
                    fleet_path TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    runtime_generation TEXT NOT NULL,
                    manifest_path TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """INSERT INTO runs (
                    run_id, definition_id, fleet_path, config_hash, phase,
                    runtime_generation, manifest_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    phase=excluded.phase,
                    runtime_generation=excluded.runtime_generation,
                    manifest_path=excluded.manifest_path,
                    updated_at=CURRENT_TIMESTAMP""",
                (
                    run_id, definition_id, str(manifest.get("fleet_path") or ""),
                    str(manifest.get("fleet_source_hash") or ""),
                    str(manifest.get("phase") or ""),
                    str(manifest.get("runtime_generation") or ""), str(path),
                ),
            )
        registry.chmod(0o600)

    @staticmethod
    def _mark_run_removed(state_dir: Path, run_id: str) -> None:
        registry = state_dir / "registry.sqlite3"
        if not registry.exists():
            return
        with closing(sqlite3.connect(registry)) as connection, connection:
            connection.execute(
                "UPDATE runs SET phase='removed', manifest_path=NULL, "
                "updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,)
            )

    @staticmethod
    def _new_run_id(definition_id: str) -> str:
        return f"{definition_id}-{uuid.uuid4().hex}"

    @staticmethod
    def _instantiate_run(resolved: ResolvedFleet, run_id: str) -> ResolvedFleet:
        if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", run_id) is None:
            raise FleetRuntimeError(f"run ID is unsafe: {run_id}")
        fleet_source = json.loads(json.dumps(resolved.fleet_source))
        fleet = json.loads(json.dumps(resolved.fleet))
        fleet_source["metadata"]["id"] = run_id
        fleet["metadata"]["id"] = run_id
        return ResolvedFleet(
            run_id, resolved.fleet_path, fleet_source, fleet,
            resolved.profile_ref, resolved.profile_path, resolved.profile,
            resolved.codex_hook_trust, resolved.fleet_source_hash,
            resolved.role_catalog, resolved.role_catalog_hash,
            definition_id=resolved.fleet_id,
        )

    def runs(
        self, state_dir: Path, definition_id: str | None = None
    ) -> list[dict[str, Any]]:
        registry = state_dir / "registry.sqlite3"
        if not registry.is_file():
            return []
        query = (
            "SELECT run_id, definition_id, fleet_path, config_hash, phase, "
            "runtime_generation, manifest_path, created_at, updated_at FROM runs"
        )
        parameters: tuple[str, ...] = ()
        if definition_id is not None:
            query += " WHERE definition_id=?"
            parameters = (definition_id,)
        query += " ORDER BY created_at, run_id"
        with closing(sqlite3.connect(registry)) as connection:
            rows = connection.execute(query, parameters).fetchall()
        keys = ("run_id", "definition_id", "fleet_path", "config_hash", "phase",
                "runtime_generation", "manifest_path", "created_at", "updated_at")
        return [dict(zip(keys, row)) for row in rows]

    def _materialize_hook_runtime(
        self, fleet_state_dir: Path, payload: bytes
    ) -> tuple[Path, str]:
        digest = hashlib.sha256(payload).hexdigest()
        root = self._prepare_private_runtime_directory(
            fleet_state_dir, "hook-runtimes", "hook runtime"
        )
        raw_version_dir = root / digest
        if raw_version_dir.is_symlink():
            raise FleetRuntimeError(
                "hook runtime version directory must not be a symbolic link"
            )
        version_dir = raw_version_dir.resolve()
        if version_dir.parent != root:
            raise FleetRuntimeError("hook runtime identity escapes the Fleet state directory")
        target = version_dir / "role_context.py"
        if not version_dir.exists():
            staging = root / f".{digest}-{uuid.uuid4().hex}.tmp"
            staging.mkdir(mode=0o700)
            temporary = staging / "role_context.py"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(0o400)
                staging.chmod(0o500)
                staging.replace(version_dir)
            except Exception:
                if staging.exists():
                    staging.chmod(0o700)
                    temporary.unlink(missing_ok=True)
                    staging.rmdir()
                raise
        self._validate_hook_runtime(fleet_state_dir, target, digest)
        return target, digest

    @staticmethod
    def _write_fixed_snapshot(path: Path, document: Mapping[str, Any]) -> None:
        payload = (
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if path.is_symlink():
            raise FleetRuntimeError("configuration snapshot must not be a symbolic link")
        if path.exists():
            if path.read_bytes() != payload:
                raise FleetRuntimeError(
                    "configuration snapshot content does not match its composition identity"
                )
            return
        temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        path.chmod(0o600)

    def _configuration_snapshot_paths(
        self, resolved: ResolvedFleet, fleet_state_dir: Path
    ) -> tuple[Path, Path | None]:
        snapshot_base = fleet_state_dir / "config-snapshots"
        if snapshot_base.is_symlink():
            raise FleetRuntimeError(
                "configuration snapshot directory must not be a symbolic link"
            )
        snapshot_root = snapshot_base / resolved.composition_hash
        role_path = (
            snapshot_root / "role-catalog.json"
            if resolved.role_catalog is not None
            else None
        )
        return snapshot_root / "fleet.json", role_path

    def _materialize_configuration_snapshots(
        self, resolved: ResolvedFleet, fleet_path: Path, role_path: Path | None
    ) -> None:
        self._write_fixed_snapshot(fleet_path, resolved.fleet_source)
        if role_path is not None and resolved.role_catalog is not None:
            self._write_fixed_snapshot(role_path, resolved.role_catalog)

    def _validate_hook_runtime(
        self, fleet_state_dir: Path, path: Path, expected_digest: str
    ) -> Path:
        root = self._prepare_private_runtime_directory(
            fleet_state_dir,
            "hook-runtimes",
            "hook runtime",
            create=False,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise FleetRuntimeError("runtime manifest has an invalid hook hash")
        if path.is_symlink() or not path.is_file():
            raise FleetRuntimeError("materialized hook runtime is missing or unsafe")
        resolved = path.resolve()
        expected = root / expected_digest / "role_context.py"
        if resolved != expected:
            raise FleetRuntimeError("materialized hook runtime escapes Fleet state")
        try:
            payload = resolved.read_bytes()
            metadata = resolved.stat()
            directory_metadata = resolved.parent.stat()
            entries = list(resolved.parent.iterdir())
        except OSError as exc:
            raise FleetRuntimeError(f"cannot validate materialized hook runtime: {exc}") from exc
        if (
            metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o7777 != 0o400
            or directory_metadata.st_uid != os.getuid()
            or directory_metadata.st_mode & 0o7777 != 0o500
            or metadata.st_nlink != 1
            or entries != [resolved]
        ):
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
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if not execute:
            return self.plan(
                fleet_name, fleet_dirs, profile_dirs, state_dir, cwd, agent_kind
            )
        definition_id = self._requested_fleet_id(fleet_name)
        run_id = run_id or self._new_run_id(definition_id)
        if self._manifest_path(state_dir, run_id).exists():
            raise FleetRuntimeError(
                f"run {run_id!r} already exists; use resume {run_id}"
            )
        with self._hold_process_lock(
            state_dir, run_id,
            timeout_seconds=0,
            timeout_message=f"run {run_id!r} is already being started",
        ):
            hook_payload = self._capture_hook_source()
            with self._capture_execution_bundle(hook_payload) as temporary_bundle:
                execution_identity = temporary_bundle.source_identity
                snapshot_runtime = self._with_execution_bundle(temporary_bundle)
                definition = snapshot_runtime.resolve(
                    fleet_name, fleet_dirs, profile_dirs, state_dir
                )
                if definition.fleet_id != definition_id:
                    raise FleetRuntimeError(
                        "resolved Fleet identity differs from the requested identity"
                    )
                resolved = self._instantiate_run(definition, run_id)
                self._raise_if_stop_requested(state_dir, resolved.fleet_id)
                self._assert_config_snapshot(resolved)
                self._raise_if_stop_requested(state_dir, resolved.fleet_id)
                with self._hold_fleet_lock(
                    state_dir,
                    resolved.fleet_id,
                    timeout_seconds=0,
                    timeout_message=(
                        f"Fleet {resolved.fleet_id!r} already has an active runtime process"
                    ),
                ):
                    self._assert_config_snapshot(resolved)
                    self._raise_if_stop_requested(state_dir, resolved.fleet_id)
                    snapshot_runtime._validate_composition(
                        resolved, state_dir, cwd, agent_kind
                    )
                    runtime_preflight = snapshot_runtime._preflight_runtime(
                        resolved, cwd
                    )
                    self._assert_config_snapshot(resolved)
                    self._raise_if_stop_requested(state_dir, resolved.fleet_id)
                    published_bundle = self._publish_execution_bundle(
                        temporary_bundle,
                        self._fleet_state_dir(state_dir, resolved.fleet_id),
                        hook_payload,
                    )
                    stable_runtime = self._with_execution_bundle(published_bundle)
                    try:
                        stable_runtime._plan_resolved(
                            resolved,
                            state_dir,
                            cwd,
                            agent_kind,
                            hook_sha256=str(execution_identity["hook_sha256"]),
                        )
                        self._raise_if_stop_requested(
                            state_dir, resolved.fleet_id
                        )
                        return stable_runtime._start_locked(
                            state_dir,
                            cwd,
                            agent_kind,
                            execution_identity,
                            hook_payload,
                            resolved,
                            runtime_preflight,
                            published_bundle,
                            once=once,
                            poll_seconds=poll_seconds,
                        )
                    except Exception:
                        manifest_path = self._manifest_path(
                            state_dir, resolved.fleet_id
                        )
                        if manifest_path.exists():
                            try:
                                failed = _load_document(manifest_path)
                                self._write_manifest(manifest_path, failed, "failed")
                            except FleetRuntimeError:
                                pass
                        else:
                            self._discard_uncommitted_execution_bundle(
                                published_bundle
                            )
                        raise

    def _start_locked(
        self,
        state_dir: Path,
        cwd: str,
        agent_kind: str,
        execution_identity: Mapping[str, Any],
        hook_payload: bytes,
        resolved: ResolvedFleet,
        runtime_preflight: Mapping[str, Any],
        execution_bundle: ExecutionBundle,
        *,
        once: bool = False,
        poll_seconds: float = 0.25,
    ) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, resolved.fleet_id)
        fleet_state_dir = self._fleet_state_dir(state_dir, resolved.fleet_id)
        fleet_snapshot_path, role_catalog_snapshot_path = (
            self._configuration_snapshot_paths(resolved, fleet_state_dir)
        )
        desired = {
            "manifest_format_version": MANIFEST_FORMAT_VERSION,
            "run_id": resolved.fleet_id,
            "definition_id": resolved.definition_id,
            "fleet_id": resolved.fleet_id,
            "fleet_path": str(resolved.fleet_path),
            "fleet_hash": resolved.fleet_hash,
            "fleet_source_hash": resolved.fleet_source_hash,
            "profile_ref": resolved.profile_ref,
            "profile_path": str(resolved.profile_path),
            "profile_hash": resolved.profile_hash,
            "composition_hash": resolved.composition_hash,
            "cwd": str(Path(cwd).resolve()),
            "member_runtimes": {
                member["agent_ref"]: dict(member["runtime"])
                for member in resolved.fleet["spec"]["members"]
            },
            "runtime_preflight": dict(runtime_preflight),
            "fleet_snapshot_path": str(fleet_snapshot_path),
            "execution_snapshot_root": str(execution_bundle.root),
            "runtime_commands": execution_bundle.commands,
        }
        if self.role_catalog is not None:
            desired["role_catalog_path"] = str(self.role_catalog.resolve())
            desired["role_catalog_hash"] = resolved.role_catalog_hash
            desired["role_catalog_snapshot_path"] = str(
                role_catalog_snapshot_path
            )
        phase = "planned"
        runtime_generation = uuid.uuid4().hex
        if manifest_path.exists():
            raise FleetRuntimeError(
                f"run {resolved.fleet_id!r} already exists; use resume"
            )
        self._materialize_configuration_snapshots(
            resolved, fleet_snapshot_path, role_catalog_snapshot_path
        )
        hook_runtime, hook_sha256 = self._materialize_hook_runtime(
            fleet_state_dir, hook_payload
        )
        runtime_manifest = {
            **desired,
            "execution_identity": dict(execution_identity),
            "runtime_generation": runtime_generation,
            "hook_runtime": str(hook_runtime),
            "hook_sha256": hook_sha256,
        }
        self._write_manifest(manifest_path, runtime_manifest, phase)
        core_db = fleet_state_dir / "core.sqlite3"
        herdr_db = fleet_state_dir / "herdr.sqlite3"
        phases = ["planned", "core_provisioned", "herdr_provisioned", "active"]
        if phase not in phases:
            raise FleetRuntimeError(f"unknown runtime phase: {phase}")
        if phases.index(phase) < phases.index("core_provisioned"):
            self._raise_if_stop_requested(state_dir, resolved.fleet_id)
            self._run_json(
                [
                    *self.core_command,
                    "--db",
                    str(core_db),
                    "fleet.provision",
                    "--config",
                    str(fleet_snapshot_path),
                    *(
                        ["--role-catalog", str(role_catalog_snapshot_path)]
                        if role_catalog_snapshot_path is not None
                        else []
                    ),
                ],
                "Core fleet provision",
            )
            self._raise_if_stop_requested(state_dir, resolved.fleet_id)
            phase = "core_provisioned"
            self._write_manifest(manifest_path, runtime_manifest, phase)
        provisioned: Mapping[str, Any] = {"status": "already_provisioned"}
        if phases.index(phase) < phases.index("herdr_provisioned"):
            self._raise_if_stop_requested(state_dir, resolved.fleet_id)
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
            self._raise_if_stop_requested(state_dir, resolved.fleet_id)
            phase = "herdr_provisioned"
            self._write_manifest(manifest_path, runtime_manifest, phase)
        spec = resolved.fleet["spec"]
        manager_ref = spec["collaboration"]["manager"]
        for task in spec["tasks"]:
            if task.get("depends_on"):
                continue
            self._raise_if_stop_requested(state_dir, resolved.fleet_id)
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
            self._raise_if_stop_requested(state_dir, resolved.fleet_id)
        for member in spec["members"]:
            self._raise_if_stop_requested(state_dir, resolved.fleet_id)
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
            if agent_ref == manager_ref:
                control["monitoring"] = {
                    "action": "task.list",
                    "prohibited_methods": [
                        "sqlite-direct",
                        "external-json-filter",
                    ],
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
            self._raise_if_stop_requested(state_dir, resolved.fleet_id)
        self._raise_if_stop_requested(state_dir, resolved.fleet_id)
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

    def resume(
        self, run_id: str, state_dir: Path, *, once: bool = False,
        poll_seconds: float = 0.25,
    ) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, run_id)
        if not manifest_path.is_file():
            raise FleetRuntimeError(f"runtime manifest not found: {run_id}")
        with self._hold_process_lock(
            state_dir, run_id, timeout_seconds=0,
            timeout_message=f"run {run_id!r} already has a controller",
        ):
            manifest = _load_document(manifest_path)
            self._validate_runtime_manifest(manifest)
            if manifest["run_id"] != run_id:
                raise FleetRuntimeError("runtime manifest run identity changed")
            if manifest["phase"] != "active":
                raise FleetRuntimeError(
                    f"run {run_id!r} is {manifest['phase']!r}; only an active run can be resumed"
                )
            bundle = self._execution_bundle_from_manifest(manifest, state_dir, run_id)
            monitor = self._with_execution_bundle(bundle).monitor(
                run_id, state_dir, once=once, poll_seconds=poll_seconds
            )
        return {**manifest, "status": "resumed", "monitor": monitor}

    def stop(
        self, fleet_id: str, state_dir: Path, *, execute: bool = False
    ) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, fleet_id)
        if not execute:
            if not manifest_path.exists():
                return {"run_id": fleet_id, "status": "inactive"}
            manifest = _load_document(manifest_path)
            fleet_id = str(manifest.get("fleet_id") or "")
            if not fleet_id:
                raise FleetRuntimeError("runtime manifest has no fleet_id")
            return {
                "run_id": fleet_id,
                "fleet_id": fleet_id,
                "status": "planned",
                "action": "stop",
            }
        with self._publish_stop_request(state_dir, fleet_id):
            with self._hold_process_lock(
                state_dir,
                fleet_id,
                timeout_seconds=30,
                timeout_message=(
                    f"Fleet {fleet_id!r} did not stop within 30 seconds"
                ),
            ):
                if not manifest_path.exists():
                    return {"run_id": fleet_id, "status": "inactive"}
                manifest = _load_document(manifest_path)
                fleet_id = str(manifest.get("fleet_id") or "")
                if not fleet_id:
                    raise FleetRuntimeError("runtime manifest has no fleet_id")
                with self._hold_fleet_lock(
                    state_dir,
                    fleet_id,
                    timeout_seconds=30,
                    timeout_message=(
                        f"Fleet {fleet_id!r} controller did not stop within 30 seconds"
                    ),
                ):
                    if not manifest_path.exists():
                        return {"run_id": fleet_id, "status": "inactive"}
                    locked_manifest = _load_document(manifest_path)
                    locked_fleet_id = str(locked_manifest.get("fleet_id") or "")
                    if locked_fleet_id != fleet_id:
                        raise FleetRuntimeError(
                            "runtime manifest Fleet identity changed while stopping"
                        )
                    bundle = self._execution_bundle_from_manifest(
                        locked_manifest, state_dir, fleet_id
                    )
                    stable_runtime = self._with_execution_bundle(bundle)
                    phase = str(locked_manifest.get("phase") or "active")
                    if phase == "removing":
                        raise FleetRuntimeError(
                            "Fleet removal is incomplete; rerun remove instead of stop"
                        )
                    if phase == "stopped":
                        herdr = {"status": "already_stopped", "idempotent": True}
                    else:
                        stopping_manifest = {
                            **locked_manifest,
                            "stop_from_phase": (
                                locked_manifest.get("stop_from_phase")
                                if phase == "stopping"
                                else phase
                            ),
                        }
                        self._write_manifest(
                            manifest_path, stopping_manifest, "stopping"
                        )
                        herdr = stable_runtime._stop_locked(
                            fleet_id,
                            state_dir,
                            manifest_path,
                            stopping_manifest,
                        )
        return {
            "run_id": fleet_id,
            "fleet_id": fleet_id,
            "status": "stopped",
            "herdr": dict(herdr),
        }

    def _stop_locked(
        self,
        fleet_id: str,
        state_dir: Path,
        manifest_path: Path,
        manifest: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        fleet_state = self._fleet_state_dir(state_dir, fleet_id)
        core_db = fleet_state / "core.sqlite3"
        stop_from_phase = str(
            manifest.get("stop_from_phase") or manifest.get("phase") or "active"
        )
        if core_db.exists() and stop_from_phase in {
            "core_provisioned",
            "herdr_provisioned",
            "active",
            "stopping",
        }:
            self._run_json(
                [
                    *self.core_command,
                    "--db",
                    str(core_db),
                    "context.invalidate",
                    "--fleet",
                    fleet_id,
                    "--operation-id",
                    f"runtime-stop:{fleet_id}:{manifest['runtime_generation']}",
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
        return herdr

    def remove(
        self, fleet_id: str, state_dir: Path, *, execute: bool = False
    ) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, fleet_id)
        if not execute:
            return {
                "run_id": fleet_id,
                "fleet_id": fleet_id,
                "status": "planned",
                "action": "remove",
            }
        with self._publish_stop_request(state_dir, fleet_id):
            with self._hold_process_lock(
                state_dir,
                fleet_id,
                timeout_seconds=30,
                timeout_message=(
                    f"Fleet {fleet_id!r} did not stop within 30 seconds"
                ),
            ):
                if not manifest_path.exists():
                    return {"run_id": fleet_id, "status": "inactive"}
                manifest = _load_document(manifest_path)
                fleet_id = str(manifest.get("fleet_id") or "")
                if not fleet_id:
                    raise FleetRuntimeError("runtime manifest has no fleet_id")
                with self._hold_fleet_lock(
                    state_dir,
                    fleet_id,
                    timeout_seconds=30,
                    timeout_message=(
                        f"Fleet {fleet_id!r} controller did not stop within 30 seconds"
                    ),
                ):
                    if not manifest_path.exists():
                        return {"run_id": fleet_id, "status": "inactive"}
                    locked_manifest = _load_document(manifest_path)
                    locked_fleet_id = str(locked_manifest.get("fleet_id") or "")
                    if locked_fleet_id != fleet_id:
                        raise FleetRuntimeError(
                            "runtime manifest Fleet identity changed while removing"
                        )
                    bundle = self._execution_bundle_from_manifest(
                        locked_manifest, state_dir, fleet_id
                    )
                    stable_runtime = self._with_execution_bundle(bundle)
                    phase = str(locked_manifest.get("phase") or "active")
                    if phase not in {"stopped", "removing"}:
                        stopping_manifest = {
                            **locked_manifest,
                            "stop_from_phase": (
                                locked_manifest.get("stop_from_phase")
                                if phase == "stopping"
                                else phase
                            ),
                        }
                        self._write_manifest(
                            manifest_path, stopping_manifest, "stopping"
                        )
                        herdr = stable_runtime._stop_locked(
                            fleet_id,
                            state_dir,
                            manifest_path,
                            stopping_manifest,
                        )
                    else:
                        herdr = {"status": "already_stopped", "idempotent": True}
                    stopped = {
                        "fleet_id": fleet_id,
                        "status": "stopped",
                        "herdr": dict(herdr),
                    }
                    self._write_manifest(manifest_path, locked_manifest, "removing")
                    core_db = (
                        self._fleet_state_dir(state_dir, fleet_id)
                        / "core.sqlite3"
                    )
                    if core_db.exists() and core_db.stat().st_size > 0:
                        core = stable_runtime._run_json(
                            [
                                *stable_runtime.core_command,
                                "--db",
                                str(core_db),
                                "fleet.remove",
                                "--fleet",
                                fleet_id,
                                "--confirm-fleet",
                                fleet_id,
                            ],
                            "Core fleet removal",
                        )
                    else:
                        core = {
                            "fleet_id": fleet_id,
                            "status": "absent",
                            "idempotent": True,
                        }
                    if manifest_path.exists():
                        manifest_path.unlink()
                    run_state = self._fleet_state_dir(state_dir, fleet_id)
                    if run_state.exists():
                        self._remove_execution_tree(run_state)
                    self._mark_run_removed(state_dir, fleet_id)
        return {
            "run_id": fleet_id,
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
        for path in [
            *fleet_dirs,
            *profile_dirs,
            state_dir,
        ]:
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
        for label, paths in (
            ("fleet_dirs", fleet_dirs),
            ("profile_dirs", profile_dirs),
        ):
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
        checks.append(
            {
                "check": "role_catalog",
                "ok": self.role_catalog is not None and self.role_catalog.is_file(),
                "path": str(self.role_catalog) if self.role_catalog is not None else None,
            }
        )
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
        manifest_path = self._manifest_path(state_dir, fleet_id)
        if not manifest_path.exists():
            raise FleetRuntimeError(f"runtime manifest not found: {fleet_id}")
        manifest = _load_document(manifest_path)
        fleet_id = str(manifest.get("fleet_id") or "")
        if not fleet_id:
            raise FleetRuntimeError("runtime manifest has no fleet_id")
        execution_bundle = self._execution_bundle_from_manifest(
            manifest, state_dir, fleet_id
        )
        execution_runtime = self._with_execution_bundle(execution_bundle)
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
                if self._stop_requested(state_dir, fleet_id):
                    stopping = True
                    break
                if manifest_path.exists():
                    phase = _load_document(manifest_path).get("phase")
                    if phase in {"stopping", "stopped"}:
                        stopping = True
                        break
                try:
                    result = execution_runtime._run_json(
                        [
                            *execution_runtime.controller_command,
                            "--core-command",
                            execution_runtime.core_command[0],
                            "--herdr-command",
                            execution_runtime.herdr_command[0],
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
            return {"run_id": fleet_id, "status": "inactive"}
        manifest = _load_document(manifest_path)
        fleet_id = str(manifest.get("fleet_id") or "")
        if not fleet_id:
            raise FleetRuntimeError("runtime manifest has no fleet_id")
        execution_bundle = self._execution_bundle_from_manifest(
            manifest, state_dir, fleet_id
        )
        phase = str(manifest["phase"])
        if phase in {
            "planned",
            "core_provisioned",
            "herdr_provisioned",
            "stopping",
            "stopped",
            "removing",
            "failed",
        }:
            return {
                "run_id": fleet_id,
                "fleet_id": fleet_id,
                "status": phase,
                "recovery_required": phase in {"stopping", "removing"},
                "configuration": dict(manifest),
            }
        execution_runtime = self._with_execution_bundle(execution_bundle)
        drift = False
        for path_key, hash_key in (
            ("fleet_path", "fleet_source_hash"),
            ("profile_path", "profile_hash"),
            ("role_catalog_path", "role_catalog_hash"),
        ):
            configured_path = manifest.get(path_key)
            if not isinstance(configured_path, str) or not Path(configured_path).is_file():
                drift = True
                continue
            drift = drift or _content_hash(_load_document(Path(configured_path))) != manifest.get(
                hash_key
            )
        core = execution_runtime._run_json(
            [
                *execution_runtime.core_command,
                "--db",
                str(self._fleet_state_dir(state_dir, fleet_id) / "core.sqlite3"),
                "status",
                "--fleet",
                fleet_id,
            ],
            "Core fleet status",
        )
        herdr = execution_runtime._run_json(
            [
                *execution_runtime.herdr_command,
                "--state-db",
                str(self._fleet_state_dir(state_dir, fleet_id) / "herdr.sqlite3"),
                "status",
                "--fleet",
                fleet_id,
            ],
            "Herdr fleet status",
        )
        return {
            "run_id": fleet_id,
            "fleet_id": fleet_id,
            "status": "configuration_drift" if drift else "active",
            "configuration": dict(manifest),
            "core": dict(core),
            "herdr": dict(herdr),
        }


def _default_state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    return Path(root) / "agent-fleet" if root else Path.home() / ".local/state/agent-fleet"


def _default_role_catalog() -> Path | None:
    configured = os.environ.get("AGENT_ROLES_CATALOG")
    if configured:
        return Path(configured)
    default = Path.home() / ".config/agent-roles/catalogs/builtin@1.json"
    return default if default.is_file() else None


def build_parser() -> argparse.ArgumentParser:
    adapter_root = Path(__file__).resolve().parent
    core_default = os.environ.get("AGENT_FLEET_CORE_COMMAND") or shutil.which(
        "fleet-control"
    ) or "fleet-control"
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fleet-dir", type=Path, action="append")
    common.add_argument("--profile-dir", type=Path, action="append", default=[])
    common.add_argument("--state-dir", type=Path, default=_default_state_dir())
    common.add_argument(
        "--role-catalog",
        type=Path,
        default=_default_role_catalog(),
    )
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
    plan.add_argument(
        "fleet",
        help="absolute Fleet YAML path",
    )
    plan.add_argument("--cwd", default=str(Path.cwd()))
    plan.add_argument(
        "--agent-kind",
        choices=["codex", "claude"],
        default="codex",
        help=argparse.SUPPRESS,
    )
    start = sub.add_parser("start", parents=[common])
    start.add_argument(
        "fleet",
        help="absolute Fleet YAML path",
    )
    start.add_argument("--cwd", default=str(Path.cwd()))
    start.add_argument(
        "--agent-kind",
        choices=["codex", "claude"],
        default="codex",
        help=argparse.SUPPRESS,
    )
    start.add_argument("--execute", action="store_true")
    start.add_argument(
        "--run-id",
        help="explicit unique run ID (normally generated automatically)",
    )
    start.add_argument("--once", action="store_true")
    start.add_argument("--poll-seconds", type=float, default=0.25)
    runs = sub.add_parser("runs", parents=[common])
    runs.add_argument("definition_id", nargs="?")
    resume = sub.add_parser("resume", parents=[common])
    resume.add_argument("run_id")
    resume.add_argument("--once", action="store_true")
    resume.add_argument("--poll-seconds", type=float, default=0.25)
    status = sub.add_parser("status", parents=[common])
    status.add_argument("run_id")
    stop = sub.add_parser("stop", parents=[common])
    stop.add_argument("run_id")
    stop.add_argument("--execute", action="store_true")
    remove = sub.add_parser("remove", parents=[common])
    remove.add_argument("run_id")
    remove.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_root = Path.home() / ".config" / "agent-fleet"
    fleet_dirs = args.fleet_dir or [config_root / "fleets"]
    profile_dirs = args.profile_dir or [config_root / "view-profiles"]
    try:
        if args.action in {"list", "plan", "start"} and args.role_catalog is None:
            raise FleetRuntimeError(
                "Role Catalog is required: install/export agent-roles builtin@1, "
                "pass --role-catalog, or set AGENT_ROLES_CATALOG"
            )
        runtime = FleetRuntime(
            [args.core_command],
            [args.herdr_command],
            [args.controller_command],
            role_catalog=args.role_catalog,
        )
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
                run_id=args.run_id,
            )
        elif args.action == "runs":
            result = runtime.runs(args.state_dir, args.definition_id)
        elif args.action == "resume":
            result = runtime.resume(
                args.run_id, args.state_dir, once=args.once,
                poll_seconds=args.poll_seconds,
            )
        elif args.action == "status":
            result = runtime.status(args.run_id, args.state_dir)
        elif args.action == "stop":
            result = runtime.stop(args.run_id, args.state_dir, execute=args.execute)
        else:
            result = runtime.remove(args.run_id, args.state_dir, execute=args.execute)
    except (FleetRuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
