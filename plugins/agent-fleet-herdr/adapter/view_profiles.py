#!/usr/bin/env python3
"""Validation and exact-version catalog for Herdr View Profiles."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping


PROFILE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)?$")
PROFILE_REF_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)?@[1-9][0-9]*$"
)
FORBIDDEN_KEYS = frozenset(
    {"fleet", "fleet_id", "fleet_ref", "pane", "pane_id", "workspace_id", "tab_id"}
)


class ViewProfileError(RuntimeError):
    """A deterministic View Profile validation or resolution error."""


def _unknown_keys(
    value: Mapping[str, Any], allowed: set[str], path: str, errors: list[str]
) -> None:
    for key in sorted(set(value) - allowed):
        errors.append(f"{path}.{key}: is not allowed")


def _forbidden_keys(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in FORBIDDEN_KEYS:
                errors.append(f"{child_path}: is not allowed in a View Profile")
            _forbidden_keys(child, child_path, errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _forbidden_keys(child, f"{path}[{index}]", errors)


def validate_document(document: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, Mapping):
        return ["$: must be an object"]
    _unknown_keys(document, {"apiVersion", "kind", "metadata", "spec"}, "$", errors)
    if document.get("apiVersion") != "fleet.herdr.harness/v1":
        errors.append("$.apiVersion: must be 'fleet.herdr.harness/v1'")
    if document.get("kind") != "ViewProfile":
        errors.append("$.kind: must be 'ViewProfile'")

    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        errors.append("$.metadata: must be an object")
    else:
        _unknown_keys(metadata, {"id", "version"}, "$.metadata", errors)
        profile_id = metadata.get("id")
        if not isinstance(profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(profile_id):
            errors.append("$.metadata.id: must be a lowercase profile identity")
        version = metadata.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            errors.append("$.metadata.version: must be a positive integer")

    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        errors.append("$.spec: must be an object")
        return errors
    _unknown_keys(spec, {"constraints", "layout"}, "$.spec", errors)
    constraints = spec.get("constraints")
    if not isinstance(constraints, Mapping):
        errors.append("$.spec.constraints: must be an object")
    else:
        _unknown_keys(
            constraints, {"min_members", "max_members"}, "$.spec.constraints", errors
        )
        minimum = constraints.get("min_members")
        maximum = constraints.get("max_members")
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            errors.append("$.spec.constraints.min_members: must be a positive integer")
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
            errors.append("$.spec.constraints.max_members: must be a positive integer")
        if isinstance(minimum, int) and isinstance(maximum, int) and minimum > maximum:
            errors.append("$.spec.constraints: min_members must not exceed max_members")

    layout = spec.get("layout")
    if not isinstance(layout, Mapping):
        errors.append("$.spec.layout: must be an object")
    else:
        _unknown_keys(layout, {"type", "direction", "children"}, "$.spec.layout", errors)
        if layout.get("type") != "split":
            errors.append("$.spec.layout.type: must be 'split'")
        if layout.get("direction") not in {"horizontal", "vertical"}:
            errors.append("$.spec.layout.direction: must be horizontal or vertical")
        children = layout.get("children")
        if not isinstance(children, list) or len(children) != 2:
            errors.append("$.spec.layout.children: must contain manager slot and member stack")
        else:
            slot, stack = children
            if not isinstance(slot, Mapping):
                errors.append("$.spec.layout.children[0]: must be an object")
            else:
                _unknown_keys(
                    slot,
                    {"type", "selector", "weight", "pane_slot"},
                    "$.spec.layout.children[0]",
                    errors,
                )
                if slot.get("type") != "slot" or slot.get("selector") != "manager":
                    errors.append(
                        "$.spec.layout.children[0]: must be a slot selecting manager"
                    )
                if "pane_slot" in slot and (
                    not isinstance(slot.get("pane_slot"), str) or not slot.get("pane_slot")
                ):
                    errors.append("$.spec.layout.children[0].pane_slot: must be non-empty")
            if not isinstance(stack, Mapping):
                errors.append("$.spec.layout.children[1]: must be an object")
            else:
                _unknown_keys(
                    stack,
                    {
                        "type",
                        "selector",
                        "weight",
                        "direction",
                        "distribution",
                        "pane_slot_prefix",
                    },
                    "$.spec.layout.children[1]",
                    errors,
                )
                if stack.get("type") != "stack" or stack.get("selector") != "non-manager":
                    errors.append(
                        "$.spec.layout.children[1]: must be a stack selecting non-manager"
                    )
                if stack.get("direction") not in {"horizontal", "vertical"}:
                    errors.append(
                        "$.spec.layout.children[1].direction: must be horizontal or vertical"
                    )
                if stack.get("distribution") != "equal":
                    errors.append("$.spec.layout.children[1].distribution: must be equal")
                if "pane_slot_prefix" in stack and (
                    not isinstance(stack.get("pane_slot_prefix"), str)
                    or not stack.get("pane_slot_prefix")
                ):
                    errors.append(
                        "$.spec.layout.children[1].pane_slot_prefix: must be non-empty"
                    )
            for index, child in enumerate(children):
                if isinstance(child, Mapping):
                    weight = child.get("weight")
                    if not isinstance(weight, int) or isinstance(weight, bool) or weight < 1:
                        errors.append(
                            f"$.spec.layout.children[{index}].weight: must be a positive integer"
                        )

    _forbidden_keys(document, "$", errors)
    return sorted(set(errors))


def profile_identity(document: Mapping[str, Any]) -> str:
    errors = validate_document(document)
    if errors:
        raise ViewProfileError("invalid View Profile: " + "; ".join(errors))
    metadata = document["metadata"]
    return f"{metadata['id']}@{metadata['version']}"


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        ruby = shutil.which("ruby")
        if ruby is None:
            raise ViewProfileError("YAML parser unavailable: install PyYAML or Ruby")
        program = (
            "require 'yaml'; require 'json'; "
            "data=YAML.safe_load(STDIN.read, permitted_classes: [], permitted_symbols: [], aliases: false); "
            "STDOUT.write(JSON.generate(data))"
        )
        result = subprocess.run(
            [ruby, "-e", program],
            input=path.read_text(encoding="utf-8"),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ViewProfileError(f"invalid View Profile YAML {path}: {result.stderr.strip()}")
        return json.loads(result.stdout)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class ViewProfileCatalog:
    def __init__(self, documents: Iterable[Mapping[str, Any]]):
        self._profiles: dict[str, Mapping[str, Any]] = {}
        for document in documents:
            identity = profile_identity(document)
            if identity in self._profiles:
                raise ViewProfileError(f"duplicate View Profile identity: {identity}")
            self._profiles[identity] = document

    @classmethod
    def from_directories(cls, directories: Iterable[Path]) -> "ViewProfileCatalog":
        documents: list[Mapping[str, Any]] = []
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
                document = _load_yaml(path)
                if not isinstance(document, Mapping):
                    raise ViewProfileError(f"View Profile root must be an object: {path}")
                documents.append(document)
        return cls(documents)

    def resolve(self, profile_ref: str) -> Mapping[str, Any]:
        if not PROFILE_REF_PATTERN.fullmatch(profile_ref):
            raise ViewProfileError(f"invalid versioned View Profile reference: {profile_ref}")
        try:
            return self._profiles[profile_ref]
        except KeyError as exc:
            raise ViewProfileError(f"View Profile not found: {profile_ref}") from exc

    def identities(self) -> list[str]:
        return sorted(self._profiles)
