#!/usr/bin/env python3
"""Validate Fleet YAML and emit the normalized subprocess contract as JSON."""

from __future__ import annotations

import argparse
import hashlib
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
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*@[1-9][0-9]*$"
)
VIEW_PROFILE_REF_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[a-z][a-z0-9]*(?:-[a-z0-9]+)*)?@[1-9][0-9]*$"
)
IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


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


def _identifier(value: Any, path: str, errors: list[str]) -> None:
    _string(value, path, errors)
    if _non_empty_string(value) and IDENTIFIER_PATTERN.fullmatch(value) is None:
        errors.append(
            f"{path}: must be a safe identifier containing lowercase letters, "
            "digits, and single hyphens"
        )


def _string_list(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{path}: must be a non-empty list")
        return
    for index, item in enumerate(value):
        _string(item, f"{path}[{index}]", errors)


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
            {"agent_ref", "role_ref", "runtime"},
            {"agent_ref", "role_ref", "runtime"},
            errors,
        )
        for field in ("agent_ref", "role_ref"):
            if field in member:
                _string(member[field], f"{member_path}.{field}", errors)
        if "agent_ref" in member:
            _identifier(member["agent_ref"], f"{member_path}.agent_ref", errors)
        role_ref = member.get("role_ref")
        if _non_empty_string(role_ref) and ROLE_REF_PATTERN.fullmatch(role_ref) is None:
            errors.append(
                f"{member_path}.role_ref: must match "
                "'<role-id>@<positive-version>'"
            )
        if "runtime" in member:
            runtime_path = f"{member_path}.runtime"
            runtime = _mapping(member["runtime"], runtime_path, errors)
            if runtime is not None:
                _keys(
                    runtime,
                    runtime_path,
                    {"product", "model", "effort", "fallback"},
                    {"product", "model", "effort", "fallback"},
                    errors,
                )
                for field in ("product", "model", "effort", "fallback"):
                    if field in runtime:
                        _string(runtime[field], f"{runtime_path}.{field}", errors)
                product = runtime.get("product")
                if _non_empty_string(product) and product not in {"codex", "claude"}:
                    errors.append(
                        f"{runtime_path}.product: must be 'codex' or 'claude'"
                    )
                effort = runtime.get("effort")
                if _non_empty_string(effort) and effort not in {
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                }:
                    errors.append(
                        f"{runtime_path}.effort: must be low, medium, high, xhigh, or max"
                    )
                fallback = runtime.get("fallback")
                if _non_empty_string(fallback) and fallback not in {
                    "fail",
                    "product-default",
                }:
                    errors.append(
                        f"{runtime_path}.fallback: must be 'fail' or 'product-default'"
                    )
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
            {
                "id",
                "assignee",
                "depends_on",
                "instructions",
                "expected_output",
                "completion_criteria",
            },
            {
                "id",
                "assignee",
                "depends_on",
                "instructions",
                "expected_output",
                "completion_criteria",
            },
            errors,
        )
        for field in ("id", "assignee", "instructions", "expected_output"):
            if field in task:
                _string(task[field], f"{task_path}.{field}", errors)
        if "id" in task:
            _identifier(task["id"], f"{task_path}.id", errors)
        if "completion_criteria" in task:
            _string_list(
                task["completion_criteria"],
                f"{task_path}.completion_criteria",
                errors,
            )

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
    advisor = collaboration.get("advisor")
    if "advisor" in collaboration:
        _string(advisor, f"{path}.advisor", errors)
    if _non_empty_string(advisor) and advisor not in members_by_ref:
        errors.append(f"{path}.advisor: unknown agent_ref {advisor!r}")

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
    if reporting.get("strategy") != "manager":
        errors.append(f"{reporting_path}.strategy: must be 'manager'")
    if "include_task_updates" in reporting and not isinstance(
        reporting["include_task_updates"], bool
    ):
        errors.append(f"{reporting_path}.include_task_updates: must be a boolean")


