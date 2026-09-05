#!/usr/bin/env python3
"""Capture, freeze and validate execution identities before any runtime mutation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence



from runtime_models import MANIFEST_FORMAT_VERSION, RUNTIME_PHASES, FleetRuntimeError, ExecutionBundle, ResolvedFleet, _content_hash, _load_document


class ExecutionIdentity:
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
                root / "core_contract.py",
                root / "command_delivery.py",
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
                root / "agent_command_profiles.py",
                root / "view_profiles.py",
                root / "scripts" / "fleet-controller",
                root / "scripts" / "fleet-herdr",
                root / "schema" / "launch-profile.schema.yml",
                root / "schema" / "agent-command-profile.schema.yml",
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
        ExecutionIdentity._remove_execution_tree(bundle.root)
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
        files = ExecutionIdentity._execution_tree_files(root)
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
            "tree_sha256": ExecutionIdentity._tree_identity(tree_root),
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
        require_agent_launch: bool = True,
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
        unprofiled_products = {
            member["runtime"]["product"]
            for member in resolved.fleet["spec"]["members"]
            if str(member["agent_ref"]) not in resolved.agent_command_profiles
        }
        commands_by_agent: dict[str, str] = {}
        for member in resolved.fleet["spec"]["members"]:
            agent_ref = str(member["agent_ref"])
            profile = resolved.agent_command_profiles.get(agent_ref)
            command = (
                str(profile["command"])
                if profile is not None
                else str(member["runtime"]["product"])
            )
            commands_by_agent[agent_ref] = command
        if require_agent_launch:
            profiled_commands = sorted(
                {
                    profile["command"]
                    for profile in resolved.agent_command_profiles.values()
                }
            )
            for command in profiled_commands:
                completed = self.runner(
                    self._interactive_shell_argv("command", ["-v", command]),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if completed.returncode != 0:
                    raise FleetRuntimeError(
                        "AgentCommandProfile command is unavailable in the "
                        f"interactive shell: {command}"
                    )
        if require_agent_launch:
            missing = sorted(
                product
                for product in unprofiled_products
                if shutil.which(product) is None
            )
            if missing:
                raise FleetRuntimeError(
                    "required agent product is unavailable: " + ", ".join(missing)
                )
        preflight: dict[str, Any] = {
            "herdr_version": version,
            "products": products,
            "agent_commands": commands_by_agent,
            "cwd": str(working_directory.resolve()),
        }
        if require_agent_launch and "claude" in products:
            claude_commands = sorted(
                {
                    commands_by_agent[str(member["agent_ref"])]
                    for member in resolved.fleet["spec"]["members"]
                    if member["runtime"]["product"] == "claude"
                }
            )
            authentications = []
            for command in claude_commands:
                argv = (
                    ["claude", "auth", "status"]
                    if command == "claude"
                    else self._interactive_shell_argv(command, ["auth", "status"])
                )
                completed = self.runner(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                try:
                    authentication = json.loads(completed.stdout)
                except json.JSONDecodeError as exc:
                    raise FleetRuntimeError(
                        f"Claude auth status returned invalid JSON through {command}"
                    ) from exc
                if (
                    completed.returncode != 0
                    or not isinstance(authentication, Mapping)
                    or authentication.get("loggedIn") is not True
                ):
                    raise FleetRuntimeError(
                        f"Claude command {command} is not authenticated; run "
                        f"'{command} auth login' before starting the Fleet"
                    )
                authentications.append(
                    {
                        "command": command,
                        "auth_method": authentication.get("authMethod"),
                    }
                )
            preflight["claude_authentications"] = authentications
        if require_agent_launch and require_codex_registration and "codex" in products:
            codex_commands = sorted(
                {
                    commands_by_agent[str(member["agent_ref"])]
                    for member in resolved.fleet["spec"]["members"]
                    if member["runtime"]["product"] == "codex"
                }
            )
            registrations = []
            for command in codex_commands:
                argv = (
                    ["codex", "plugin", "list", "--json"]
                    if command == "codex"
                    else self._interactive_shell_argv(
                        command, ["plugin", "list", "--json"]
                    )
                )
                completed = self.runner(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                if completed.returncode != 0:
                    raise FleetRuntimeError(
                        "cannot inspect the Codex agent-fleet-herdr "
                        f"registration through {command}"
                    )
                try:
                    plugin_catalog = json.loads(completed.stdout)
                except json.JSONDecodeError as exc:
                    raise FleetRuntimeError(
                        f"Codex plugin list returned invalid JSON through {command}"
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
                        == "agent-fleet-herdr@agent-fleet"
                        and item.get("installed") is True
                    ),
                    None,
                )
                if registration is None:
                    raise FleetRuntimeError(
                        "Codex plugin agent-fleet-herdr@agent-fleet must be "
                        f"installed for {command} before creating Codex panes"
                    )
                registrations.append(
                    {
                        "command": command,
                        "plugin_id": "agent-fleet-herdr@agent-fleet",
                        "version": registration.get("version"),
                    }
                )
            preflight["codex_hook_registrations"] = registrations
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
        for source in resolved.agent_command_profile_sources:
            checks.append(
                (
                    f"AgentCommandProfile {source['profile_ref']}",
                    Path(source["path"]),
                    source["hash"],
                )
            )
        for label, path, expected in checks:
            if _content_hash(_load_document(path)) != expected:
                raise FleetRuntimeError(
                    f"configuration changed during launch preflight: {label} ({path})"
                )

