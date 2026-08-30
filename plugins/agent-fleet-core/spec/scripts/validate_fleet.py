#!/usr/bin/env python3
"""Validate Fleet YAML and emit the normalized subprocess contract as JSON."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


class FleetLoadError(Exception):
    """Raised when a Fleet YAML document cannot be loaded safely."""


ROLE_REF_PATTERN = re.compile(
    r"^(manager|advisor|worker|reviewer|researcher)@[1-9][0-9]*$"
)


def _load_with_ruby(path: Path) -> Any:
    ruby = shutil.which("ruby")
    if ruby is None:
        raise FleetLoadError(
            "YAML parser unavailable: install PyYAML or provide Ruby with standard yaml"
        )
    program = (
        "require 'yaml'; require 'json'; "
        "data = YAML.safe_load(STDIN.read, permitted_classes: [], "
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
        detail = (
            result.stderr.strip().splitlines()[-1]
            if result.stderr.strip()
            else "unknown error"
        )
        raise FleetLoadError(f"invalid YAML: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise FleetLoadError(f"Ruby YAML bridge returned invalid JSON: {error}") from error


def load_yaml(path: Path) -> Any:
    """Load YAML safely, falling back to Ruby's standard yaml implementation."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return _load_with_ruby(path)

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise FleetLoadError(f"invalid YAML: {error}") from error


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _mapping(value: Any, path: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be a mapping")
        return None
    if any(not isinstance(key, str) for key in value):
        errors.append(f"{path}: all field names must be strings")
        return None
    return value


def _keys(
    value: dict[str, Any],
    path: str,
    required: set[str],
    allowed: set[str],
    errors: list[str],
) -> None:
    for key in sorted(required - value.keys()):
        errors.append(f"{path}.{key}: is required")
    for key in sorted(value.keys() - allowed):
        errors.append(f"{path}.{key}: is not allowed")


def _string(value: Any, path: str, errors: list[str]) -> None:
    if not _non_empty_string(value):
        errors.append(f"{path}: must be a non-empty string")


def _members(value: Any, errors: list[str]) -> dict[str, str]:
    path = "spec.members"
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list")
        return {}
    if not value:
        errors.append(f"{path}: must contain at least one member")

    members_by_ref: dict[str, str] = {}
    for index, raw_member in enumerate(value):
        member_path = f"{path}[{index}]"
        member = _mapping(raw_member, member_path, errors)
        if member is None:
            continue
        _keys(
            member,
            member_path,
            {"agent_ref", "role_ref"},
            {"agent_ref", "role_ref", "model"},
            errors,
        )
        for field in ("agent_ref", "role_ref"):
            if field in member:
                _string(member[field], f"{member_path}.{field}", errors)
        role_ref = member.get("role_ref")
        if _non_empty_string(role_ref) and ROLE_REF_PATTERN.fullmatch(role_ref) is None:
            errors.append(
                f"{member_path}.role_ref: must match "
                "'^(manager|advisor|worker|reviewer|researcher)@[1-9][0-9]*$'"
            )
        if "model" in member:
            _string(member["model"], f"{member_path}.model", errors)
        agent_ref = member.get("agent_ref")
        if _non_empty_string(agent_ref):
            if agent_ref in members_by_ref:
                errors.append(f"{member_path}.agent_ref: duplicate agent_ref {agent_ref!r}")
            else:
                members_by_ref[agent_ref] = role_ref if isinstance(role_ref, str) else ""
    return members_by_ref


def _tasks(value: Any, members_by_ref: dict[str, str], errors: list[str]) -> None:
    path = "spec.tasks"
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list")
        return

    task_ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    task_paths: dict[str, str] = {}
    for index, raw_task in enumerate(value):
        task_path = f"{path}[{index}]"
        task = _mapping(raw_task, task_path, errors)
        if task is None:
            continue
        _keys(
            task,
            task_path,
            {"id", "assignee", "depends_on", "instructions"},
            {"id", "assignee", "depends_on", "instructions"},
            errors,
        )
        for field in ("id", "assignee", "instructions"):
            if field in task:
                _string(task[field], f"{task_path}.{field}", errors)

        task_id = task.get("id")
        if _non_empty_string(task_id):
            if task_id in task_ids:
                errors.append(f"{task_path}.id: duplicate task id {task_id!r}")
            else:
                task_ids.add(task_id)
                task_paths[task_id] = task_path

        assignee = task.get("assignee")
        if _non_empty_string(assignee) and assignee not in members_by_ref:
            errors.append(f"{task_path}.assignee: unknown agent_ref {assignee!r}")

        raw_dependencies = task.get("depends_on")
        valid_dependencies: list[str] = []
        if not isinstance(raw_dependencies, list):
            if "depends_on" in task:
                errors.append(f"{task_path}.depends_on: must be a list")
        else:
            seen: set[str] = set()
            for dep_index, dependency in enumerate(raw_dependencies):
                dep_path = f"{task_path}.depends_on[{dep_index}]"
                _string(dependency, dep_path, errors)
                if not _non_empty_string(dependency):
                    continue
                if dependency in seen:
                    errors.append(f"{dep_path}: duplicate dependency {dependency!r}")
                else:
                    seen.add(dependency)
                    valid_dependencies.append(dependency)
        if _non_empty_string(task_id) and task_id not in dependencies:
            dependencies[task_id] = valid_dependencies

    for task_id in sorted(dependencies):
        for dependency in dependencies[task_id]:
            if dependency not in task_ids:
                errors.append(f"{task_paths[task_id]}.depends_on: unknown task id {dependency!r}")

    state: dict[str, int] = {}
    reported_cycles: set[tuple[str, ...]] = set()

    def visit(task_id: str, trail: list[str]) -> None:
        if state.get(task_id) == 2:
            return
        if state.get(task_id) == 1:
            start = trail.index(task_id)
            cycle = trail[start:] + [task_id]
            rotations = [
                tuple(cycle[index:-1] + cycle[:index] + [cycle[index]])
                for index in range(len(cycle) - 1)
            ]
            canonical = min(rotations)
            if canonical not in reported_cycles:
                reported_cycles.add(canonical)
                errors.append(f"spec.tasks: dependency cycle {' -> '.join(canonical)}")
            return
        state[task_id] = 1
        trail.append(task_id)
        for dependency in sorted(dependencies.get(task_id, [])):
            if dependency in dependencies:
                visit(dependency, trail)
        trail.pop()
        state[task_id] = 2

    for task_id in sorted(dependencies):
        visit(task_id, [])


def _collaboration(
    value: Any, members_by_ref: dict[str, str], errors: list[str]
) -> None:
    path = "spec.collaboration"
    collaboration = _mapping(value, path, errors)
    if collaboration is None:
        return
    _keys(
        collaboration,
        path,
        {"manager", "reporting"},
        {"manager", "advisor", "reporting"},
        errors,
    )
    manager = collaboration.get("manager")
    if "manager" in collaboration:
        _string(manager, f"{path}.manager", errors)
    if _non_empty_string(manager) and manager not in members_by_ref:
        errors.append(f"{path}.manager: unknown agent_ref {manager!r}")
    elif _non_empty_string(manager):
        role_ref = members_by_ref[manager]
        if ROLE_REF_PATTERN.fullmatch(role_ref) and not role_ref.startswith("manager@"):
            errors.append(
                f"{path}.manager: member {manager!r} must use a manager@<version> role_ref"
            )
    advisor = collaboration.get("advisor")
    if "advisor" in collaboration:
        _string(advisor, f"{path}.advisor", errors)
    if _non_empty_string(advisor) and advisor not in members_by_ref:
        errors.append(f"{path}.advisor: unknown agent_ref {advisor!r}")
    elif _non_empty_string(advisor):
        role_ref = members_by_ref[advisor]
        if ROLE_REF_PATTERN.fullmatch(role_ref) and not role_ref.startswith("advisor@"):
            errors.append(
                f"{path}.advisor: member {advisor!r} must use an advisor@<version> role_ref"
            )

    if "reporting" not in collaboration:
        return
    reporting_path = f"{path}.reporting"
    reporting = _mapping(collaboration["reporting"], reporting_path, errors)
    if reporting is None:
        return
    _keys(
        reporting,
        reporting_path,
        {"strategy", "include_task_updates"},
        {"strategy", "include_task_updates"},
        errors,
    )
    if reporting.get("strategy") not in {"manager", "direct"}:
        errors.append(f"{reporting_path}.strategy: must be 'manager' or 'direct'")
    if "include_task_updates" in reporting and not isinstance(
        reporting["include_task_updates"], bool
    ):
        errors.append(f"{reporting_path}.include_task_updates: must be a boolean")


def _runtime(value: Any, errors: list[str]) -> None:
    path = "spec.runtime"
    runtime = _mapping(value, path, errors)
    if runtime is None:
        return
    _keys(runtime, path, {"provider"}, {"provider"}, errors)
    if runtime.get("provider") != "herdr":
        errors.append(f"{path}.provider: must be 'herdr'")


def _view(value: Any, errors: list[str]) -> None:
    path = "spec.view"
    view = _mapping(value, path, errors)
    if view is None:
        return
    _keys(view, path, {"profile"}, {"profile"}, errors)
    if view.get("profile") != "command-deck":
        errors.append(f"{path}.profile: must be 'command-deck'")


def validate_document(document: Any) -> list[str]:
    """Return deterministic diagnostics; an empty list means valid."""
    errors: list[str] = []
    root = _mapping(document, "$", errors)
    if root is None:
        return errors
    _keys(
        root,
        "$",
        {"apiVersion", "kind", "metadata", "spec"},
        {"apiVersion", "kind", "metadata", "spec"},
        errors,
    )
    if root.get("apiVersion") != "fleet.harness/v1":
        errors.append("$.apiVersion: must be 'fleet.harness/v1'")
    if root.get("kind") != "Fleet":
        errors.append("$.kind: must be 'Fleet'")

    metadata = _mapping(root.get("metadata"), "metadata", errors)
    if metadata is not None:
        _keys(metadata, "metadata", {"id"}, {"id"}, errors)
        if "id" in metadata:
            _string(metadata["id"], "metadata.id", errors)

    fleet_spec = _mapping(root.get("spec"), "spec", errors)
    if fleet_spec is None:
        return errors
    _keys(
        fleet_spec,
        "spec",
        {"objective", "members", "tasks", "collaboration", "runtime", "view"},
        {"objective", "members", "tasks", "collaboration", "runtime", "view"},
        errors,
    )
    if "objective" in fleet_spec:
        _string(fleet_spec["objective"], "spec.objective", errors)
    members_by_ref = _members(fleet_spec.get("members"), errors)
    _tasks(fleet_spec.get("tasks"), members_by_ref, errors)
    _collaboration(fleet_spec.get("collaboration"), members_by_ref, errors)
    _runtime(fleet_spec.get("runtime"), errors)
    _view(fleet_spec.get("view"), errors)
    return errors


def normalize_document(document: dict[str, Any]) -> dict[str, Any]:
    """Copy the validated API envelope into its JSON-safe normalized form."""
    return json.loads(json.dumps(document, ensure_ascii=False))


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[2] != "--output-json":
        print(f"usage: {Path(argv[0]).name} FLEET_YML --output-json", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        document = load_yaml(path)
    except (FleetLoadError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    errors = validate_document(document)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    normalized = normalize_document(document)
    print(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
