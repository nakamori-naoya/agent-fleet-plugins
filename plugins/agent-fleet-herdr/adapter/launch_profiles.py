#!/usr/bin/env python3
"""Herdr固有の起動設定を検査し、FleetとView Profileを合成する。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping


IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
PROFILE_REF_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[a-z][a-z0-9]*(?:-[a-z0-9]+)*)?@[1-9][0-9]*$"
)


class LaunchProfileError(RuntimeError):
    """Herdr LaunchProfileを安全に解決できない。"""


def _unknown_keys(
    value: Mapping[str, Any], allowed: set[str], path: str, errors: list[str]
) -> None:
    for key in sorted(set(value) - allowed):
        errors.append(f"{path}.{key}: is not allowed")


def _identifier(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        errors.append(f"{path}: must be a lowercase identifier")


def validate_document(document: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, Mapping):
        return ["$: must be an object"]
    _unknown_keys(document, {"apiVersion", "kind", "metadata", "spec"}, "$", errors)
    if document.get("apiVersion") != "fleet.herdr.harness/v1":
        errors.append("$.apiVersion: must be 'fleet.herdr.harness/v1'")
    if document.get("kind") != "LaunchProfile":
        errors.append("$.kind: must be 'LaunchProfile'")

    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        errors.append("$.metadata: must be an object")
    else:
        _unknown_keys(metadata, {"id"}, "$.metadata", errors)
        _identifier(metadata.get("id"), "$.metadata.id", errors)

    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        errors.append("$.spec: must be an object")
        return sorted(set(errors))
    _unknown_keys(
        spec,
        {
            "fleet_ref",
            "view_profile_ref",
            "codex_hook_trust",
            "agent_command_profiles",
        },
        "$.spec",
        errors,
    )
    _identifier(spec.get("fleet_ref"), "$.spec.fleet_ref", errors)
    view_ref = spec.get("view_profile_ref")
    if not isinstance(view_ref, str) or PROFILE_REF_PATTERN.fullmatch(view_ref) is None:
        errors.append(
            "$.spec.view_profile_ref: must match "
            "'<namespace/>name@<positive-version>'"
        )
    if spec.get("codex_hook_trust") not in {"preapproved", "review"}:
        errors.append(
            "$.spec.codex_hook_trust: must be 'preapproved' or 'review'"
        )
    command_profiles = spec.get("agent_command_profiles", {})
    if not isinstance(command_profiles, Mapping):
        errors.append("$.spec.agent_command_profiles: must be an object")
    else:
        for agent_ref, profile_ref in command_profiles.items():
            if not isinstance(agent_ref, str) or IDENTIFIER_PATTERN.fullmatch(agent_ref) is None:
                errors.append(
                    "$.spec.agent_command_profiles: keys must be lowercase agent identifiers"
                )
                continue
            if not isinstance(profile_ref, str) or PROFILE_REF_PATTERN.fullmatch(profile_ref) is None:
                errors.append(
                    f"$.spec.agent_command_profiles.{agent_ref}: must match "
                    "'<namespace/>name@<positive-version>'"
                )
    return sorted(set(errors))


def profile_identity(document: Mapping[str, Any]) -> str:
    errors = validate_document(document)
    if errors:
        raise LaunchProfileError("invalid LaunchProfile: " + "; ".join(errors))
    return str(document["metadata"]["id"])


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        ruby = shutil.which("ruby")
        if ruby is None:
            raise LaunchProfileError("YAML parser unavailable: install PyYAML or Ruby")
        program = (
            "require 'yaml'; require 'json'; "
            "data=YAML.safe_load(STDIN.read, permitted_classes: [], "
            "permitted_symbols: [], aliases: false); STDOUT.write(JSON.generate(data))"
        )
        result = subprocess.run(
            [ruby, "-e", program],
            input=path.read_text(encoding="utf-8"),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise LaunchProfileError(
                f"invalid LaunchProfile YAML {path}: {result.stderr.strip()}"
            )
        return json.loads(result.stdout)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class LaunchProfileCatalog:
    def __init__(self, documents: Iterable[tuple[Path, Mapping[str, Any]]]):
        self._profiles: dict[str, tuple[Path, Mapping[str, Any]]] = {}
        for path, document in documents:
            identity = profile_identity(document)
            if identity in self._profiles:
                raise LaunchProfileError(
                    f"duplicate LaunchProfile identity: {identity}"
                )
            self._profiles[identity] = (path, document)

    @classmethod
    def from_directories(cls, directories: Iterable[Path]) -> "LaunchProfileCatalog":
        documents: list[tuple[Path, Mapping[str, Any]]] = []
        for directory in directories:
            if not directory.is_dir():
                continue
            paths = sorted(
                {*directory.glob("*.yml"), *directory.glob("*.yaml"), *directory.glob("*.json")},
                key=str,
            )
            for path in paths:
                document = _load_yaml(path)
                if not isinstance(document, Mapping):
                    raise LaunchProfileError(
                        f"LaunchProfile root must be an object: {path}"
                    )
                documents.append((path.resolve(), document))
        return cls(documents)

    def resolve(self, launch_id: str) -> tuple[Path, Mapping[str, Any]]:
        _errors: list[str] = []
        _identifier(launch_id, "launch_id", _errors)
        if _errors:
            raise LaunchProfileError(_errors[0])
        try:
            return self._profiles[launch_id]
        except KeyError as exc:
            raise LaunchProfileError(f"LaunchProfile not found: {launch_id}") from exc

    def entries(self) -> list[tuple[str, Path, Mapping[str, Any]]]:
        return [
            (identity, path, document)
            for identity, (path, document) in sorted(self._profiles.items())
        ]
