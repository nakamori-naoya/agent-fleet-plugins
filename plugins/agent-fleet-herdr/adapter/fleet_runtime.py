#!/usr/bin/env python3
"""複数のFleet設定を解決し、Herdr艦隊を一つの入口から起動する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


Runner = Callable[..., subprocess.CompletedProcess[str]]


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
    ):
        self.core_command = tuple(core_command)
        self.herdr_command = tuple(herdr_command)
        self.controller_command = tuple(controller_command)
        self.runner = runner
        self.sleeper = sleeper

    def _run_json(
        self, argv: Sequence[str], context: str, *, timeout: int = 60
    ) -> Mapping[str, Any]:
        completed = self.runner(
            list(argv), capture_output=True, text=True, timeout=timeout
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
            rows.append(
                {
                    "fleet_id": fleet_id,
                    "path": str(path),
                    "objective": spec["objective"],
                    "members": len(spec["members"]),
                    "profile_ref": profile_ref,
                    "profile_resolved": profile_ref in profiles,
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
        return state_dir / "runtimes" / f"{fleet_id}.json"

    @staticmethod
    def _write_manifest(path: Path, desired: Mapping[str, Any], phase: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
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
        poll_seconds: float = 0.05,
    ) -> dict[str, Any]:
        planned = self.plan(
            fleet_name, fleet_dirs, profile_dirs, state_dir, cwd, agent_kind
        )
        if not execute:
            return planned
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
        if manifest_path.exists():
            current = _load_document(manifest_path)
            if not all(current.get(key) == value for key, value in desired.items()):
                raise FleetRuntimeError(
                    "configuration conflict: stop the active Fleet before changing its config"
                )
            phase = str(current.get("phase") or "active")
            if phase == "active":
                monitor = self.monitor(
                    resolved.fleet_id,
                    state_dir,
                    once=once,
                    poll_seconds=poll_seconds,
                )
                return {**desired, "status": "resumed", "monitor": monitor}
        else:
            self._write_manifest(manifest_path, desired, phase)
        core_db = state_dir / "core.sqlite3"
        herdr_db = state_dir / "herdr.sqlite3"
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
            self._write_manifest(manifest_path, desired, phase)
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
                    "--execute",
                ],
                "Herdr fleet provision",
                timeout=180,
            )
            phase = "herdr_provisioned"
            self._write_manifest(manifest_path, desired, phase)
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
                "core_command": list(self.core_command),
                "core_db": str(core_db),
                "fleet_id": resolved.fleet_id,
                "status_argv": [
                    *self.core_command,
                    "--db",
                    str(core_db),
                    "status",
                    "--fleet",
                    resolved.fleet_id,
                ],
                "context_confirm_argv": [
                    *self.core_command,
                    "--db",
                    str(core_db),
                    "context.confirm",
                    "--fleet",
                    resolved.fleet_id,
                    "--agent-ref",
                    agent_ref,
                ],
                "reporting": {
                    "progress_action": "progress-report",
                    "state_action": "task-report",
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
                    f"context-sync:{resolved.fleet_id}:{agent_ref}:startup",
                    "--payload",
                    json.dumps(
                        {"reason": "fleet_start", "control": control},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ],
                f"Context activation ({agent_ref})",
            )
        self._write_manifest(manifest_path, desired, "active")
        monitor = self.monitor(
            resolved.fleet_id,
            state_dir,
            once=once,
            poll_seconds=poll_seconds,
        )
        return {
            **desired,
            "status": "started",
            "herdr": dict(provisioned),
            "monitor": monitor,
        }

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
        stopping = False

        def stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        previous_int = signal.signal(signal.SIGINT, stop)
        previous_term = signal.signal(signal.SIGTERM, stop)
        try:
            while not stopping:
                result = self._run_json(
                    [
                        *self.controller_command,
                        "--core-db",
                        str(state_dir / "core.sqlite3"),
                        "--herdr-db",
                        str(state_dir / "herdr.sqlite3"),
                        "--fleet",
                        fleet_id,
                        "--worker-id",
                        f"controller:{os.getpid()}",
                        "--execute",
                    ],
                    "Fleet controller",
                )
                status = result.get("status")
                if status == "idle":
                    if once:
                        break
                    self.sleeper(poll_seconds)
                else:
                    processed += 1
                    if once:
                        break
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        return {"status": "stopped" if stopping else "idle", "processed": processed}

    def status(self, fleet_id: str, state_dir: Path) -> dict[str, Any]:
        manifest_path = self._manifest_path(state_dir, fleet_id)
        if not manifest_path.exists():
            return {"fleet_id": fleet_id, "status": "inactive"}
        manifest = _load_document(manifest_path)
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
                str(state_dir / "core.sqlite3"),
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
                str(state_dir / "herdr.sqlite3"),
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
    plugin_root = adapter_root.parent
    core_default = shutil.which("fleet-control") or str(
        plugin_root.parent / "agent-fleet-core" / "core" / "scripts" / "fleet-control"
    )
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
    start.add_argument("--poll-seconds", type=float, default=0.05)
    status = sub.add_parser("status", parents=[common])
    status.add_argument("fleet")
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
        if args.action == "list":
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
        else:
            result = runtime.status(args.fleet, args.state_dir)
    except (FleetRuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
