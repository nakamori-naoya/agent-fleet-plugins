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

from launch_profiles import LaunchProfileCatalog, LaunchProfileError


Runner = Callable[..., subprocess.CompletedProcess[str]]
DEFAULT_HOOK_SOURCE = Path(__file__).resolve().parents[1] / "hooks" / "role_context.py"
MANIFEST_FORMAT_VERSION = 1
RUNTIME_PHASES = frozenset(
    {"planned", "core_provisioned", "herdr_provisioned", "active", "stopping", "stopped", "removing"}
)


class FleetRuntimeError(RuntimeError):
    """設定解決または艦隊起動を安全に継続できない。"""


class ExecutionBundle:
    """One immutable Core/Herdr/Controller runtime closure."""

    def __init__(
        self,
        root: Path,
        source_identity: Mapping[str, Any],
        command_relatives: Mapping[str, Sequence[str]],
        *,
        created: bool = False,
    ):
        self.root = root
        self.source_identity = dict(source_identity)
        self.command_relatives = {
            name: tuple(argv) for name, argv in command_relatives.items()
        }
        self.created = created

    def command(self, name: str) -> tuple[str, ...]:
        argv = self.command_relatives[name]
        return (str((self.root / argv[0]).resolve()), *argv[1:])

    @property
    def commands(self) -> dict[str, list[str]]:
        return {
            name: list(self.command(name))
            for name in ("core", "herdr", "controller")
        }