def _legacy_runtime(value: Any, errors: list[str]) -> None:
    path = "spec.runtime"
    runtime = _mapping(value, path, errors)
    if runtime is None:
        return
    _keys(runtime, path, {"provider"}, {"provider", "codex_hook_trust"}, errors)
    if "provider" in runtime:
        _string(runtime.get("provider"), f"{path}.provider", errors)
    hook_trust = runtime.get("codex_hook_trust")
    if "codex_hook_trust" in runtime and hook_trust not in {
        "preapproved",
        "review",
    }:
        errors.append(
            f"{path}.codex_hook_trust: must be 'preapproved' or 'review'"
        )


def _legacy_view(value: Any, errors: list[str]) -> None:
    path = "spec.view"
    view = _mapping(value, path, errors)
    if view is None:
        return
    _keys(view, path, {"profile_ref"}, {"profile_ref"}, errors)
    profile_ref = view.get("profile_ref")
    if "profile_ref" in view:
        _string(profile_ref, f"{path}.profile_ref", errors)
    if _non_empty_string(profile_ref) and not VIEW_PROFILE_REF_PATTERN.fullmatch(
        profile_ref
    ):
        errors.append(
            f"{path}.profile_ref: must match '<namespace/>name@<positive-version>'"
        )


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
    api_version = root.get("apiVersion")
    if api_version not in {"fleet.harness/v1", "fleet.harness/v2"}:
        errors.append("$.apiVersion: must be 'fleet.harness/v1' or 'fleet.harness/v2'")
    if root.get("kind") != "Fleet":
        errors.append("$.kind: must be 'Fleet'")

    metadata = _mapping(root.get("metadata"), "metadata", errors)
    if metadata is not None:
        _keys(metadata, "metadata", {"id"}, {"id"}, errors)
        if "id" in metadata:
            _identifier(metadata["id"], "metadata.id", errors)

    fleet_spec = _mapping(root.get("spec"), "spec", errors)
    if fleet_spec is None:
        return errors
    adapter_fields = {"runtime", "view"} if api_version == "fleet.harness/v1" else set()
    _keys(
        fleet_spec,
        "spec",
        {
            "objective",
            "completion_criteria",
            "stop_conditions",
            "members",
            "tasks",
            "collaboration",
        },
        {
            "objective",
            "completion_criteria",
            "stop_conditions",
            "members",
            "tasks",
            "collaboration",
            *adapter_fields,
        },
        errors,
    )
    if "objective" in fleet_spec:
        _string(fleet_spec["objective"], "spec.objective", errors)
    if "completion_criteria" in fleet_spec:
        _string_list(
            fleet_spec["completion_criteria"], "spec.completion_criteria", errors
        )
    if "stop_conditions" in fleet_spec:
        _string_list(fleet_spec["stop_conditions"], "spec.stop_conditions", errors)
    members_by_ref = _members(fleet_spec.get("members"), errors)
    _tasks(fleet_spec.get("tasks"), members_by_ref, errors)
    _collaboration(fleet_spec.get("collaboration"), members_by_ref, errors)
    if api_version == "fleet.harness/v1":
        if "runtime" in fleet_spec:
            _legacy_runtime(fleet_spec.get("runtime"), errors)
        if "view" in fleet_spec:
            _legacy_view(fleet_spec.get("view"), errors)
    return errors


def normalize_document(document: dict[str, Any]) -> dict[str, Any]:
    """Copy the validated API envelope into its JSON-safe normalized form."""
    return json.loads(json.dumps(document, ensure_ascii=False))


ROLE_DEFINITION_FIELDS = {
    "id",
    "version",
    "produces",
    "mission",
    "responsibilities",
    "forbidden",
    "authority",
    "receives",
    "sends",
}


