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
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TextIO

from agent_command_profiles import (
    AgentCommandProfileCatalog,
    AgentCommandProfileError,
)
from launch_profiles import LaunchProfileCatalog, LaunchProfileError


from runtime_models import (Runner, DEFAULT_HOOK_SOURCE, MANIFEST_FORMAT_VERSION, RUNTIME_PHASES,
                            FleetRuntimeError, ExecutionBundle, ResolvedFleet, _content_hash, _load_document)
from execution_identity import ExecutionIdentity


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
        launch_dirs: Sequence[Path] = (),
        agent_command_profile_dirs: Sequence[Path] = (),
        allow_legacy_fleet: bool = False,
    ):
        self.core_command = tuple(core_command)
        self.herdr_command = tuple(herdr_command)
        self.controller_command = tuple(controller_command)
        self.runner = runner
        self.sleeper = sleeper
        self.hook_source = hook_source
        self.role_catalog = role_catalog
        self.launch_dirs = tuple(launch_dirs)
        self.agent_command_profile_dirs = tuple(agent_command_profile_dirs)
        self.allow_legacy_fleet = allow_legacy_fleet

    def _launch_roots(self, fleet_dirs: Sequence[Path]) -> tuple[Path, ...]:
        if self.launch_dirs:
            return self.launch_dirs
        return tuple(path.parent / "herdr-launch-profiles" for path in fleet_dirs)

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
                "an interactive shell is required to resolve AgentCommandProfile commands"
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
                "AgentCommandProfile aliases require an executable /bin or /usr/bin "
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
            launch_dirs=self.launch_dirs,
            agent_command_profile_dirs=self.agent_command_profile_dirs,
            allow_legacy_fleet=self.allow_legacy_fleet,
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


    def _assert_no_other_active_launch(
        self, resolved: ResolvedFleet, state_dir: Path
    ) -> None:
        runtime_root = state_dir / "runtimes"
        if not runtime_root.is_dir():
            return
        current_path = self._manifest_path(state_dir, resolved.launch_id)
        for manifest_path in sorted(runtime_root.glob("*.json"), key=str):
            if manifest_path.resolve() == current_path:
                continue
            manifest = _load_document(manifest_path)
            if (
                manifest.get("fleet_id") == resolved.fleet_id
                and manifest.get("phase") != "stopped"
            ):
                other_launch = str(
                    manifest.get("launch_id") or manifest_path.stem
                )
                raise FleetRuntimeError(
                    f"Fleet {resolved.fleet_id!r} already has active LaunchProfile "
                    f"{other_launch!r}; stop it before starting {resolved.launch_id!r}"
                )

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

    @staticmethod
    def _profile_identity(profile: Mapping[str, Any], path: Path) -> str:
        if (
            profile.get("apiVersion")
            not in {"fleet.herdr.harness/v1", "fleet.herdr.harness/v2"}
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

    def _resolve_agent_command_profiles(
        self,
        launch: Mapping[str, Any],
        fleet: Mapping[str, Any],
    ) -> tuple[dict[str, dict[str, str]], tuple[dict[str, str], ...]]:
        launch_spec = launch.get("spec")
        requested = (
            launch_spec.get("agent_command_profiles", {})
            if isinstance(launch_spec, Mapping)
            else {}
        )
        if not isinstance(requested, Mapping):
            raise FleetRuntimeError(
                "LaunchProfile agent_command_profiles must be an object"
            )
        if not requested:
            return {}, ()
        try:
            catalog = AgentCommandProfileCatalog.from_directories(
                self.agent_command_profile_dirs
            )
        except AgentCommandProfileError as exc:
            raise FleetRuntimeError(str(exc)) from exc
        fleet_spec = fleet.get("spec")
        members = (
            fleet_spec.get("members") if isinstance(fleet_spec, Mapping) else None
        )
        member_products = {
            member.get("agent_ref"): member.get("runtime", {}).get("product")
            for member in members or []
            if isinstance(member, Mapping)
            and isinstance(member.get("runtime"), Mapping)
        }
        resolved: dict[str, dict[str, str]] = {}
        sources: dict[str, dict[str, str]] = {}
        for agent_ref, profile_ref in requested.items():
            if agent_ref not in member_products:
                raise FleetRuntimeError(
                    f"LaunchProfile AgentCommandProfile targets unknown Fleet member: {agent_ref}"
                )
            try:
                path, document = catalog.resolve(str(profile_ref))
            except AgentCommandProfileError as exc:
                raise FleetRuntimeError(str(exc)) from exc
            profile_product = str(document["spec"]["product"])
            if profile_product != member_products[agent_ref]:
                raise FleetRuntimeError(
                    f"AgentCommandProfile {profile_ref} product {profile_product!r} "
                    f"does not match Fleet member {agent_ref!r} product "
                    f"{member_products[agent_ref]!r}"
                )
            resolved[str(agent_ref)] = {
                "profile_ref": str(profile_ref),
                "product": profile_product,
                "command": str(document["spec"]["command"]),
            }
            sources[str(profile_ref)] = {
                "profile_ref": str(profile_ref),
                "path": str(path),
                "hash": _content_hash(document),
            }
        return resolved, tuple(sources[key] for key in sorted(sources))

    def resolve(
        self,
        fleet_name: str,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
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
        fleets = self._validated_fleets(
            fleet_dirs,
            state_dir / ".validation-does-not-write.sqlite3",
            role_catalog,
        )
        if (
            self.role_catalog is not None
            and _content_hash(_load_document(self.role_catalog))
            != role_catalog_hash
        ):
            raise FleetRuntimeError(
                f"configuration changed during role catalog validation: {self.role_catalog}"
            )
        profiles = self._profiles(profile_dirs)
        try:
            launch_path, launch = LaunchProfileCatalog.from_directories(
                self._launch_roots(fleet_dirs)
            ).resolve(fleet_name)
        except LaunchProfileError as exc:
            if "not found" not in str(exc):
                raise FleetRuntimeError(str(exc)) from exc
            if not self.allow_legacy_fleet:
                raise FleetRuntimeError(f"LaunchProfile not found: {fleet_name}") from exc
            selected = fleets.get(fleet_name)
            if selected is None:
                raise FleetRuntimeError(f"LaunchProfile not found: {fleet_name}") from exc
            fleet_path, fleet_source, fleet, fleet_source_hash = selected
            spec = fleet.get("spec")
            runtime = spec.get("runtime") if isinstance(spec, Mapping) else None
            view = spec.get("view") if isinstance(spec, Mapping) else None
            profile_ref = view.get("profile_ref") if isinstance(view, Mapping) else None
            if (
                fleet.get("apiVersion") != "fleet.harness/v1"
                or not isinstance(runtime, Mapping)
                or runtime.get("provider") != "herdr"
                or not isinstance(profile_ref, str)
                or not profile_ref
            ):
                raise FleetRuntimeError(f"LaunchProfile not found: {fleet_name}") from exc
            launch = {
                "apiVersion": "fleet.herdr.harness/v1",
                "kind": "LaunchProfile",
                "metadata": {"id": fleet_name},
                "spec": {
                    "fleet_ref": fleet_name,
                    "view_profile_ref": profile_ref,
                    "codex_hook_trust": runtime.get("codex_hook_trust", "review"),
                },
            }
            launch_path = None
            legacy = True
        else:
            launch_spec = launch["spec"]
            fleet_ref = launch_spec["fleet_ref"]
            selected = fleets.get(fleet_ref)
            if selected is None:
                raise FleetRuntimeError(
                    f"Fleet not found for LaunchProfile {fleet_name}: {fleet_ref}"
                )
            fleet_path, fleet_source, fleet, fleet_source_hash = selected
            spec = fleet.get("spec")
            if isinstance(spec, Mapping) and ("runtime" in spec or "view" in spec):
                raise FleetRuntimeError(
                    "LaunchProfile cannot be combined with legacy Fleet runtime/view fields"
                )
            profile_ref = launch_spec["view_profile_ref"]
            legacy = False
        resolved_profile = profiles.get(profile_ref)
        if resolved_profile is None:
            raise FleetRuntimeError(f"ViewProfile not found: {profile_ref}")
        profile_path, profile = resolved_profile
        agent_command_profiles, agent_command_profile_sources = (
            self._resolve_agent_command_profiles(launch, fleet)
        )
        metadata = fleet["metadata"]
        return ResolvedFleet(
            fleet_name,
            launch_path,
            launch,
            str(metadata["id"]),
            fleet_path,
            fleet_source,
            fleet,
            profile_ref,
            profile_path,
            profile,
            str(launch["spec"]["codex_hook_trust"]),
            fleet_source_hash,
            role_catalog,
            role_catalog_hash,
            agent_command_profiles,
            agent_command_profile_sources,
            legacy=legacy,
        )

    def list_configs(
        self,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
        state_dir: Path,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        catalog = LaunchProfileCatalog.from_directories(
            self._launch_roots(fleet_dirs)
        )
        launch_ids = [identity for identity, _, _ in catalog.entries()]
        fleets = self._validated_fleets(
            fleet_dirs,
            state_dir / ".validation-does-not-write.sqlite3",
            (
                _load_document(self.role_catalog)
                if self.role_catalog is not None
                else None
            ),
        )
        for fleet_id, (_, _, fleet, _) in sorted(fleets.items()):
            spec = fleet.get("spec")
            if (
                self.allow_legacy_fleet
                and
                fleet.get("apiVersion") == "fleet.harness/v1"
                and isinstance(spec, Mapping)
                and isinstance(spec.get("runtime"), Mapping)
                and isinstance(spec.get("view"), Mapping)
                and fleet_id not in launch_ids
            ):
                launch_ids.append(fleet_id)
        for launch_id in sorted(launch_ids):
            resolved = self.resolve(
                launch_id, fleet_dirs, profile_dirs, state_dir
            )
            spec = resolved.fleet["spec"]
            self._run_json(
                [
                    *self.herdr_command,
                    "--state-db",
                    str(state_dir / ".list-does-not-write.sqlite3"),
                    "provision",
                    "--fleet-json",
                    json.dumps(resolved.fleet, ensure_ascii=False, sort_keys=True),
                    "--launch-profile-json",
                    json.dumps(
                        resolved.launch_profile,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "--agent-command-profiles-json",
                    json.dumps(
                        resolved.agent_command_profiles,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "--view-profile-json",
                    json.dumps(resolved.profile, ensure_ascii=False, sort_keys=True),
                    "--cwd",
                    str(resolved.fleet_path.parent),
                    "--agent-kind",
                    "codex",
                ],
                f"Launch composition validation ({launch_id})",
            )
            rows.append(
                {
                    "launch_id": launch_id,
                    "fleet_id": resolved.fleet_id,
                    "path": str(resolved.fleet_path),
                    "objective": spec["objective"],
                    "members": len(spec["members"]),
                    "member_runtimes": {
                        member["agent_ref"]: dict(member["runtime"])
                        for member in spec["members"]
                    },
                    "agent_command_profiles": resolved.agent_command_profiles,
                    "profile_ref": resolved.profile_ref,
                    "profile_resolved": True,
                    "legacy": resolved.legacy,
                    "start_command": shlex.join(
                        [
                            "fleet-runtime",
                            "start",
                            launch_id,
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
        fleet_state_dir = self._fleet_state_dir(state_dir, resolved.launch_id)
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
                "--launch-profile-json",
                json.dumps(
                    resolved.launch_profile, ensure_ascii=False, sort_keys=True
                ),
                "--agent-command-profiles-json",
                json.dumps(
                    resolved.agent_command_profiles,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
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
            "launch_id": resolved.launch_id,
            "launch_path": (
                str(resolved.launch_path) if resolved.launch_path is not None else None
            ),
            "launch_hash": resolved.launch_hash,
            "legacy": resolved.legacy,
            "fleet_id": resolved.fleet_id,
            "fleet_path": str(resolved.fleet_path),
            "fleet_hash": resolved.fleet_hash,
            "fleet_source_hash": resolved.fleet_source_hash,
            "profile_ref": resolved.profile_ref,
            "profile_path": str(resolved.profile_path),
            "profile_hash": resolved.profile_hash,
            "agent_command_profiles": resolved.agent_command_profiles,
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
        """Validate Fleet/Launch/View composition without creating runtime state."""
        self._run_json(
            [
                *self.herdr_command,
                "--state-db",
                str(state_dir / ".composition-validation-does-not-write.sqlite3"),
                "provision",
                "--fleet-json",
                json.dumps(resolved.fleet, ensure_ascii=False, sort_keys=True),
                "--launch-profile-json",
                json.dumps(
                    resolved.launch_profile, ensure_ascii=False, sort_keys=True
                ),
                "--agent-command-profiles-json",
                json.dumps(
                    resolved.agent_command_profiles,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "--view-profile-json",
                json.dumps(resolved.profile, ensure_ascii=False, sort_keys=True),
                "--cwd",
                cwd,
                "--agent-kind",
                agent_kind,
            ],
            f"Launch composition validation ({resolved.launch_id})",
        )

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
    def _launch_lock_path(state_dir: Path, launch_id: str) -> Path:
        return FleetRuntime._operation_lock_path(state_dir, "launch", launch_id)

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
    def _stop_request_dir(state_dir: Path, launch_id: str) -> Path:
        root = (state_dir / "stop-requests").resolve()
        request_dir = (root / launch_id).resolve()
        if request_dir.parent != root:
            raise FleetRuntimeError("Launch identity escapes the stop request directory")
        return request_dir

    @contextmanager
    def _publish_stop_request(
        self, state_dir: Path, launch_id: str
    ) -> Iterator[Path]:
        request_dir = self._stop_request_dir(state_dir, launch_id)
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

    def _stop_requested(self, state_dir: Path, launch_id: str) -> bool:
        request_dir = self._stop_request_dir(state_dir, launch_id)
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

    def _raise_if_stop_requested(self, state_dir: Path, launch_id: str) -> None:
        if self._stop_requested(state_dir, launch_id):
            raise FleetRuntimeError(
                f"Fleet launch {launch_id!r} was cancelled by a stop request"
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
    def _hold_launch_lock(
        self,
        state_dir: Path,
        launch_id: str,
        *,
        timeout_seconds: float,
        timeout_message: str,
    ) -> Iterator[None]:
        with self._hold_identity_lock(
            self._launch_lock_path(state_dir, launch_id),
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

    @contextmanager
    def _hold_runtime_locks(
        self,
        state_dir: Path,
        launch_id: str,
        fleet_id: str,
        *,
        timeout_seconds: float,
        timeout_message: str,
    ) -> Iterator[None]:
        """Compatibility helper; lifecycle code always locks launch, then Fleet."""
        with self._hold_launch_lock(
            state_dir,
            launch_id,
            timeout_seconds=timeout_seconds,
            timeout_message=timeout_message,
        ):
            with self._hold_fleet_lock(
                state_dir,
                fleet_id,
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
    ) -> dict[str, Any]:
        if not execute:
            return self.plan(
                fleet_name, fleet_dirs, profile_dirs, state_dir, cwd, agent_kind
            )
        state_dir_existed = state_dir.exists()
        with self._hold_launch_lock(
            state_dir,
            fleet_name,
            timeout_seconds=0,
            timeout_message=(
                f"Fleet {fleet_name!r} already has an active runtime process"
            ),
        ):
            known_manifest_path = self._manifest_path(state_dir, fleet_name)
            if known_manifest_path.exists():
                known = _load_document(known_manifest_path)
                self._validate_runtime_manifest(known)
                if known.get("phase") != "stopped":
                    execution_bundle = self._execution_bundle_from_manifest(
                        known, state_dir, fleet_name
                    )
                    if known["phase"] == "stopping":
                        raise FleetRuntimeError(
                            "Fleet stop is incomplete; rerun stop instead of start"
                        )
                    if known["phase"] == "removing":
                        raise FleetRuntimeError(
                            "Fleet removal is incomplete; rerun remove instead of start"
                        )
                    stable_runtime = self._with_execution_bundle(execution_bundle)
                    hook_runtime = Path(str(known["hook_runtime"]))
                    hook_payload = hook_runtime.read_bytes()
                    resolved = stable_runtime.resolve(
                        fleet_name, fleet_dirs, profile_dirs, state_dir
                    )
                    if resolved.launch_id != fleet_name:
                        raise FleetRuntimeError(
                            "resolved LaunchProfile identity differs from the requested identity"
                        )
                    self._raise_if_stop_requested(state_dir, resolved.launch_id)
                    self._assert_no_other_active_launch(resolved, state_dir)
                    self._assert_config_snapshot(resolved)
                    with self._hold_fleet_lock(
                        state_dir,
                        resolved.fleet_id,
                        timeout_seconds=0,
                        timeout_message=(
                            f"Fleet {resolved.fleet_id!r} already has an active runtime process"
                        ),
                    ):
                        self._assert_no_other_active_launch(resolved, state_dir)
                        self._assert_config_snapshot(resolved)
                        self._raise_if_stop_requested(state_dir, resolved.launch_id)
                        stable_runtime._validate_composition(
                            resolved, state_dir, cwd, agent_kind
                        )
                        stable_runtime._preflight_runtime(
                            resolved,
                            cwd,
                            require_codex_registration=(
                                known["phase"] in {"planned", "core_provisioned"}
                            ),
                            require_agent_launch=(
                                known["phase"] in {"planned", "core_provisioned"}
                            ),
                        )
                        runtime_preflight = dict(known["runtime_preflight"])
                        self._assert_config_snapshot(resolved)
                        self._raise_if_stop_requested(state_dir, resolved.launch_id)
                        return stable_runtime._start_locked(
                            fleet_name,
                            fleet_dirs,
                            profile_dirs,
                            state_dir,
                            cwd,
                            agent_kind,
                            known["execution_identity"],
                            hook_payload,
                            resolved,
                            runtime_preflight,
                            execution_bundle,
                            once=once,
                            poll_seconds=poll_seconds,
                        )
            hook_payload = self._capture_hook_source()
            with self._capture_execution_bundle(hook_payload) as temporary_bundle:
                execution_identity = temporary_bundle.source_identity
                snapshot_runtime = self._with_execution_bundle(temporary_bundle)
                self._raise_if_stop_requested(state_dir, fleet_name)
                resolved = snapshot_runtime.resolve(
                    fleet_name, fleet_dirs, profile_dirs, state_dir
                )
                if resolved.launch_id != fleet_name:
                    raise FleetRuntimeError(
                        "resolved LaunchProfile identity differs from the requested identity"
                    )
                self._raise_if_stop_requested(state_dir, resolved.launch_id)
                self._assert_no_other_active_launch(resolved, state_dir)
                self._assert_config_snapshot(resolved)
                self._raise_if_stop_requested(state_dir, resolved.launch_id)
                with self._hold_fleet_lock(
                    state_dir,
                    resolved.fleet_id,
                    timeout_seconds=0,
                    timeout_message=(
                        f"Fleet {resolved.fleet_id!r} already has an active runtime process"
                    ),
                ):
                    self._assert_no_other_active_launch(resolved, state_dir)
                    self._assert_config_snapshot(resolved)
                    self._raise_if_stop_requested(state_dir, resolved.launch_id)
                    snapshot_runtime._validate_composition(
                        resolved, state_dir, cwd, agent_kind
                    )
                    runtime_preflight = snapshot_runtime._preflight_runtime(
                        resolved, cwd
                    )
                    self._assert_config_snapshot(resolved)
                    self._raise_if_stop_requested(state_dir, resolved.launch_id)
                    published_bundle = self._publish_execution_bundle(
                        temporary_bundle,
                        self._fleet_state_dir(state_dir, resolved.launch_id),
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
                            state_dir, resolved.launch_id
                        )
                        return stable_runtime._start_locked(
                            fleet_name,
                            fleet_dirs,
                            profile_dirs,
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
                            state_dir, resolved.launch_id
                        )
                        bundle_committed = False
                        if manifest_path.exists():
                            try:
                                bundle_committed = (
                                    _load_document(manifest_path).get(
                                        "execution_snapshot_root"
                                    )
                                    == str(published_bundle.root)
                                )
                            except FleetRuntimeError:
                                bundle_committed = False
                        if not bundle_committed:
                            self._discard_uncommitted_execution_bundle(
                                published_bundle
                            )
                            if not state_dir_existed:
                                for empty in (state_dir / "fleets", state_dir):
                                    try:
                                        empty.rmdir()
                                    except OSError:
                                        pass
                        raise

    def _start_locked(
        self,
        fleet_name: str,
        fleet_dirs: Sequence[Path],
        profile_dirs: Sequence[Path],
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
        manifest_path = self._manifest_path(state_dir, resolved.launch_id)
        fleet_state_dir = self._fleet_state_dir(state_dir, resolved.launch_id)
        fleet_snapshot_path, role_catalog_snapshot_path = (
            self._configuration_snapshot_paths(resolved, fleet_state_dir)
        )
        desired = {
            "manifest_format_version": MANIFEST_FORMAT_VERSION,
            "launch_id": resolved.launch_id,
            "launch_path": (
                str(resolved.launch_path) if resolved.launch_path is not None else None
            ),
            "launch_hash": resolved.launch_hash,
            "fleet_id": resolved.fleet_id,
            "fleet_path": str(resolved.fleet_path),
            "fleet_hash": resolved.fleet_hash,
            "fleet_source_hash": resolved.fleet_source_hash,
            "profile_ref": resolved.profile_ref,
            "profile_path": str(resolved.profile_path),
            "profile_hash": resolved.profile_hash,
            "composition_hash": resolved.composition_hash,
            "legacy": resolved.legacy,
            "cwd": str(Path(cwd).resolve()),
            "member_runtimes": {
                member["agent_ref"]: dict(member["runtime"])
                for member in resolved.fleet["spec"]["members"]
            },
            "agent_command_profiles": resolved.agent_command_profiles,
            "agent_command_profile_sources": list(
                resolved.agent_command_profile_sources
            ),
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
        restarting = False
        current: Mapping[str, Any] | None = None
        if manifest_path.exists():
            current = _load_document(manifest_path)
            phase = str(current.get("phase") or "active")
            if phase != "stopped":
                # Repeat under both locks to close the early-check window.
                self._assert_runtime_identity(current, execution_identity)
            stable_configuration = {
                key: value
                for key, value in desired.items()
                if phase != "stopped"
                or key not in {
                    "execution_snapshot_root",
                    "runtime_commands",
                    "runtime_preflight",
                }
            }
            if not all(
                current.get(key) == value
                for key, value in stable_configuration.items()
            ):
                raise FleetRuntimeError(
                    "configuration conflict: stop the active Fleet before changing its config"
                )
            runtime_generation = str(
                current.get("runtime_generation") or runtime_generation
            )
            if phase == "stopped":
                phase = "planned"
                runtime_generation = uuid.uuid4().hex
                restarting = True
        self._materialize_configuration_snapshots(
            resolved, fleet_snapshot_path, role_catalog_snapshot_path
        )
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
        if current is None:
            self._write_manifest(
                manifest_path,
                runtime_manifest,
                phase,
            )
        elif phase == "active":
            monitor = self.monitor(
                resolved.launch_id,
                state_dir,
                once=once,
                poll_seconds=poll_seconds,
            )
            return {**runtime_manifest, "status": "resumed", "monitor": monitor}
        elif restarting or not current.get("hook_runtime"):
            self._write_manifest(manifest_path, runtime_manifest, phase)
        core_db = fleet_state_dir / "core.sqlite3"
        herdr_db = fleet_state_dir / "herdr.sqlite3"
        phases = ["planned", "core_provisioned", "herdr_provisioned", "active"]
        if phase not in phases:
            raise FleetRuntimeError(f"unknown runtime phase: {phase}")
        if phases.index(phase) < phases.index("core_provisioned"):
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
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
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
            phase = "core_provisioned"
            self._write_manifest(manifest_path, runtime_manifest, phase)
        if restarting:
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
            self._run_json(
                [
                    *self.core_command,
                    "--db",
                    str(core_db),
                    "context.invalidate",
                    "--fleet",
                    resolved.fleet_id,
                    "--operation-id",
                    f"runtime-restart:{resolved.launch_id}:{runtime_generation}",
                ],
                "Core context invalidation",
            )
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
        provisioned: Mapping[str, Any] = {"status": "already_provisioned"}
        if phases.index(phase) < phases.index("herdr_provisioned"):
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
            provisioned = self._run_json(
                [
                    *self.herdr_command,
                    "--state-db",
                    str(herdr_db),
                    "provision",
                    "--fleet-json",
                    json.dumps(resolved.fleet, ensure_ascii=False, sort_keys=True),
                    "--launch-profile-json",
                    json.dumps(
                        resolved.launch_profile,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "--agent-command-profiles-json",
                    json.dumps(
                        resolved.agent_command_profiles,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
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
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
            phase = "herdr_provisioned"
            self._write_manifest(manifest_path, runtime_manifest, phase)
        spec = resolved.fleet["spec"]
        manager_ref = spec["collaboration"]["manager"]
        for task in spec["tasks"]:
            if task.get("depends_on"):
                continue
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
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
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
        for member in spec["members"]:
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
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
            self._raise_if_stop_requested(state_dir, resolved.launch_id)
        self._raise_if_stop_requested(state_dir, resolved.launch_id)
        self._write_manifest(manifest_path, runtime_manifest, "active")
        monitor = self.monitor(
            resolved.launch_id,
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
        self, launch_id: str, state_dir: Path, *, execute: bool = False
    ) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, launch_id)
        if not execute:
            if not manifest_path.exists():
                return {"launch_id": launch_id, "status": "inactive"}
            manifest = _load_document(manifest_path)
            fleet_id = str(manifest.get("fleet_id") or "")
            if not fleet_id:
                raise FleetRuntimeError("runtime manifest has no fleet_id")
            return {
                "launch_id": launch_id,
                "fleet_id": fleet_id,
                "status": "planned",
                "action": "stop",
            }
        with self._publish_stop_request(state_dir, launch_id):
            with self._hold_launch_lock(
                state_dir,
                launch_id,
                timeout_seconds=30,
                timeout_message=(
                    f"Fleet launch {launch_id!r} did not stop within 30 seconds"
                ),
            ):
                if not manifest_path.exists():
                    return {"launch_id": launch_id, "status": "inactive"}
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
                        return {"launch_id": launch_id, "status": "inactive"}
                    locked_manifest = _load_document(manifest_path)
                    locked_fleet_id = str(locked_manifest.get("fleet_id") or "")
                    if locked_fleet_id != fleet_id:
                        raise FleetRuntimeError(
                            "runtime manifest Fleet identity changed while stopping"
                        )
                    bundle = self._execution_bundle_from_manifest(
                        locked_manifest, state_dir, launch_id
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
                            launch_id,
                            state_dir,
                            manifest_path,
                            stopping_manifest,
                            fleet_id,
                        )
        return {
            "launch_id": launch_id,
            "fleet_id": fleet_id,
            "status": "stopped",
            "herdr": dict(herdr),
        }

    def _stop_locked(
        self,
        launch_id: str,
        state_dir: Path,
        manifest_path: Path,
        manifest: Mapping[str, Any],
        fleet_id: str,
    ) -> Mapping[str, Any]:
        fleet_state = self._fleet_state_dir(state_dir, launch_id)
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
                    f"runtime-stop:{launch_id}:{manifest['runtime_generation']}",
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
        self, launch_id: str, state_dir: Path, *, execute: bool = False
    ) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, launch_id)
        if not execute:
            return {
                "launch_id": launch_id,
                "status": "planned",
                "action": "remove",
            }
        with self._publish_stop_request(state_dir, launch_id):
            with self._hold_launch_lock(
                state_dir,
                launch_id,
                timeout_seconds=30,
                timeout_message=(
                    f"Fleet launch {launch_id!r} did not stop within 30 seconds"
                ),
            ):
                if not manifest_path.exists():
                    return {"launch_id": launch_id, "status": "inactive"}
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
                        return {"launch_id": launch_id, "status": "inactive"}
                    locked_manifest = _load_document(manifest_path)
                    locked_fleet_id = str(locked_manifest.get("fleet_id") or "")
                    if locked_fleet_id != fleet_id:
                        raise FleetRuntimeError(
                            "runtime manifest Fleet identity changed while removing"
                        )
                    bundle = self._execution_bundle_from_manifest(
                        locked_manifest, state_dir, launch_id
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
                            launch_id,
                            state_dir,
                            manifest_path,
                            stopping_manifest,
                            fleet_id,
                        )
                    else:
                        herdr = {"status": "already_stopped", "idempotent": True}
                    stopped = {
                        "launch_id": launch_id,
                        "fleet_id": fleet_id,
                        "status": "stopped",
                        "herdr": dict(herdr),
                    }
                    self._write_manifest(manifest_path, locked_manifest, "removing")
                    core_db = (
                        self._fleet_state_dir(state_dir, launch_id)
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
        return {
            "launch_id": launch_id,
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
            *self._launch_roots(fleet_dirs),
            *self.agent_command_profile_dirs,
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
            ("launch_dirs", self._launch_roots(fleet_dirs)),
            ("agent_command_profile_dirs", self.agent_command_profile_dirs),
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
        launch_id: str,
        state_dir: Path,
        *,
        once: bool,
        poll_seconds: float,
    ) -> dict[str, Any]:
        if poll_seconds <= 0:
            raise FleetRuntimeError("poll_seconds must be positive")
        manifest_path = self._manifest_path(state_dir, launch_id)
        if not manifest_path.exists():
            raise FleetRuntimeError(f"runtime manifest not found: {launch_id}")
        manifest = _load_document(manifest_path)
        fleet_id = str(manifest.get("fleet_id") or "")
        if not fleet_id:
            raise FleetRuntimeError("runtime manifest has no fleet_id")
        execution_bundle = self._execution_bundle_from_manifest(
            manifest, state_dir, launch_id
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
                if self._stop_requested(state_dir, launch_id):
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
                            str(self._fleet_state_dir(state_dir, launch_id) / "core.sqlite3"),
                            "--herdr-db",
                            str(self._fleet_state_dir(state_dir, launch_id) / "herdr.sqlite3"),
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

    def status(self, launch_id: str, state_dir: Path) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, launch_id)
        if not manifest_path.exists():
            return {"launch_id": launch_id, "status": "inactive"}
        manifest = _load_document(manifest_path)
        fleet_id = str(manifest.get("fleet_id") or "")
        if not fleet_id:
            raise FleetRuntimeError("runtime manifest has no fleet_id")
        execution_bundle = self._execution_bundle_from_manifest(
            manifest, state_dir, launch_id
        )
        phase = str(manifest["phase"])
        if phase in {
            "planned",
            "core_provisioned",
            "herdr_provisioned",
            "stopping",
            "stopped",
            "removing",
        }:
            return {
                "launch_id": launch_id,
                "fleet_id": fleet_id,
                "status": phase,
                "recovery_required": phase in {"stopping", "removing"},
                "configuration": dict(manifest),
            }
        execution_runtime = self._with_execution_bundle(execution_bundle)
        drift = False
        for path_key, hash_key in (
            ("launch_path", "launch_hash"),
            ("fleet_path", "fleet_source_hash"),
            ("profile_path", "profile_hash"),
            ("role_catalog_path", "role_catalog_hash"),
        ):
            configured_path = manifest.get(path_key)
            if configured_path is None and path_key == "launch_path" and manifest.get("legacy"):
                continue
            if not isinstance(configured_path, str) or not Path(configured_path).is_file():
                drift = True
                continue
            drift = drift or _content_hash(_load_document(Path(configured_path))) != manifest.get(
                hash_key
            )
        command_sources = manifest.get("agent_command_profile_sources", [])
        if not isinstance(command_sources, list):
            drift = True
        else:
            for source in command_sources:
                if not isinstance(source, Mapping):
                    drift = True
                    continue
                configured_path = source.get("path")
                expected_hash = source.get("hash")
                if (
                    not isinstance(configured_path, str)
                    or not Path(configured_path).is_file()
                    or _content_hash(_load_document(Path(configured_path)))
                    != expected_hash
                ):
                    drift = True
        core = execution_runtime._run_json(
            [
                *execution_runtime.core_command,
                "--db",
                str(self._fleet_state_dir(state_dir, launch_id) / "core.sqlite3"),
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
                str(self._fleet_state_dir(state_dir, launch_id) / "herdr.sqlite3"),
                "status",
                "--fleet",
                fleet_id,
            ],
            "Herdr fleet status",
        )
        return {
            "launch_id": launch_id,
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
    common.add_argument("--launch-dir", type=Path, action="append", default=[])
    common.add_argument(
        "--agent-command-profile-dir", type=Path, action="append", default=[]
    )
    common.add_argument(
        "--legacy-fleet",
        action="store_true",
        help="deprecated: build an in-memory LaunchProfile from a Fleet v1 document",
    )
    common.add_argument("--state-dir", type=Path, default=_default_state_dir())
    common.add_argument(
        "--role-catalog",
        type=Path,
        default=(Path(os.environ["AGENT_ROLES_CATALOG"]) if os.environ.get("AGENT_ROLES_CATALOG") else None),
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
    plan.add_argument("fleet")
    plan.add_argument("--cwd", default=str(Path.cwd()))
    plan.add_argument(
        "--agent-kind",
        choices=["codex", "claude"],
        default="codex",
        help=argparse.SUPPRESS,
    )
    start = sub.add_parser("start", parents=[common])
    start.add_argument("fleet")
    start.add_argument("--cwd", default=str(Path.cwd()))
    start.add_argument(
        "--agent-kind",
        choices=["codex", "claude"],
        default="codex",
        help=argparse.SUPPRESS,
    )
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
    launch_dirs = args.launch_dir or [config_root / "herdr-launch-profiles"]
    agent_command_profile_dirs = args.agent_command_profile_dir or [
        config_root / "agent-command-profiles"
    ]
    try:
        if args.action in {"list", "plan", "start"} and args.role_catalog is None:
            raise FleetRuntimeError(
                "Role Catalog is required: pass --role-catalog or set AGENT_ROLES_CATALOG"
            )
        runtime = FleetRuntime(
            [args.core_command],
            [args.herdr_command],
            [args.controller_command],
            role_catalog=args.role_catalog,
            launch_dirs=launch_dirs,
            agent_command_profile_dirs=agent_command_profile_dirs,
            allow_legacy_fleet=args.legacy_fleet,
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
