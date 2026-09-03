#!/usr/bin/env python3
"""Validate catalog, manifest, source, and license distribution invariants."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def fail(message: str) -> None:
    raise ValueError(message)


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        fail(f"JSON objectではありません: {path}")
    return value


def catalog_entries(path: Path, runtime: str) -> tuple[str, dict[str, tuple[str, str]]]:
    catalog = load_json(path)
    marketplace = catalog.get("name")
    if not isinstance(marketplace, str) or not marketplace:
        fail(f"marketplace名が不正です: {path}")
    entries: dict[str, tuple[str, str]] = {}
    for entry in catalog.get("plugins", []):
        if not isinstance(entry, dict):
            fail(f"plugin entryが不正です: {path}")
        name = entry.get("name")
        version = entry.get("version")
        source = entry.get("source")
        if runtime == "codex":
            if not isinstance(source, dict) or source.get("source") != "local":
                fail(f"Codex sourceがlocal形式ではありません: {name}")
            source = source.get("path")
        if not all(isinstance(value, str) and value for value in (name, version, source)):
            fail(f"catalog entryの識別情報が不正です: {path}")
        if name in entries:
            fail(f"plugin名が重複しています: {name}")
        entries[name] = (version, source)
    return marketplace, entries


def require_regular_file(path: Path) -> None:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink():
        fail(f"regular fileではありません: {path}")


def reject_symlinks(source_root: Path) -> None:
    if source_root.is_symlink():
        fail(f"plugin source subtreeにsymlinkがあります: {source_root}")
    for directory, subdirectories, filenames in os.walk(source_root, followlinks=False):
        parent = Path(directory)
        for name in [*subdirectories, *filenames]:
            path = parent / name
            if path.is_symlink():
                fail(f"plugin source subtreeにsymlinkがあります: {path}")


def resolve_declared_path(source_root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        fail(f"{label}が未宣言です: {source_root}")
    declared = source_root / value
    resolved = declared.resolve(strict=False)
    if resolved == source_root or source_root not in resolved.parents:
        fail(f"{label}がplugin root外を指しています: {source_root}")
    if declared.is_symlink():
        fail(f"{label}がsymlinkです: {source_root}")
    return resolved


def codex_capabilities(manifest: dict) -> list[str]:
    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        fail("Codex manifestのinterfaceがobjectではありません")
    capabilities = interface.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(value, str) for value in capabilities):
        fail("Codex manifestのinterface.capabilitiesがstring配列ではありません")
    return capabilities


def validate_skills(source_root: Path, manifests: dict[str, dict]) -> None:
    bundled_skills = source_root / "skills"
    capabilities = codex_capabilities(manifests["codex"])
    skills_declared = any("skills" in manifest for manifest in manifests.values())
    skills_capability = "Skills" in capabilities
    skills_expected = bundled_skills.exists() or skills_declared or skills_capability
    if not skills_expected:
        return
    if not bundled_skills.is_dir():
        fail(f"physical skills directoryがありません: {source_root}")
    for runtime, manifest in manifests.items():
        skills_root = resolve_declared_path(source_root, manifest.get("skills"), f"{runtime} skills path")
        if not skills_root.is_dir():
            fail(f"{runtime} skills pathが実在directoryではありません: {source_root}")
        skill_files = [
            path
            for path in skills_root.rglob("SKILL.md")
            if path.is_file() and not path.is_symlink() and skills_root in path.resolve().parents
        ]
        if not skill_files:
            fail(f"{runtime} skills pathにSKILL.mdがありません: {source_root}")
        for skill_file in skill_files:
            if not skill_file.read_text(encoding="utf-8").strip():
                fail(f"SKILL.mdが空です: {skill_file}")
    if not skills_capability:
        fail(f"Codex interface.capabilitiesにSkillsがありません: {source_root}")


def validate_hook_document(path: Path, plugin_name: str) -> None:
    document = load_json(path)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        fail(f"hooks fileにhook定義がありません: {plugin_name}")
    if plugin_name == "agent-fleet-session-hooks":
        required_events = {"UserPromptSubmit", "SessionStart"}
        if not required_events.issubset(hooks):
            fail(f"session-hooksに必須eventがありません: {plugin_name}")
    for event, groups in hooks.items():
        if not isinstance(groups, list) or not groups:
            fail(f"hooks eventが空です: {plugin_name}/{event}")
        for group in groups:
            commands = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(commands, list) or not commands:
                fail(f"hooks command groupが空です: {plugin_name}/{event}")
            if not all(
                isinstance(command, dict)
                and command.get("type") == "command"
                and isinstance(command.get("command"), str)
                and bool(command["command"])
                for command in commands
            ):
                fail(f"hooks command契約が不正です: {plugin_name}/{event}")


def validate_hooks(source_root: Path, plugin_name: str, manifests: dict[str, dict]) -> None:
    capabilities = codex_capabilities(manifests["codex"])
    hooks_capability = "Hooks" in capabilities
    hooks_declared = any("hooks" in manifest for manifest in manifests.values())
    if hooks_declared != hooks_capability:
        fail(f"hooks宣言とHooks capabilityが一致しません: {plugin_name}")
    if plugin_name == "agent-fleet-session-hooks" and not hooks_capability:
        fail(f"session-hooksにHooks capabilityがありません: {plugin_name}")
    if not hooks_capability:
        return
    for runtime, manifest in manifests.items():
        hooks_path = resolve_declared_path(source_root, manifest.get("hooks"), f"{runtime} hooks path")
        require_regular_file(hooks_path)
        if not hooks_path.read_bytes():
            fail(f"hooks fileが空です: {plugin_name}/{runtime}")
        validate_hook_document(hooks_path, plugin_name)


def validate_repository(root: Path) -> int:
    root_license = root / "LICENSE"
    require_regular_file(root_license)
    license_bytes = root_license.read_bytes()

    claude_name, claude = catalog_entries(root / ".claude-plugin/marketplace.json", "claude")
    codex_name, codex = catalog_entries(root / ".agents/plugins/marketplace.json", "codex")
    if claude_name != codex_name:
        fail("Claude/Codex marketplace名が一致しません")
    if claude != codex:
        fail("Claude/Codexのplugin名・version・sourceが一致しません")

    expected_roots: set[Path] = set()
    plugins_root = (root / "plugins").resolve(strict=True)
    for name, (version, source) in codex.items():
        source_declared = root / source
        if source_declared.is_symlink():
            fail(f"plugin source pathがsymlinkです: {name}")
        source_root = source_declared.resolve(strict=True)
        if source_root == plugins_root or plugins_root not in source_root.parents:
            fail(f"plugin sourceがplugins配下ではありません: {name}")
        reject_symlinks(source_declared)
        expected_roots.add(source_root)
        source_license = source_root / "LICENSE"
        require_regular_file(source_license)
        if source_license.read_bytes() != license_bytes:
            fail(f"source LICENSEがrepository rootと一致しません: {name}")
        manifests: dict[str, dict] = {}
        for runtime in ("claude", "codex"):
            manifest = source_root / f".{runtime}-plugin/plugin.json"
            require_regular_file(manifest)
            identity = load_json(manifest)
            if identity.get("name") != name or identity.get("version") != version:
                fail(f"{runtime} manifestのname/versionがcatalogと一致しません: {name}")
            manifests[runtime] = identity
        validate_skills(source_root, manifests)
        validate_hooks(source_root, name, manifests)

    discovered: set[Path] = set()
    for manifest in plugins_root.rglob("plugin.json"):
        if manifest.parent.name not in {".claude-plugin", ".codex-plugin"}:
            continue
        discovered.add(manifest.parent.parent.resolve(strict=True))
    if discovered != expected_roots:
        missing = sorted(os.fspath(path.relative_to(root)) for path in expected_roots - discovered)
        extra = sorted(os.fspath(path.relative_to(root)) for path in discovered - expected_roots)
        fail(f"catalogとsource集合が一致しません: missing={missing}, extra={extra}")

    print(f"Distribution: passed ({len(expected_roots)} plugins)")
    return 0


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def expect_mutation_rejected(root: Path, mutation: str, expected_error: str) -> None:
    with tempfile.TemporaryDirectory(prefix="distribution-negative-") as temporary:
        fixture = Path(temporary) / "repository"
        shutil.copytree(root, fixture, ignore=shutil.ignore_patterns(".git"), symlinks=True)
        _, entries = catalog_entries(fixture / ".agents/plugins/marketplace.json", "codex")
        skills_name = next(
            name for name, (_, source) in entries.items() if (fixture / source / "skills").is_dir()
        )
        skills_root = (fixture / entries[skills_name][1]).resolve(strict=True)
        if mutation == "skills-deleted":
            manifest_path = skills_root / ".claude-plugin/plugin.json"
            manifest = load_json(manifest_path)
            manifest.pop("skills", None)
            write_json(manifest_path, manifest)
        elif mutation == "physical-skills-deleted":
            shutil.rmtree(skills_root / "skills")
        elif mutation in {"skill-zero-byte", "skill-whitespace"}:
            skill_file = next((skills_root / "skills").rglob("SKILL.md"))
            skill_file.write_text("" if mutation == "skill-zero-byte" else " \n\t\n", encoding="utf-8")
        elif mutation == "skills-intermediate-symlink":
            (skills_root / "skill-alias").symlink_to(".", target_is_directory=True)
            for runtime in ("claude", "codex"):
                manifest_path = skills_root / f".{runtime}-plugin/plugin.json"
                manifest = load_json(manifest_path)
                manifest["skills"] = "./skill-alias/skills"
                write_json(manifest_path, manifest)
        elif mutation == "capabilities-empty":
            manifest_path = skills_root / ".codex-plugin/plugin.json"
            manifest = load_json(manifest_path)
            manifest.setdefault("interface", {})["capabilities"] = []
            write_json(manifest_path, manifest)
        elif mutation == "hooks-invalid":
            hook_name = next(
                (
                    name
                    for name, (_, source) in entries.items()
                    if "Hooks"
                    in load_json((fixture / source / ".codex-plugin/plugin.json")).get("interface", {}).get(
                        "capabilities", []
                    )
                ),
                skills_name,
            )
            hook_root = (fixture / entries[hook_name][1]).resolve(strict=True)
            for runtime in ("claude", "codex"):
                manifest_path = hook_root / f".{runtime}-plugin/plugin.json"
                manifest = load_json(manifest_path)
                manifest["hooks"] = "../outside-hooks.json"
                if runtime == "codex":
                    capabilities = manifest.setdefault("interface", {}).setdefault("capabilities", [])
                    if "Hooks" not in capabilities:
                        capabilities.append("Hooks")
                write_json(manifest_path, manifest)
        elif mutation == "hooks-intermediate-symlink":
            hook_name = next(
                (
                    name
                    for name, (_, source) in entries.items()
                    if "Hooks"
                    in load_json((fixture / source / ".codex-plugin/plugin.json")).get("interface", {}).get(
                        "capabilities", []
                    )
                ),
                skills_name,
            )
            hook_root = (fixture / entries[hook_name][1]).resolve(strict=True)
            (hook_root / "hook-alias").symlink_to(".", target_is_directory=True)
            has_hooks = "Hooks" in load_json(hook_root / ".codex-plugin/plugin.json").get("interface", {}).get(
                "capabilities", []
            )
            if not has_hooks:
                write_json(
                    hook_root / "fixture-hooks.json",
                    {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "true"}]}]}},
                )
            for runtime in ("claude", "codex"):
                manifest_path = hook_root / f".{runtime}-plugin/plugin.json"
                manifest = load_json(manifest_path)
                if has_hooks:
                    original = str(manifest["hooks"]).removeprefix("./")
                    manifest["hooks"] = f"./hook-alias/{original}"
                else:
                    manifest["hooks"] = "./hook-alias/fixture-hooks.json"
                    if runtime == "codex":
                        manifest.setdefault("interface", {}).setdefault("capabilities", []).append("Hooks")
                write_json(manifest_path, manifest)
        else:
            fail(f"未知のmutationです: {mutation}")

        result = subprocess.run(
            [sys.executable, os.fspath(fixture / "scripts/validate-distribution.py"), os.fspath(fixture)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or expected_error not in result.stderr:
            fail(
                f"改変負例を期待した理由で拒否できません: mutation={mutation}, "
                f"exit={result.returncode}, stderr={result.stderr.strip()}"
            )


def self_test(root: Path) -> int:
    expect_mutation_rejected(root, "skills-deleted", "skills path")
    expect_mutation_rejected(root, "physical-skills-deleted", "skills")
    expect_mutation_rejected(root, "skill-zero-byte", "SKILL.md")
    expect_mutation_rejected(root, "skill-whitespace", "SKILL.md")
    expect_mutation_rejected(root, "skills-intermediate-symlink", "symlink")
    expect_mutation_rejected(root, "capabilities-empty", "interface.capabilitiesにSkills")
    expect_mutation_rejected(root, "hooks-invalid", "hooks path")
    expect_mutation_rejected(root, "hooks-intermediate-symlink", "symlink")
    print("Distribution negative tests: passed (8 mutations)")
    return 0


def main() -> int:
    if len(sys.argv) == 2:
        return validate_repository(Path(sys.argv[1]).resolve(strict=True))
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        return self_test(Path(sys.argv[2]).resolve(strict=True))
    print(f"usage: {Path(sys.argv[0]).name} [--self-test] REPOSITORY_ROOT", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Distribution: failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
