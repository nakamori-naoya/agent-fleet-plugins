#!/usr/bin/env python3
"""Validation and exact-version catalog for Herdr View Profiles."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROFILE_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[a-z][a-z0-9]*(?:-[a-z0-9]+)*)?$"
)
PROFILE_REF_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[a-z][a-z0-9]*(?:-[a-z0-9]+)*)?@[1-9][0-9]*$"
)
IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
SUPPORTED_API_VERSIONS = frozenset(
    {"fleet.herdr.harness/v1", "fleet.herdr.harness/v2"}
)
FORBIDDEN_KEYS = frozenset(
    {"fleet", "fleet_id", "fleet_ref", "pane", "pane_id", "workspace_id", "tab_id"}
)


class ViewProfileError(RuntimeError):
    """A deterministic View Profile validation or resolution error."""


@dataclass(frozen=True)
class ResolvedLayoutGroup:
    group_id: str
    member_refs: tuple[str, ...]
    weight: int
    direction: str


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


def _positive_weight(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        errors.append(f"{path}: must be a positive integer")


def _safe_identifier(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        errors.append(f"{path}: must be a lowercase identifier")


def _identifier_list(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{path}: must be a non-empty list")
        return
    seen: set[str] = set()
    for index, item in enumerate(value):
        _safe_identifier(item, f"{path}[{index}]", errors)
        if isinstance(item, str) and item in seen:
            errors.append(f"{path}[{index}]: duplicate identifier {item!r}")
        elif isinstance(item, str):
            seen.add(item)


def _validate_v1_layout(layout: Mapping[str, Any], errors: list[str]) -> None:
    children = layout.get("children")
    if not isinstance(children, list) or len(children) != 2:
        errors.append("$.spec.layout.children: must contain manager slot and member stack")
        return
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
            errors.append("$.spec.layout.children[0]: must be a slot selecting manager")
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
            _positive_weight(
                child.get("weight"),
                f"$.spec.layout.children[{index}].weight",
                errors,
            )


def _validate_v2_layout(layout: Mapping[str, Any], errors: list[str]) -> None:
    children = layout.get("children")
    if not isinstance(children, list) or not 2 <= len(children) <= 8:
        errors.append("$.spec.layout.children: must contain between 2 and 8 stacks")
        return
    group_ids: set[str] = set()
    remaining_indexes: list[int] = []
    for index, child in enumerate(children):
        path = f"$.spec.layout.children[{index}]"
        if not isinstance(child, Mapping):
            errors.append(f"{path}: must be an object")
            continue
        _unknown_keys(
            child,
            {"type", "id", "selector", "weight", "direction", "distribution"},
            path,
            errors,
        )
        if child.get("type") != "stack":
            errors.append(f"{path}.type: must be 'stack'")
        group_id = child.get("id")
        _safe_identifier(group_id, f"{path}.id", errors)
        if isinstance(group_id, str) and group_id in group_ids:
            errors.append(f"{path}.id: duplicate group id {group_id!r}")
        elif isinstance(group_id, str):
            group_ids.add(group_id)
        _positive_weight(child.get("weight"), f"{path}.weight", errors)
        if child.get("direction") not in {"horizontal", "vertical"}:
            errors.append(f"{path}.direction: must be horizontal or vertical")
        if child.get("distribution") != "equal":
            errors.append(f"{path}.distribution: must be equal")

        selector = child.get("selector")
        selector_path = f"{path}.selector"
        if not isinstance(selector, Mapping):
            errors.append(f"{selector_path}: must be an object")
            continue
        _unknown_keys(
            selector,
            {"role_ids", "agent_refs", "remaining"},
            selector_path,
            errors,
        )
        modes = [
            key for key in ("role_ids", "agent_refs", "remaining") if key in selector
        ]
        if len(modes) != 1:
            errors.append(
                f"{selector_path}: must contain exactly one of role_ids, agent_refs, remaining"
            )
            continue
        mode = modes[0]
        if mode in {"role_ids", "agent_refs"}:
            _identifier_list(selector[mode], f"{selector_path}.{mode}", errors)
        elif selector[mode] is not True:
            errors.append(f"{selector_path}.remaining: must be true")
        else:
            remaining_indexes.append(index)
    if len(remaining_indexes) > 1:
        errors.append("$.spec.layout.children: remaining selector may appear only once")
    if remaining_indexes and remaining_indexes[0] != len(children) - 1:
        errors.append("$.spec.layout.children: remaining selector must be last")


def validate_document(document: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, Mapping):
        return ["$: must be an object"]
    _unknown_keys(document, {"apiVersion", "kind", "metadata", "spec"}, "$", errors)
    api_version = document.get("apiVersion")
    if api_version not in SUPPORTED_API_VERSIONS:
        errors.append("$.apiVersion: must be fleet.herdr.harness/v1 or v2")
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
        if api_version == "fleet.herdr.harness/v1":
            _validate_v1_layout(layout, errors)
        elif api_version == "fleet.herdr.harness/v2":
            _validate_v2_layout(layout, errors)

    _forbidden_keys(document, "$", errors)
    return sorted(set(errors))


def resolve_layout_groups(
    document: Mapping[str, Any],
    members: Sequence[tuple[str, str]],
    manager_ref: str,
) -> tuple[ResolvedLayoutGroup, ...]:
    errors = validate_document(document)
    if errors:
        raise ViewProfileError("invalid View Profile: " + "; ".join(errors))
    member_refs = tuple(agent_ref for agent_ref, _ in members)
    role_by_agent = {
        agent_ref: role_ref.rsplit("@", 1)[0] for agent_ref, role_ref in members
    }
    layout = document["spec"]["layout"]
    if document["apiVersion"] == "fleet.herdr.harness/v1":
        manager_slot, member_stack = layout["children"]
        non_manager_refs = tuple(ref for ref in member_refs if ref != manager_ref)
        if not non_manager_refs:
            raise ViewProfileError(
                "View Profile non-manager stack requires at least one member"
            )
        return (
            ResolvedLayoutGroup(
                manager_slot.get("pane_slot", "manager"),
                (manager_ref,),
                manager_slot["weight"],
                "vertical",
            ),
            ResolvedLayoutGroup(
                member_stack.get("pane_slot_prefix", "members"),
                non_manager_refs,
                member_stack["weight"],
                member_stack["direction"],
            ),
        )

    known_refs = set(member_refs)
    assigned: set[str] = set()
    groups: list[ResolvedLayoutGroup] = []
    for child in layout["children"]:
        selector = child["selector"]
        if "agent_refs" in selector:
            requested = tuple(selector["agent_refs"])
            unknown = tuple(ref for ref in requested if ref not in known_refs)
            if unknown:
                raise ViewProfileError(
                    "View Profile agent_ref "
                    + ", ".join(repr(ref) for ref in unknown)
                    + " does not exist in the Fleet"
                )
            selected = requested
        elif "role_ids" in selector:
            role_ids = set(selector["role_ids"])
            selected = tuple(
                ref for ref in member_refs if role_by_agent[ref] in role_ids
            )
        else:
            selected = tuple(ref for ref in member_refs if ref not in assigned)
        duplicates = tuple(ref for ref in selected if ref in assigned)
        if duplicates:
            raise ViewProfileError(
                "View Profile assigns member "
                + ", ".join(repr(ref) for ref in duplicates)
                + " to more than one layout group"
            )
        if not selected:
            raise ViewProfileError(
                f"View Profile layout group {child['id']!r} selects no Fleet members"
            )
        assigned.update(selected)
        groups.append(
            ResolvedLayoutGroup(
                child["id"],
                selected,
                child["weight"],
                child["direction"],
            )
        )
    unassigned = tuple(ref for ref in member_refs if ref not in assigned)
    if unassigned:
        raise ViewProfileError(
            "View Profile members "
            + ", ".join(repr(ref) for ref in unassigned)
            + " are not assigned to a layout group"
        )
    return tuple(groups)


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
