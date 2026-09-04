#!/usr/bin/env python3
"""実行基盤に依存しないagent起動コマンド設定を検査・解決する。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping


PROFILE_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[a-z][a-z0-9]*(?:-[a-z0-9]+)*)?$"
)
PROFILE_REF_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[a-z][a-z0-9]*(?:-[a-z0-9]+)*)?@[1-9][0-9]*$"
)
COMMAND_PATTERN = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_.+-]*|/(?:[A-Za-z0-9_.+-]+/)*[A-Za-z0-9_.+-]+)$"
)


class AgentCommandProfileError(RuntimeError):
    """AgentCommandProfileを安全に解決できない。"""


def _unknown_keys(
    value: Mapping[str, Any], allowed: set[str], path: str, errors: list[str]
) -> None:
    for key in sorted(set(value) - allowed):
        errors.append(f"{path}.{key}: is not allowed")


def validate_document(document: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, Mapping):
        return ["$: must be an object"]
    _unknown_keys(document, {"apiVersion", "kind", "metadata", "spec"}, "$", errors)
    if document.get("apiVersion") != "fleet.runtime.harness/v1":
        errors.append("$.apiVersion: must be 'fleet.runtime.harness/v1'")
    if document.get("kind") != "AgentCommandProfile":
        errors.append("$.kind: must be 'AgentCommandProfile'")

    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        errors.append("$.metadata: must be an object")
    else:
        _unknown_keys(metadata, {"id", "version"}, "$.metadata", errors)
        profile_id = metadata.get("id")
        if not isinstance(profile_id, str) or PROFILE_ID_PATTERN.fullmatch(profile_id) is None:
            errors.append("$.metadata.id: must be a lowercase profile identifier")
        version = metadata.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            errors.append("$.metadata.version: must be a positive integer")

    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        errors.append("$.spec: must be an object")
        return sorted(set(errors))
    _unknown_keys(spec, {"product", "command"}, "$.spec", errors)
    if spec.get("product") not in {"codex", "claude"}:
        errors.append("$.spec.product: must be 'codex' or 'claude'")
    command = spec.get("command")
    if not isinstance(command, str) or COMMAND_PATTERN.fullmatch(command) is None:
        errors.append(
            "$.spec.command: must be one shell command name or an absolute executable path"
        )
    return sorted(set(errors))


def profile_identity(document: Mapping[str, Any]) -> str:
    errors = validate_document(document)
    if errors:
        raise AgentCommandProfileError(
            "invalid AgentCommandProfile: " + "; ".join(errors)
        )
    return f"{document['metadata']['id']}@{document['metadata']['version']}"


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        ruby = shutil.which("ruby")
        if ruby is None:
            raise AgentCommandProfileError(
                "YAML parser unavailable: install PyYAML or Ruby"
            )
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
            raise AgentCommandProfileError(
                f"invalid AgentCommandProfile YAML {path}: {result.stderr.strip()}"
            )
        return json.loads(result.stdout)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class AgentCommandProfileCatalog:
    def __init__(self, documents: Iterable[tuple[Path, Mapping[str, Any]]]):
        self._profiles: dict[str, tuple[Path, Mapping[str, Any]]] = {}
        for path, document in documents:
            identity = profile_identity(document)
            if identity in self._profiles:
                raise AgentCommandProfileError(
                    f"duplicate AgentCommandProfile identity: {identity}"
                )
            self._profiles[identity] = (path, document)

    @classmethod
    def from_directories(
        cls, directories: Iterable[Path]
    ) -> "AgentCommandProfileCatalog":
        documents: list[tuple[Path, Mapping[str, Any]]] = []
        for directory in directories:
            if not directory.is_dir():
                continue
            paths = sorted(
                {
                    *directory.glob("*.yml"),
                    *directory.glob("*.yaml"),
                    *directory.glob("*.json"),
                },
                key=str,
            )
            for path in paths:
                document = _load_yaml(path)
                if not isinstance(document, Mapping):
                    raise AgentCommandProfileError(
                        f"AgentCommandProfile root must be an object: {path}"
                    )
                documents.append((path.resolve(), document))
        return cls(documents)

    def resolve(self, profile_ref: str) -> tuple[Path, Mapping[str, Any]]:
        if not isinstance(profile_ref, str) or PROFILE_REF_PATTERN.fullmatch(profile_ref) is None:
            raise AgentCommandProfileError(
                "AgentCommandProfile reference must match "
                "'<namespace/>name@<positive-version>'"
            )
        try:
            return self._profiles[profile_ref]
        except KeyError as exc:
            raise AgentCommandProfileError(
                f"AgentCommandProfile not found: {profile_ref}"
            ) from exc

    def entries(self) -> list[tuple[str, Path, Mapping[str, Any]]]:
        return [
            (identity, path, document)
            for identity, (path, document) in sorted(self._profiles.items())
        ]
