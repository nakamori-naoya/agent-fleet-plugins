#!/usr/bin/env python3
"""Runtime values, immutable execution bundle and configuration identity."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence



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
        agent_command_profiles: Mapping[str, Mapping[str, str]],
        agent_command_profile_sources: Sequence[Mapping[str, str]],
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
        self.agent_command_profiles = {
            agent_ref: dict(profile)
            for agent_ref, profile in agent_command_profiles.items()
        }
        self.agent_command_profile_sources = tuple(
            dict(source) for source in agent_command_profile_sources
        )
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
                "agent_command_profiles": self.agent_command_profiles,
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