class ResolvedFleet:
    def __init__(
        self,
        launch_id: str,
        launch_path: Path | None,
        launch_profile: Mapping[str, Any],
        fleet_id: str,
        fleet_path: Path,
        fleet_source: Mapping[str, Any],
        fleet: Mapping[str, Any],
        profile_ref: str,
        profile_path: Path,
        profile: Mapping[str, Any],
        codex_hook_trust: str,
        fleet_source_hash: str,
        role_catalog: Mapping[str, Any] | None,
        role_catalog_hash: str | None,
        *,
        legacy: bool = False,
    ):
        self.launch_id = launch_id
        self.launch_path = launch_path
        self.launch_profile = launch_profile
        self.fleet_id = fleet_id
        self.fleet_path = fleet_path
        self.fleet_source = fleet_source
        self.fleet = fleet
        self.profile_ref = profile_ref
        self.profile_path = profile_path
        self.profile = profile
        self.codex_hook_trust = codex_hook_trust
        self.role_catalog = role_catalog
        self.role_catalog_hash = role_catalog_hash
        self.legacy = legacy
        self._fleet_source_hash = fleet_source_hash
        self._launch_source_hash = (
            _content_hash(launch_profile)
            if launch_path is not None
            else None
        )
        self._profile_source_hash = _content_hash(profile)

    @property
    def launch_hash(self) -> str:
        return _content_hash(self.launch_profile)

    @property
    def fleet_hash(self) -> str:
        return _content_hash(self.fleet)

    @property
    def fleet_source_hash(self) -> str:
        return self._fleet_source_hash

    @property
    def launch_source_hash(self) -> str | None:
        return self._launch_source_hash

    @property
    def profile_source_hash(self) -> str:
        return self._profile_source_hash

    @property
    def profile_hash(self) -> str:
        return _content_hash(self.profile)

    @property
    def composition_hash(self) -> str:
        return _content_hash(
            {
                "launch": self.launch_profile,
                "fleet": self.fleet,
                "profile": self.profile,
            }
        )


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
        role_catalog: Path | None = None,
        launch_dirs: Sequence[Path] = (),
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
        executable = Path(argv[0])
        if executable.is_file():
            argv[0] = str(executable.resolve())
        else:
            discovered = shutil.which(argv[0])
            if discovered:
                argv[0] = discovered
        return shlex.join(argv)

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

    @staticmethod
    def _bundle_identity(
        root: Path,
        command_relatives: Mapping[str, Sequence[str]],
        hook_payload: bytes,
    ) -> dict[str, Any]:
        files = []
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(root).as_posix(),
        ):
            metadata = path.stat()
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "mode": metadata.st_mode & 0o777,
                    "size": metadata.st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        return {
            "format_version": 1,
            "hook_sha256": hashlib.sha256(hook_payload).hexdigest(),
            "files": files,
            "commands": {
                name: list(command_relatives[name])
                for name in ("core", "herdr", "controller")
            },
        }

    @staticmethod
    def _assert_tree_has_no_symlinks(root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise FleetRuntimeError(
                f"execution source must be a regular directory: {root}"
            )
        for directory, subdirectories, filenames in os.walk(
            root, followlinks=False
        ):
            parent = Path(directory)
            for name in [*subdirectories, *filenames]:
                if (parent / name).is_symlink():
                    raise FleetRuntimeError(
                        f"execution source contains a symbolic link: {parent / name}"
                    )

    @staticmethod
    def _freeze_execution_tree(root: Path) -> None:
        paths = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
        for path in paths:
            if path.is_symlink():
                raise FleetRuntimeError(
                    f"execution snapshot contains a symbolic link: {path}"
                )
            mode = path.stat().st_mode
            if path.is_dir():
                path.chmod(0o500)
            elif path.is_file():
                path.chmod(0o500 if mode & 0o111 else 0o400)
        root.chmod(0o500)

    @staticmethod
    def _copy_execution_tree(source: Path, target: Path) -> None:
        shutil.copytree(
            source,
            target,
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    @staticmethod
    def _execution_tree_files(root: Path) -> list[Path]:
        candidates: list[Path]
        if root.name == "core":
            plugin_root = root.parent
            candidates = [
                root / "fleet_control.py",
                root / "scripts" / "fleet-control",
                plugin_root / "spec" / "scripts" / "validate_fleet.py",
                plugin_root / "spec" / "schema" / "envelopes.schema.yml",
                plugin_root / "spec" / "schema" / "fleet.schema.yml",
                plugin_root / "spec" / "config" / "defaults.yml",
                plugin_root / "config" / "defaults.yml",
            ]
        elif root.name == "adapter":
            plugin_root = root.parent
            hook_plugin = plugin_root / "session-hooks-plugin"
            candidates = [
                root / "fleet_controller.py",
                root / "herdr_adapter.py",
                root / "launch_profiles.py",
                root / "view_profiles.py",
                root / "scripts" / "fleet-controller",
                root / "scripts" / "fleet-herdr",
                root / "schema" / "launch-profile.schema.yml",
                root / "schema" / "view-profile.schema.yml",
                hook_plugin / "hooks" / "claude-hooks.json",
                hook_plugin / ".claude-plugin" / "plugin.json",
            ]
        else:
            raise FleetRuntimeError(
                f"unsupported execution tree identity: {root.name}"
            )
        source_plugin_root = root.parent
        for path in candidates:
            try:
                relative = path.relative_to(source_plugin_root)
            except ValueError as exc:
                raise FleetRuntimeError(
                    f"required execution closure file escapes its plugin: {path}"
                ) from exc
            ancestor = source_plugin_root
            for part in relative.parts[:-1]:
                ancestor = ancestor / part
                try:
                    metadata = ancestor.lstat()
                except OSError as exc:
                    raise FleetRuntimeError(
                        f"required execution closure directory is unavailable: {ancestor}"
                    ) from exc
                if ancestor.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                    raise FleetRuntimeError(
                        f"execution source contains a symbolic or invalid directory: {ancestor}"
                    )
            if path.is_symlink() or not path.is_file():
                raise FleetRuntimeError(
                    f"required execution closure file is unavailable: {path}"
                )
        return sorted(candidates, key=lambda path: path.relative_to(root.parent).as_posix())

    @staticmethod
    def _validate_claude_hook_registration(adapter_root: Path) -> None:
        plugin_root = adapter_root.parent / "session-hooks-plugin"
        plugin_path = plugin_root / ".claude-plugin" / "plugin.json"
        hooks_path = plugin_root / "hooks" / "claude-hooks.json"
        try:
            plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
            registration = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FleetRuntimeError(
                f"Claude hook registration is unreadable or invalid JSON: {exc}"
            ) from exc
        if (
            not isinstance(plugin, Mapping)
            or plugin.get("name") != "agent-fleet-session-hooks"
            or plugin.get("hooks") != "./hooks/claude-hooks.json"
        ):
            raise FleetRuntimeError("Claude hook plugin manifest has an invalid registration")
        hooks = registration.get("hooks") if isinstance(registration, Mapping) else None
        if not isinstance(hooks, Mapping) or set(hooks) != {
            "SessionStart",
            "UserPromptSubmit",
        }:
            raise FleetRuntimeError("Claude hook registration has invalid event bindings")
        expected_script = (
            'if [ -n "${AGENT_FLEET_HOOK_RUNTIME:-}" ]; then exec python3 '
            '"$AGENT_FLEET_HOOK_RUNTIME" --runtime-product claude; fi'
        )
        for event_name in ("SessionStart", "UserPromptSubmit"):
            event_bindings = hooks.get(event_name)
            if not isinstance(event_bindings, list) or len(event_bindings) != 1:
                raise FleetRuntimeError(
                    f"Claude hook registration has invalid {event_name} bindings"
                )
            event_binding = event_bindings[0]
            if (
                not isinstance(event_binding, Mapping)
                or (
                    event_name == "SessionStart"
                    and event_binding.get("matcher")
                    != "startup|resume|clear|compact|fork"
                )
                or (event_name == "UserPromptSubmit" and "matcher" in event_binding)
            ):
                raise FleetRuntimeError(
                    f"Claude hook registration has invalid {event_name} matcher"
                )
            handlers = (
                event_binding.get("hooks")
                if isinstance(event_binding, Mapping) else None
            )
            if not isinstance(handlers, list) or len(handlers) != 1:
                raise FleetRuntimeError(
                    f"Claude hook registration has invalid {event_name} handler"
                )
            handler = handlers[0]
            args = handler.get("args") if isinstance(handler, Mapping) else None
            if (
                not isinstance(handler, Mapping)
                or handler.get("type") != "command"
                or handler.get("command") != "sh"
                or args != ["-c", expected_script]
                or not isinstance(handler.get("timeout"), int)
                or isinstance(handler.get("timeout"), bool)
                or handler["timeout"] != 12
            ):
                raise FleetRuntimeError(
                    f"Claude hook registration has an invalid {event_name} command"
                )

    @classmethod
    def _copy_execution_closure(cls, source_root: Path, target_root: Path) -> None:
        files = cls._execution_tree_files(source_root)
        if not files:
            raise FleetRuntimeError(
                f"required implementation tree is empty: {source_root}"
            )
        source_plugin_root = source_root.parent
        target_plugin_root = target_root.parent
        for source in files:
            metadata = source.lstat()
            if source.is_symlink() or not source.is_file() or metadata.st_nlink != 1:
                raise FleetRuntimeError(
                    f"execution source file is not an independent regular file: {source}"
                )
            relative = source.relative_to(source_plugin_root)
            target = target_plugin_root / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, target, follow_symlinks=False)

    @contextmanager
    def _capture_execution_bundle(
        self, hook_payload: bytes
    ) -> Iterator[ExecutionBundle]:
        """Copy the executable closure once, then execute only the copy."""
        source_probe = self._preflight_execution(hook_payload)
        source_commands = {
            "core": self.core_command,
            "herdr": self.herdr_command,
            "controller": self.controller_command,
        }
        identity_names = {
            "core": "core",
            "herdr": "adapter",
            "controller": "controller",
        }
        with tempfile.TemporaryDirectory(
            prefix="agent-fleet-execution-snapshot-"
        ) as temporary:
            snapshot_root = Path(temporary) / "bundle"
            snapshot_root.mkdir(mode=0o700)
            copied_roots: dict[Path, Path] = {}
            command_relatives: dict[str, tuple[str, ...]] = {}
            for name in ("core", "herdr", "controller"):
                command = source_commands[name]
                command_path = Path(
                    str(source_probe[identity_names[name]]["command_path"])
                )
                source_root = command_path.parent.parent.resolve()
                self._assert_tree_has_no_symlinks(source_root)
                target_root = copied_roots.get(source_root)
                if target_root is None:
                    target_root = (
                        snapshot_root
                        / "trees"
                        / f"{len(copied_roots)}-{source_root.name}-plugin"
                        / source_root.name
                    )
                    self._copy_execution_closure(source_root, target_root)
                    copied_roots[source_root] = target_root
                executable = target_root / command_path.relative_to(source_root)
                command_relatives[name] = (
                    executable.relative_to(snapshot_root).as_posix(),
                    *command[1:],
                )
            self._freeze_execution_tree(snapshot_root)
            provisional = ExecutionBundle(
                snapshot_root,
                {},
                command_relatives,
            )
            stable_runtime = self._with_execution_bundle(provisional)
            stable_runtime._preflight_execution(hook_payload)
            stable_runtime._validate_claude_hook_registration(
                Path(stable_runtime.herdr_command[0]).parent.parent
            )
            stable_runtime._run_json(
                [
                    *stable_runtime.controller_command,
                    "--core-db",
                    "__fleet_runtime_preflight_core__",
                    "--herdr-db",
                    "__fleet_runtime_preflight_herdr__",
                    "--fleet",
                    "__fleet_runtime_preflight__",
                    "--worker-id",
                    "__fleet_runtime_preflight__",
                ],
                "Fleet controller executable preflight",
                timeout=10,
            )
            bundle = ExecutionBundle(
                snapshot_root,
                self._bundle_identity(
                    snapshot_root, command_relatives, hook_payload
                ),
                command_relatives,
            )
            self._validate_execution_bundle(bundle, hook_payload)
            yield bundle

    @staticmethod
    def _prepare_private_runtime_directory(
        fleet_state_dir: Path,
        name: str,
        label: str,
        *,
        create: bool = True,
    ) -> Path:
        """Create or validate one private, non-symlink runtime directory."""
        if create:
            fleet_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif not fleet_state_dir.is_dir():
            raise FleetRuntimeError(f"{label} Fleet state directory is missing")
        child = fleet_state_dir / name
        if child.is_symlink():
            raise FleetRuntimeError(f"{label} directory must not be a symbolic link")
        if create:
            child.mkdir(mode=0o700, exist_ok=True)
        if not child.exists():
            raise FleetRuntimeError(f"{label} directory is missing")
        try:
            metadata = child.lstat()
        except OSError as exc:
            raise FleetRuntimeError(f"cannot validate {label} directory: {exc}") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o7777 != 0o700
        ):
            raise FleetRuntimeError(f"{label} directory has unsafe ownership or mode")
        return child.resolve()

    def _publish_execution_bundle(
        self,
        bundle: ExecutionBundle,
        fleet_state_dir: Path,
        hook_payload: bytes,
    ) -> ExecutionBundle:
        identity_payload = json.dumps(
            bundle.source_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        identity_hash = hashlib.sha256(identity_payload).hexdigest()
        parent = self._prepare_private_runtime_directory(
            fleet_state_dir, "execution-runtimes", "execution snapshot"
        )
        target = parent / identity_hash
        created = False
        if not target.exists():
            temporary = parent / f".{identity_hash}-{uuid.uuid4().hex}.tmp"
            try:
                self._copy_execution_tree(bundle.root, temporary)
                self._freeze_execution_tree(temporary)
                staged = ExecutionBundle(
                    temporary,
                    bundle.source_identity,
                    bundle.command_relatives,
                )
                self._validate_execution_bundle(staged, hook_payload)
                temporary.replace(target)
                created = True
            except Exception:
                if temporary.exists():
                    self._remove_execution_tree(temporary)
                raise
        published = ExecutionBundle(
            target,
            bundle.source_identity,
            bundle.command_relatives,
            created=created,
        )
        try:
            self._validate_execution_bundle(published, hook_payload)
        except Exception:
            if created and target.exists():
                self._remove_execution_tree(target)
            raise
        return published

    @staticmethod
    def _remove_execution_tree(root: Path) -> None:
        for path in [root, *root.rglob("*")]:
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        shutil.rmtree(root)

    @staticmethod
    def _discard_uncommitted_execution_bundle(bundle: ExecutionBundle) -> None:
        if not bundle.created or not bundle.root.exists():
            return
        FleetRuntime._remove_execution_tree(bundle.root)
        try:
            bundle.root.parent.rmdir()
            bundle.root.parent.parent.rmdir()
        except OSError:
            pass

    def _validate_execution_bundle(
        self, bundle: ExecutionBundle, hook_payload: bytes
    ) -> None:
        raw_root = bundle.root
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise FleetRuntimeError("immutable execution snapshot is missing or unsafe")
        root = raw_root.resolve()
        if (
            root.stat().st_uid != os.getuid()
            or root.stat().st_mode & 0o7777 != 0o500
        ):
            raise FleetRuntimeError("immutable execution snapshot has unsafe ownership or mode")
        identity = bundle.source_identity
        format_version = identity.get("format_version")
        if (
            not isinstance(format_version, int)
            or isinstance(format_version, bool)
            or format_version != 1
        ):
            raise FleetRuntimeError("unsupported immutable execution snapshot format")
        if identity.get("hook_sha256") != hashlib.sha256(hook_payload).hexdigest():
            raise FleetRuntimeError("immutable execution snapshot hook identity changed")
        descriptors = identity.get("files")
        if not isinstance(descriptors, list) or not descriptors:
            raise FleetRuntimeError("immutable execution snapshot has no file manifest")
        expected_files: dict[Path, Mapping[str, Any]] = {}
        for descriptor in descriptors:
            if not isinstance(descriptor, Mapping):
                raise FleetRuntimeError("immutable execution file descriptor is invalid")
            relative_value = descriptor.get("path")
            mode = descriptor.get("mode")
            size = descriptor.get("size")
            digest = descriptor.get("sha256")
            if (
                not isinstance(relative_value, str)
                or not relative_value
                or not isinstance(mode, int)
                or mode not in {0o400, 0o500}
                or not isinstance(size, int)
                or size < 0
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise FleetRuntimeError("immutable execution file descriptor is invalid")
            relative = Path(relative_value)
            if relative.is_absolute() or ".." in relative.parts:
                raise FleetRuntimeError("immutable execution file path is unsafe")
            expected = (root / relative).resolve()
            if not expected.is_relative_to(root) or expected in expected_files:
                raise FleetRuntimeError("immutable execution file path is unsafe")
            expected_files[expected] = descriptor
        expected_directories = {root}
        for path in expected_files:
            parent = path.parent
            while parent.is_relative_to(root):
                expected_directories.add(parent)
                if parent == root:
                    break
                parent = parent.parent
        actual_files: set[Path] = set()
        actual_directories = {root}
        for path in root.rglob("*"):
            if path.is_symlink():
                raise FleetRuntimeError(
                    "immutable execution snapshot contains a symbolic link"
                )
            if path.is_file():
                actual_files.add(path.resolve())
            elif path.is_dir():
                actual_directories.add(path.resolve())
            else:
                raise FleetRuntimeError(
                    "immutable execution snapshot contains a special file"
                )
        if (
            actual_files != set(expected_files)
            or actual_directories != expected_directories
        ):
            raise FleetRuntimeError(
                "immutable execution snapshot contains an unexpected or missing path"
            )
        command_paths = {
            Path(bundle.command(name)[0]).resolve()
            for name in ("core", "herdr", "controller")
        }
        if not command_paths.issubset(expected_files):
            raise FleetRuntimeError("immutable execution command is not a declared file")
        for path in actual_directories:
            metadata = path.stat()
            if (
                metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o7777 != 0o500
            ):
                raise FleetRuntimeError(
                    "immutable execution snapshot has unsafe ownership or mode"
                )
        for path, descriptor in expected_files.items():
            metadata = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if (
                metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o7777 != descriptor["mode"]
                or metadata.st_size != descriptor["size"]
                or digest != descriptor["sha256"]
            ):
                raise FleetRuntimeError(
                    "immutable execution snapshot file does not match its descriptor"
                )
            if path in command_paths and descriptor["mode"] != 0o500:
                raise FleetRuntimeError(
                    "immutable execution snapshot command is not owner-executable"
                )

    def _execution_bundle_from_manifest(
        self,
        manifest: Mapping[str, Any],
        state_dir: Path,
        launch_id: str,
    ) -> ExecutionBundle:
        self._validate_runtime_manifest(manifest)
        if manifest["launch_id"] != launch_id:
            raise FleetRuntimeError(
                "runtime manifest launch identity does not match the requested launch"
            )
        source_identity = manifest.get("execution_identity")
        snapshot_root_value = manifest.get("execution_snapshot_root")
        runtime_commands = manifest.get("runtime_commands")
        if (
            not isinstance(source_identity, Mapping)
            or not isinstance(snapshot_root_value, str)
            or not isinstance(runtime_commands, Mapping)
        ):
            raise FleetRuntimeError(
                "runtime has no immutable execution snapshot; remove it with its "
                "original plugin version and start it again"
            )
        fleet_state_dir = self._fleet_state_dir(state_dir, launch_id)
        allowed_root = self._prepare_private_runtime_directory(
            fleet_state_dir,
            "execution-runtimes",
            "execution snapshot",
            create=False,
        )
        snapshot_root = Path(snapshot_root_value)
        if snapshot_root.is_symlink():
            raise FleetRuntimeError("immutable execution snapshot path is unsafe")
        resolved_root = snapshot_root.resolve()
        if resolved_root.parent != allowed_root:
            raise FleetRuntimeError("immutable execution snapshot escapes Fleet state")
        identity_payload = json.dumps(
            source_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if resolved_root.name != hashlib.sha256(identity_payload).hexdigest():
            raise FleetRuntimeError(
                "immutable execution snapshot path does not match its identity"
            )
        declared_commands = source_identity.get("commands")
        if not isinstance(declared_commands, Mapping):
            raise FleetRuntimeError("runtime identity has no canonical commands")
        command_relatives: dict[str, tuple[str, ...]] = {}
        for name in ("core", "herdr", "controller"):
            argv = declared_commands.get(name)
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(value, str) and value for value in argv)
            ):
                raise FleetRuntimeError(
                    f"runtime manifest has an invalid {name} command snapshot"
                )
            relative_executable = Path(argv[0])
            if relative_executable.is_absolute() or ".." in relative_executable.parts:
                raise FleetRuntimeError("runtime command snapshot path is unsafe")
            executable = resolved_root / relative_executable
            if executable.is_symlink():
                raise FleetRuntimeError("runtime command snapshot path is unsafe")
            resolved_executable = executable.resolve()
            if not resolved_executable.is_relative_to(resolved_root):
                raise FleetRuntimeError("runtime command snapshot escapes its root")
            command_relatives[name] = (
                resolved_executable.relative_to(resolved_root).as_posix(),
                *argv[1:],
            )
        bundle = ExecutionBundle(
            resolved_root,
            source_identity,
            command_relatives,
        )
        if runtime_commands != bundle.commands:
            raise FleetRuntimeError(
                "runtime manifest commands do not match the immutable identity"
            )
        hook_sha256 = manifest.get("hook_sha256")
        hook_runtime = manifest.get("hook_runtime")
        if not isinstance(hook_sha256, str) or not isinstance(hook_runtime, str):
            raise FleetRuntimeError("runtime manifest has no fixed hook runtime")
        hook_payload = self._validate_hook_runtime(
            self._fleet_state_dir(state_dir, launch_id),
            Path(hook_runtime),
            hook_sha256,
        ).read_bytes()
        self._validate_execution_bundle(bundle, hook_payload)
        return bundle

    @staticmethod
    def _validate_runtime_manifest(manifest: Mapping[str, Any]) -> None:
        manifest_format_version = manifest.get("manifest_format_version")
        if (
            not isinstance(manifest_format_version, int)
            or isinstance(manifest_format_version, bool)
            or manifest_format_version != MANIFEST_FORMAT_VERSION
        ):
            raise FleetRuntimeError(
                "runtime manifest format is unsupported; remove it with its original "
                "plugin version and start it again"
            )
        phase = manifest.get("phase")
        if not isinstance(phase, str) or phase not in RUNTIME_PHASES:
            raise FleetRuntimeError("runtime manifest has an invalid or missing phase")
        for key in ("launch_id", "fleet_id"):
            value = manifest.get(key)
            if not isinstance(value, str) or not value:
                raise FleetRuntimeError(f"runtime manifest has no {key}")
        runtime_generation = manifest.get("runtime_generation")
        if not isinstance(runtime_generation, str) or not runtime_generation:
            raise FleetRuntimeError("runtime manifest has no runtime_generation")
        if not isinstance(manifest.get("runtime_preflight"), Mapping):
            raise FleetRuntimeError("runtime manifest has no runtime_preflight")

    @staticmethod
    def _tree_identity(root: Path) -> str:
        if root.is_symlink() or not root.is_dir():
            raise FleetRuntimeError(f"required implementation tree is unavailable: {root}")
        digest = hashlib.sha256()
        # Deliberately enumerate the runtime closure: rglob would include generated
        # caches and tests that are not executable dependencies.
        files = FleetRuntime._execution_tree_files(root)
        if not files:
            raise FleetRuntimeError(f"required implementation tree is empty: {root}")
        for path in files:
            relative = path.relative_to(root.parent).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update((path.stat().st_mode & 0o111).to_bytes(2, "big"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    @staticmethod
    def _executable_identity(
        label: str, command: Sequence[str], implementation: str
    ) -> dict[str, str]:
        """Return a runtime identity that deliberately binds content and install path."""
        if not command or not command[0]:
            raise FleetRuntimeError(f"{label} command is empty")
        candidate = Path(command[0])
        resolved = candidate.resolve() if candidate.is_file() else None
        if resolved is None:
            discovered = shutil.which(command[0])
            resolved = Path(discovered).resolve() if discovered else None
        if resolved is None or resolved.is_symlink() or not resolved.is_file():
            raise FleetRuntimeError(f"required executable is unavailable: {label} ({command[0]})")
        try:
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError as exc:
            raise FleetRuntimeError(f"cannot read required executable {label}: {exc}") from exc
        implementation_path = (resolved.parent / implementation).resolve()
        if (
            implementation_path.is_symlink()
            or not implementation_path.is_file()
        ):
            raise FleetRuntimeError(
                f"required implementation is unavailable: {label} ({implementation_path})"
            )
        try:
            implementation_digest = hashlib.sha256(
                implementation_path.read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise FleetRuntimeError(
                f"cannot read required implementation {label}: {exc}"
            ) from exc
        tree_root = resolved.parent.parent
        return {
            "command_path": str(resolved),
            "entry_sha256": digest,
            "implementation_sha256": implementation_digest,
            "tree_sha256": FleetRuntime._tree_identity(tree_root),
        }

    def _capture_hook_source(self) -> bytes:
        hook = self.hook_source
        if hook.is_symlink() or not hook.is_file():
            raise FleetRuntimeError("required hook runtime source is unavailable")
        try:
            payload = hook.read_bytes()
        except OSError as exc:
            raise FleetRuntimeError(f"cannot read hook runtime source: {exc}") from exc
        try:
            compile(payload, str(hook), "exec")
        except (SyntaxError, ValueError, TypeError) as exc:
            raise FleetRuntimeError(
                f"required hook runtime source has invalid Python syntax: {exc}"
            ) from exc
        return payload

    def _preflight_execution(
        self, hook_payload: bytes | None = None
    ) -> dict[str, Any]:
        """Validate every executable before creating fleet or Herdr state."""
        if hook_payload is None:
            hook_payload = self._capture_hook_source()
        hook_sha256 = hashlib.sha256(hook_payload).hexdigest()
        if self.role_catalog is not None and (self.role_catalog.is_symlink() or not self.role_catalog.is_file()):
            raise FleetRuntimeError("required role catalog is unavailable")
        return {
            "core": self._executable_identity(
                "fleet-control", self.core_command, "../fleet_control.py"
            ),
            "adapter": self._executable_identity(
                "fleet-herdr", self.herdr_command, "../herdr_adapter.py"
            ),
            "controller": self._executable_identity(
                "fleet-controller", self.controller_command, "../fleet_controller.py"
            ),
            "hook": {"sha256": hook_sha256},
            "command_arguments": {
                "core": list(self.core_command[1:]),
                "herdr": list(self.herdr_command[1:]),
                "controller": list(self.controller_command[1:]),
            },
        }

    def _preflight_runtime(
        self,
        resolved: ResolvedFleet,
        cwd: str,
        *,
        require_codex_registration: bool = True,
    ) -> dict[str, Any]:
        working_directory = Path(cwd)
        if not working_directory.is_dir():
            raise FleetRuntimeError(f"working directory is unavailable: {cwd}")
        completed = self.runner(
            ["herdr", "--version"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        version = completed.stdout.strip()
        if completed.returncode != 0 or not re.fullmatch(r"herdr 0\.8\.\d+", version):
            raise FleetRuntimeError(
                f"Herdr 0.8 is required before launch (observed={version!r})"
            )
        products = sorted(
            {member["runtime"]["product"] for member in resolved.fleet["spec"]["members"]}
        )
        missing = [product for product in products if shutil.which(product) is None]
        if missing:
            raise FleetRuntimeError(
                "required agent product is unavailable: " + ", ".join(missing)
            )
        preflight: dict[str, Any] = {
            "herdr_version": version,
            "products": products,
            "cwd": str(working_directory.resolve()),
        }
        if require_codex_registration and "codex" in products:
            completed = self.runner(
                ["codex", "plugin", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if completed.returncode != 0:
                raise FleetRuntimeError(
                    "cannot inspect the Codex agent-fleet-session-hooks registration"
                )
            try:
                plugin_catalog = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise FleetRuntimeError(
                    "Codex plugin list returned invalid JSON"
                ) from exc
            installed = (
                plugin_catalog.get("installed")
                if isinstance(plugin_catalog, Mapping)
                else None
            )
            registration = next(
                (
                    item
                    for item in installed or []
                    if isinstance(item, Mapping)
                    and item.get("pluginId")
                    == "agent-fleet-session-hooks@agent-fleet"
                    and item.get("installed") is True
                ),
                None,
            )
            if registration is None:
                raise FleetRuntimeError(
                    "Codex plugin agent-fleet-session-hooks@agent-fleet must be installed "
                    "before creating Codex panes"
                )
            preflight["codex_hook_registration"] = {
                "plugin_id": "agent-fleet-session-hooks@agent-fleet",
                "version": registration.get("version"),
            }
        return preflight

    @staticmethod
    def _assert_runtime_identity(current: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
        if "execution_identity" not in current:
            raise FleetRuntimeError("runtime identity conflict: the existing Fleet has a legacy manifest with no recorded identity. Start with a new fleet ID, or remove this runtime using the same installed version.")
        if current.get("execution_identity") != expected:
            raise FleetRuntimeError(
                "runtime identity conflict: the existing Fleet was started with a "
                f"different executable identity (actual={current.get('execution_identity')!r}, "
                f"expected={expected!r}). Start with a new fleet ID, or remove the existing runtime."
            )

    def _assert_config_snapshot(self, resolved: ResolvedFleet) -> None:
        """Reject source changes made after the three documents were composed."""
        checks: list[tuple[str, Path, str]] = [
            ("Fleet", resolved.fleet_path, resolved.fleet_source_hash),
            ("ViewProfile", resolved.profile_path, resolved.profile_source_hash),
        ]
        if resolved.launch_path is not None and resolved.launch_source_hash is not None:
            checks.append(
                ("LaunchProfile", resolved.launch_path, resolved.launch_source_hash)
            )
        if self.role_catalog is not None and resolved.role_catalog_hash is not None:
            checks.append(
                ("role catalog", self.role_catalog, resolved.role_catalog_hash)
            )
        for label, path, expected in checks:
            if _content_hash(_load_document(path)) != expected:
                raise FleetRuntimeError(
                    f"configuration changed during launch preflight: {label} ({path})"
                )

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