def resolve_role_definitions(
    document: dict[str, Any], catalog: Any
) -> tuple[dict[str, Any], list[str]]:
    """Resolve versioned role references from the public Role Catalog contract."""
    normalized = normalize_document(document)
    errors: list[str] = []
    if not isinstance(catalog, dict):
        return normalized, ["role catalog: must be a mapping"]
    if catalog.get("apiVersion") != "roles.harness/v1" or catalog.get("kind") != "RoleCatalog":
        return normalized, ["role catalog: unsupported apiVersion or kind"]
    metadata = catalog.get("metadata")
    spec = catalog.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        return normalized, ["role catalog: metadata and spec must be mappings"]
    name = metadata.get("name")
    version = metadata.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, int) or version < 1:
        return normalized, ["role catalog: metadata name/version is invalid"]
    catalog_ref = f"{name}@{version}"
    roles = spec.get("roles")
    if not isinstance(roles, list):
        return normalized, ["role catalog: spec.roles must be a list"]

    by_ref: dict[str, dict[str, Any]] = {}
    for index, raw_role in enumerate(roles):
        if not isinstance(raw_role, dict):
            errors.append(f"role catalog spec.roles[{index}]: must be a mapping")
            continue
        missing = sorted(ROLE_DEFINITION_FIELDS - raw_role.keys())
        if missing:
            errors.append(
                f"role catalog spec.roles[{index}]: missing keys: {', '.join(missing)}"
            )
            continue
        role_id = raw_role.get("id")
        role_version = raw_role.get("version")
        if (
            not isinstance(role_id, str)
            or IDENTIFIER_PATTERN.fullmatch(role_id) is None
            or not isinstance(role_version, int)
            or role_version < 1
        ):
            errors.append(f"role catalog spec.roles[{index}]: invalid id/version")
            continue
        role_ref = f"{role_id}@{role_version}"
        if role_ref in by_ref:
            errors.append(f"role catalog: duplicate role reference {role_ref!r}")
            continue
        by_ref[role_ref] = {
            field: json.loads(json.dumps(raw_role[field], ensure_ascii=False))
            for field in sorted(ROLE_DEFINITION_FIELDS)
        }

    fleet_spec = normalized.get("spec")
    if not isinstance(fleet_spec, dict):
        return normalized, errors
    members = fleet_spec.get("members")
    if not isinstance(members, list):
        return normalized, errors
    member_roles: dict[str, dict[str, Any]] = {}
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            continue
        role_ref = member.get("role_ref")
        role_definition = by_ref.get(role_ref) if isinstance(role_ref, str) else None
        if role_definition is None:
            errors.append(
                f"spec.members[{index}].role_ref: {role_ref!r} does not exist "
                f"in Role Catalog {catalog_ref}"
            )
            continue
        member["role_definition"] = role_definition
        agent_ref = member.get("agent_ref")
        if isinstance(agent_ref, str):
            member_roles[agent_ref] = role_definition

    collaboration = fleet_spec.get("collaboration")
    if isinstance(collaboration, dict):
        manager_ref = collaboration.get("manager")
        manager_role = member_roles.get(manager_ref) if isinstance(manager_ref, str) else None
        if manager_role is not None:
            authority = manager_role.get("authority")
            missing_authority = sorted(
                {"assign", "accept"}
                - (set(authority) if isinstance(authority, list) else set())
            )
            if missing_authority:
                errors.append(
                    f"spec.collaboration.manager: member {manager_ref!r} role "
                    f"must grant authority: {', '.join(missing_authority)}"
                )

    canonical = json.dumps(
        catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    normalized["resolved_role_catalog"] = {
        "apiVersion": "roles.harness/v1",
        "ref": catalog_ref,
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }
    return normalized, errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=Path(argv[0]).name)
    parser.add_argument("fleet", type=Path)
    parser.add_argument("--role-catalog", type=Path, required=True)
    parser.add_argument("--output-json", action="store_true", required=True)
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2
    path = args.fleet
    try:
        document = load_yaml(path)
        catalog = load_yaml(args.role_catalog)
    except (FleetLoadError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    errors = validate_document(document)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    normalized, role_errors = resolve_role_definitions(document, catalog)
    if role_errors:
        for error in role_errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
