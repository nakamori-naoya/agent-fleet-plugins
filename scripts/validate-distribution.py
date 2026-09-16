#!/usr/bin/env python3
"""明示された plugin repository に package 契約を fail-closed で適用する。

判定するのは決定論的な述語だけである（`.agents/rules/plugin-package-contract.md` の
「plugin package 構造検査の宣言」と `.agents/rules/deterministic-validation.md` の「禁止参照形の検査宣言」）。

  1. 両 marketplace の package identity（名前・version・source）が一致し、source は ./plugins/<package> の2階層。
     plugins/ 直下に manifest が無い
  2. Codex / Claude の manifest は名前・version・skills・metadata.harness が同一。metadata.harness は
     marketplace（両 marketplace name と一致）と contractVersion（整数）を持ち、installationSurface / entryRoot を持たない
  3. skills は ./skills/<entry> の list。<entry>/SKILL.md の frontmatter name が <entry>。skills/ 直下の directory 集合と一致
  4. playbooks は CONTRACT.md を持つ入口だけ。implements は playbooks の各 key につき1件（kind: playbook、id: <marketplace>/<key>）
  5. internalPlugins は ./internal/<name>。internal/ 直下の directory 集合と一致。hook sidecar 以外は SKILL.md（name = <name>）を持ち、
     hook sidecar は SKILL.md を持たない。内部 skill は CONTRACT.md を持たない
  6. package 配下の SKILL.md は skills/<entry>/ と internal/<name>/ にだけあり、.claude-plugin / .codex-plugin は
     package root と hook sidecar root にだけある
  7. 隣接 playbook.yml は version 2、name = directory 名、requires は外部 package だけ、steps の参照が宣言と実体へ到達する
  8. 公開入口と内部 skill の SKILL.md / references/**/*.md / CONTRACT.md / playbook.yml 文字列値に禁止参照形が無い
  9. package 内に symlink が無い

使い方: validate-plugin-repository.py [--self-test] <repository の絶対パス>
       validate-plugin-repository.py --self-test            （合成 fixture だけを検査する）
違反があれば理由を出力して終了コード 1。

各 plugin repository には同じ内容が `scripts/validate-distribution.py` として配られる（正本は
`product-planning-plugins/shared/runtime-source/validate-distribution.py`、root の本 file と byte 一致）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

FORBIDDEN_TOKENS = ("${.", "<!-- BEGIN shared:", "CLAUDE_PLUGIN_ROOT", "BUNDLE_ROOT")


class ContractError(ValueError):
    pass


def fail(message: str) -> None:
    raise ContractError(message)


def load_object(path: Path, label: str) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"{label} が regular file ではない: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"{label} が有効な JSON ではない: {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} は JSON object でなければならない: {path}")
    return value


def safe_relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.startswith("./"):
        fail(f"{label} は ./ から始まる package 内 path でなければならない: {value!r}")
    relative = Path(value[2:])
    if not relative.parts or ".." in relative.parts or relative.is_absolute():
        fail(f"{label} が package 境界を越える: {value!r}")
    return relative


def catalog(repository: Path, runtime: str) -> tuple[str, list[tuple[str, str, str]]]:
    relative = ".agents/plugins/marketplace.json" if runtime == "codex" else ".claude-plugin/marketplace.json"
    data = load_object(repository / relative, f"{runtime} marketplace")
    marketplace = data.get("name")
    if not isinstance(marketplace, str) or not marketplace:
        fail(f"{runtime} marketplace の name が空でない文字列ではない")
    plugins = data.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        fail(f"{runtime} marketplace に公開 package が無い")
    entries: list[tuple[str, str, str]] = []
    for index, item in enumerate(plugins):
        if not isinstance(item, dict):
            fail(f"{runtime} marketplace plugins[{index}] が object ではない")
        source = item.get("source")
        if runtime == "codex":
            if not isinstance(source, dict) or source.get("source") != "local":
                fail(f"Codex marketplace plugins[{index}] の source が local object ではない")
            source = source.get("path")
        relative = safe_relative(source, f"{runtime} marketplace source")
        if len(relative.parts) != 2 or relative.parts[0] != "plugins":
            fail(f"{runtime} marketplace source は ./plugins/<package> の2階層でなければならない: {source}")
        name, version = item.get("name"), item.get("version")
        if not all(isinstance(v, str) and v for v in (name, version, source)):
            fail(f"{runtime} marketplace plugins[{index}] の identity が不正")
        entries.append((name, version, source))
    if len(entries) != len(set(entries)):
        fail(f"{runtime} marketplace に公開 package の重複がある")
    return marketplace, entries


def parse_yaml(text: str, label: str) -> Any:
    """Parse one YAML document through the explicit mikefarah/yq v4 dependency."""
    yq = shutil.which("yq")
    if yq is None:
        fail(f"{label} の検査には mikefarah/yq v4 が必要")
    try:
        result = subprocess.run([yq, "-o=json", "."], input=text, text=True, capture_output=True, check=False)
    except OSError as exc:
        fail(f"{label} の YAML parser を実行できない: {exc}")
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown parse error"
        fail(f"{label} が有効な YAML ではない: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"{label} の YAML parser 出力が JSON ではない: {exc}")


def load_yaml(path: Path, label: str) -> Any:
    return parse_yaml(path.read_text(encoding="utf-8"), f"{label}: {path}")


def skill_name(entry: Path) -> str:
    if entry.is_symlink() or not entry.is_file():
        fail(f"SKILL.md が regular file ではない: {entry}")
    lines = entry.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        fail(f"SKILL.md に YAML frontmatter が無い: {entry}")
    try:
        closing = lines.index("---", 1)
    except ValueError:
        fail(f"SKILL.md の YAML frontmatter が閉じていない: {entry}")
    document = parse_yaml("\n".join(lines[1:closing]) + "\n", f"{entry} frontmatter")
    if not isinstance(document, dict):
        fail(f"SKILL.md frontmatter は mapping でなければならない: {entry}")
    name = document.get("name")
    if not isinstance(name, str) or not name.strip():
        fail(f"SKILL.md frontmatter の name は空でない文字列でなければならない: {entry}")
    return name


def mapping_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        fail(f"{label} は list でなければならない")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
            fail(f"{label}[{index}] は文字列keyの mapping でなければならない")
        result.append(item)
    return result


def string_set_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        fail(f"{label} は空でない文字列のlistでなければならない")
    if len(value) != len(set(value)):
        fail(f"{label} に重複がある")
    return value


def optional_string_set_list(mapping: dict[str, Any], key: str, label: str) -> list[str]:
    if key not in mapping:
        return []
    return string_set_list(mapping[key], label)


def path_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in value.items()):
        fail(f"{label} は空でない {{名前: path}} の mapping でなければならない")
    return value


# ---- 隣接 playbook.yml -----------------------------------------------------------------------

def validate_ordered_steps(document: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    inputs = set(optional_string_set_list(document, "inputs", f"{path} inputs"))
    steps = mapping_list(document.get("steps"), f"{path} steps")
    if not steps:
        fail(f"{path} steps は1工程以上でなければならない")
    ids: set[str] = set()
    provided_at: dict[str, list[int]] = {}
    for index, item in enumerate(steps):
        step_id = item.get("id")
        if not isinstance(step_id, str) or not step_id:
            fail(f"steps[{index}].id は空でない文字列でなければならない: {path}")
        if step_id in ids:
            fail(f"steps の id が重複している: {path}: {step_id}")
        ids.add(step_id)
        actions = [key for key in ("agent_work", "script", "skill", "playbook") if key in item]
        if len(actions) != 1:
            fail(f"steps[{index}] は agent_work/script/skill/playbook のどれか1つだけを宣言しなければならない: {path}")
        if "agent_work" in item and item["agent_work"] != "invoking_agent":
            fail(f"steps[{index}].agent_work は invoking_agent でなければならない: {path}")
        if item.get("agent_work") == "invoking_agent" and (
                not isinstance(item.get("purpose"), str) or not item["purpose"]):
            fail(f"steps[{index}].agent_work には空でないpurposeが必要: {path}")
        for value in optional_string_set_list(item, "provides", f"{path} steps[{index}].provides"):
            provided_at.setdefault(value, []).append(index)

    available = set(inputs)
    for index, item in enumerate(steps):
        needs = optional_string_set_list(item, "needs", f"{path} steps[{index}].needs")
        if "conditional_needs" in item:
            for condition_index, condition in enumerate(mapping_list(
                    item["conditional_needs"], f"{path} steps[{index}].conditional_needs")):
                when = condition.get("when")
                if not isinstance(when, str) or not when:
                    fail(f"steps[{index}].conditional_needs[{condition_index}].when は空でない文字列でなければならない: {path}")
                if "needs" not in condition:
                    fail(f"{path} steps[{index}].conditional_needs[{condition_index}].needs は空でない文字列のlistでなければならない")
                needs += string_set_list(
                    condition["needs"], f"{path} steps[{index}].conditional_needs[{condition_index}].needs")
        for need in needs:
            if need not in available:
                origin = "後方の未実行工程" if need in provided_at else "inputsまたは先行provides"
                fail(f"steps の needs が{origin}から到達できない: {path}: {item['id']} -> {need}")
        available.update(optional_string_set_list(item, "provides", f"{path} steps[{index}].provides"))
    return steps


def external_dependencies(document: dict[str, Any], path: Path, own_marketplace: str) -> set[str]:
    value = document.get("requires")
    if value is None:
        return set()
    external: set[str] = set()
    for item in mapping_list(value, f"{path} requires"):
        if set(item) != {"plugin", "marketplace"}:
            fail(f"外部依存は plugin と marketplace だけで宣言する: {path}")
        if not all(isinstance(item[key], str) and item[key] for key in ("plugin", "marketplace")):
            fail(f"外部依存の plugin と marketplace は空でない文字列でなければならない: {path}")
        if item["marketplace"] == own_marketplace:
            fail(f"requires は外部 package だけを宣言する（自 marketplace の要素がある）: {path}: {item['plugin']}")
        external.add(item["plugin"])
    return external


def validate_step_references(steps: list[dict[str, Any]], path: Path, callable_skills: set[str], external: set[str]) -> None:
    used_playbooks: set[str] = set()
    root = path.parent
    for index, item in enumerate(steps):
        if "script" in item:
            value = item["script"]
            if not isinstance(value, str) or not value or value.startswith("/"):
                fail(f"steps[{index}].script は入口からの相対path文字列でなければならない: {path}")
            relative = Path(value)
            if not relative.parts or relative.parts[0] != "scripts" or ".." in relative.parts:
                fail(f"steps[{index}].script は入口の scripts/ 配下でなければならない: {path}: {value}")
            target = root / relative
            if target.is_symlink() or not target.is_file():
                fail(f"steps の script が入口内のregular fileとして実在しない: {path}: {value}")
        if "skill" in item:
            skill = item["skill"]
            if isinstance(skill, str) and skill in external:
                fail(f"外部 package は steps の playbook: からだけ呼ぶ: {path}")
            if not isinstance(skill, str) or not skill or skill not in callable_skills:
                fail(f"steps の skill が宣言済みの実在skillに無い: {path}: {skill}")
        if "playbook" in item:
            playbook = item["playbook"]
            if not isinstance(playbook, str) or not playbook or playbook not in external:
                fail(f"steps の playbook が requires に宣言されていない: {path}: {playbook}")
            used_playbooks.add(playbook)
    missing = external - used_playbooks
    if missing:
        fail(f"requires の外部 package に対応する playbook step が無い: {path}: {sorted(missing)}")


def validate_playbook_yml(path: Path, expected_name: str, own_marketplace: str, callable_skills: set[str]) -> Any:
    if path.is_symlink() or not path.is_file():
        fail(f"playbook.yml がregular fileではない: {path}")
    document = load_yaml(path, "playbook.yml")
    if not isinstance(document, dict):
        fail(f"playbook.yml は top-level mapping でなければならない: {path}")
    if document.get("version") != 2:
        fail(f"playbook.yml は version 2 でなければならない: {path}")
    if document.get("name") != expected_name:
        fail(f"playbook.yml の name が directory 名と一致しない: {path}: {document.get('name')!r} != {expected_name!r}")
    steps = validate_ordered_steps(document, path)
    external = external_dependencies(document, path, own_marketplace)
    validate_step_references(steps, path, callable_skills, external)
    return document


# ---- 禁止参照形 ------------------------------------------------------------------------------

def scan_markdown(path: Path) -> None:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for token in FORBIDDEN_TOKENS:
            if token in line:
                fail(f"禁止された参照形がある: {path}:{number}: {token}")


def scan_yaml_strings(value: Any, path: Path, key_path: str) -> None:
    if isinstance(value, str):
        for token in FORBIDDEN_TOKENS:
            if token in value:
                fail(f"禁止された参照形がある: {path}:{key_path}: {token}")
    elif isinstance(value, dict):
        for key, item in value.items():
            scan_yaml_strings(item, path, f"{key_path}.{key}" if key_path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            scan_yaml_strings(item, path, f"{key_path}[{index}]")


def scan_skill_root(root: Path, playbook_document: Any) -> None:
    for name in ("SKILL.md", "CONTRACT.md"):
        if (root / name).is_file():
            scan_markdown(root / name)
    references = root / "references"
    if references.is_dir():
        for path in sorted(references.rglob("*.md")):
            if path.is_file():
                scan_markdown(path)
    if playbook_document is not None:
        scan_yaml_strings(playbook_document, root / "playbook.yml", "")


# ---- package -------------------------------------------------------------------------------

def subdirectories(path: Path) -> set[str]:
    if not path.is_dir():
        return set()
    return {child.name for child in path.iterdir() if child.is_dir()}


def validate_package(repository: Path, identity: tuple[str, str, str], marketplace: str) -> None:
    name, version, source = identity
    package = repository / safe_relative(source, f"{name} package source")
    if package.is_symlink() or not package.is_dir():
        fail(f"公開 package source が directory ではない: {package}")
    for path in package.rglob("*"):
        if path.is_symlink():
            fail(f"package 内に symlink がある: {path}")
    for runtime in ("codex", "claude"):
        if (repository / "plugins" / f".{runtime}-plugin").exists():
            fail(f"plugins/ 直下に manifest を置かない: {repository / 'plugins' / f'.{runtime}-plugin'}")

    manifests = [load_object(package / f".{rt}-plugin/plugin.json", f"{name} {rt} manifest") for rt in ("codex", "claude")]
    shared = [{k: m.get(k) for k in ("name", "version", "skills")} | {"harness": m.get("metadata", {}).get("harness")} for m in manifests]
    if shared[0] != shared[1]:
        fail(f"{name} の Codex / Claude manifest（名前・version・skills・metadata.harness）が一致しない")
    manifest = manifests[0]
    if manifest.get("name") != name or manifest.get("version") != version:
        fail(f"{name} manifest の identity が marketplace と一致しない")
    harness = manifest.get("metadata", {}).get("harness") if isinstance(manifest.get("metadata"), dict) else None
    if not isinstance(harness, dict):
        fail(f"{name} metadata.harness は object でなければならない")
    if harness.get("marketplace") != marketplace:
        fail(f"{name} metadata.harness.marketplace が両 marketplace の name と一致しない: {harness.get('marketplace')!r} != {marketplace!r}")
    contract_version = harness.get("contractVersion")
    if isinstance(contract_version, bool) or not isinstance(contract_version, int):
        fail(f"{name} metadata.harness.contractVersion は整数でなければならない")
    for key in ("installationSurface", "entryRoot"):
        if key in harness:
            fail(f"{name} metadata.harness に {key} を置かない")

    # 公開入口
    skills = manifest.get("skills")
    if not isinstance(skills, list) or not skills or not all(isinstance(item, str) and item for item in skills):
        fail(f"{name} manifest skills は ./skills/<entry> の空でない list でなければならない（文字列 \"./skills/\" は不可）")
    if len(skills) != len(set(skills)):
        fail(f"{name} manifest skills に重複がある")
    public_names: list[str] = []
    for value in skills:
        relative = safe_relative(value, f"{name} skills")
        if len(relative.parts) != 2 or relative.parts[0] != "skills":
            fail(f"公開入口は ./skills/<entry> でなければならない: {value}")
        entry = relative.parts[1]
        root = package / relative
        if root.is_symlink() or not root.is_dir():
            fail(f"manifest が宣言した公開入口 directory が無い: {root}")
        declared = skill_name(root / "SKILL.md")
        if declared != entry:
            fail(f"公開入口の SKILL.md name が directory 名と一致しない: {root / 'SKILL.md'}: {declared!r} != {entry!r}")
        public_names.append(entry)
    undeclared = subdirectories(package / "skills") - set(public_names)
    if undeclared:
        fail(f"{name} skills/ 直下に manifest が宣言しない directory がある: {sorted(undeclared)}")

    # 公開 playbook
    playbooks = harness.get("playbooks")
    playbook_names: set[str] = set()
    if playbooks is not None:
        playbooks = path_mapping(playbooks, f"{name} metadata.harness.playbooks")
        for key, value in playbooks.items():
            if key not in public_names:
                fail(f"playbooks の key が skills の要素名に無い: {name}: {key}")
            if value != f"./skills/{key}":
                fail(f"playbooks.{key} は ./skills/{key} でなければならない: {value}")
            for required in ("CONTRACT.md", "playbook.yml"):
                target = package / "skills" / key / required
                if target.is_symlink() or not target.is_file():
                    fail(f"公開 playbook に {required} が無い: {target}")
        playbook_names = set(playbooks)
    for entry in public_names:
        if entry not in playbook_names and (package / "skills" / entry / "CONTRACT.md").exists():
            fail(f"playbooks に無い入口が CONTRACT.md を持つ: {package / 'skills' / entry / 'CONTRACT.md'}")

    implements = harness.get("implements")
    if playbooks is None:
        if implements is not None:
            fail(f"{name} playbooks が無い package に implements を置かない")
    else:
        items = mapping_list(implements, f"{name} metadata.harness.implements")
        seen: set[str] = set()
        for index, item in enumerate(items):
            playbook = item.get("playbook")
            if item.get("kind") != "playbook" or playbook not in playbook_names:
                fail(f"implements[{index}] は playbooks の入口を kind: playbook で宣言しなければならない: {name}")
            if item.get("id") != f"{marketplace}/{playbook}":
                fail(f"implements[{index}].id は {marketplace}/{playbook} でなければならない: {item.get('id')!r}")
            if isinstance(item.get("version"), bool) or not isinstance(item.get("version"), int):
                fail(f"implements[{index}].version は整数でなければならない: {name}")
            if playbook in seen:
                fail(f"implements に同じ playbook が2件ある: {name}: {playbook}")
            seen.add(playbook)
        if seen != playbook_names:
            fail(f"implements は playbooks の各 key につき1件でなければならない: {name}: {sorted(playbook_names - seen)}")

    # 内部 skill
    internal = harness.get("internalPlugins")
    internal_callable: set[str] = set()
    sidecar_roots: set[Path] = set()
    internal_names: list[str] = []
    if internal is not None:
        internal = path_mapping(internal, f"{name} metadata.harness.internalPlugins")
        for internal_name, value in internal.items():
            if value != f"./internal/{internal_name}":
                fail(f"internalPlugins.{internal_name} は ./internal/{internal_name} でなければならない: {value}")
            if internal_name in public_names:
                fail(f"内部 skill の name が公開入口の名前と同じ: {name}: {internal_name}")
            root = package / "internal" / internal_name
            if root.is_symlink() or not root.is_dir():
                fail(f"内部 plugin root が無い: {root}")
            manifest_paths = [root / f".{rt}-plugin/plugin.json" for rt in ("codex", "claude")]
            if any(path.exists() for path in manifest_paths):
                if not all(path.exists() for path in manifest_paths):
                    fail(f"hook sidecar 以外の内部 skill に nested runtime manifest を置かない（両 runtime の hooks manifest が揃っていない）: {root}")
                nested = [load_object(path, f"{internal_name} {rt} manifest") for path, rt in zip(manifest_paths, ("codex", "claude"))]
                if not all(isinstance(item.get("hooks"), str) and item["hooks"] for item in nested):
                    fail(f"hook sidecar 以外の内部 skill に nested runtime manifest を置かない: {root}")
                identities = [(item.get("name"), item.get("version")) for item in nested]
                if identities[0] != identities[1] or identities[0][0] != internal_name:
                    fail(f"hook sidecar の runtime identity が一致しない: {root}")
                for runtime, item in zip(("codex", "claude"), nested):
                    hook = root / safe_relative(item["hooks"], f"{internal_name} {runtime} hooks")
                    if root not in hook.parents or hook.is_symlink() or not hook.is_file():
                        fail(f"hook sidecar の hook 入口が regular file として実在しない: {hook}")
                sidecar_roots.add(root)
                if (root / "SKILL.md").exists():
                    fail(f"hook sidecar は SKILL.md を持たない（判断責務を持つなら内部 skill にする）: {root / 'SKILL.md'}")
            else:
                declared = skill_name(root / "SKILL.md")
                if declared != internal_name:
                    fail(f"内部 skill の SKILL.md name が directory 名と一致しない: {root / 'SKILL.md'}: {declared!r} != {internal_name!r}")
                internal_callable.add(internal_name)
            if (root / "CONTRACT.md").exists():
                fail(f"内部 skill は CONTRACT.md を持たない（外部から呼べる契約は公開 playbook だけ）: {root / 'CONTRACT.md'}")
            internal_names.append(internal_name)
    undeclared = subdirectories(package / "internal") - set(internal_names)
    if undeclared:
        fail(f"{name} internal/ 直下に internalPlugins が宣言しない directory がある: {sorted(undeclared)}")

    # 配置: SKILL.md と nested manifest の置き場
    allowed_skill_files = {package / "skills" / entry / "SKILL.md" for entry in public_names} | {
        package / "internal" / internal_name / "SKILL.md" for internal_name in internal_names}
    for path in sorted(package.rglob("SKILL.md")):
        if path not in allowed_skill_files:
            fail(f"SKILL.md は skills/<entry>/ か internal/<name>/ にだけ置く: {path}")
    allowed_manifest_roots = {package} | sidecar_roots
    for path in sorted(package.rglob(".codex-plugin")) + sorted(package.rglob(".claude-plugin")):
        if path.parent not in allowed_manifest_roots:
            fail(f".claude-plugin / .codex-plugin は package root と hook sidecar root にだけ置く: {path}")

    # 隣接 playbook.yml と禁止参照形
    callable_skills = set(public_names) | internal_callable
    for entry in public_names:
        root = package / "skills" / entry
        document = None
        if (root / "playbook.yml").exists():
            document = validate_playbook_yml(root / "playbook.yml", entry, marketplace, callable_skills)
        scan_skill_root(root, document)
    for internal_name in internal_names:
        root = package / "internal" / internal_name
        document = None
        if (root / "playbook.yml").exists():
            document = validate_playbook_yml(root / "playbook.yml", internal_name, marketplace, callable_skills)
        scan_skill_root(root, document)


def validate_repository(repository: Path) -> None:
    if not repository.is_absolute():
        fail(f"repository は絶対パスで指定する: {repository}")
    if repository.is_symlink() or not repository.is_dir():
        fail(f"repository が directory ではない: {repository}")
    codex_name, codex = catalog(repository, "codex")
    claude_name, claude = catalog(repository, "claude")
    if codex_name != claude_name:
        fail(f"Codex / Claude marketplace の name が一致しない: {codex_name!r} != {claude_name!r}")
    if codex != claude:
        fail("Codex / Claude marketplace の package identity が一致しない")
    for identity in codex:
        validate_package(repository, identity, codex_name)
    print(f"Root contract: passed ({repository}, packages={len(codex)})")


# ---- self-test: 合成 fixture に対する正例・反例・境界例 ------------------------------------------

P = "plugins/pkg"


def write_fixture(root: Path) -> None:
    package = root / P
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".agents" / "plugins").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"name": "pkg", "plugins": [{"name": "pkg", "version": "1.0.0", "source": "./plugins/pkg"}]}), encoding="utf-8")
    (root / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps(
        {"name": "pkg", "plugins": [{"name": "pkg", "version": "1.0.0", "source": {"source": "local", "path": "./plugins/pkg"}}]}), encoding="utf-8")
    # 公開 playbook: alpha（CONTRACT.md あり、外部 grill と内部 shared-judgment を使う）
    alpha = package / "skills" / "alpha"
    (alpha / "references").mkdir(parents=True)
    (alpha / "scripts").mkdir()
    (alpha / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: alpha の仕事\n---\n\n# alpha\n\n"
        "[工程順序](playbook.yml)に従う。検査は `scripts/check.py` を入口相対で実行し、終了code 0 を成功とする。\n",
        encoding="utf-8")
    (alpha / "CONTRACT.md").write_text("# alpha 公開契約\n\n入力 `request`、出力 `status`。\n", encoding="utf-8")
    (alpha / "references" / "guide.md").write_text("判断の手引き。\n", encoding="utf-8")
    (alpha / "scripts" / "check.py").write_text("print('ok')\n", encoding="utf-8")
    (alpha / "playbook.yml").write_text(
        "version: 2\nname: alpha\ninputs: [request]\nrequires:\n  - {plugin: grill, marketplace: grill}\nsteps:\n"
        "  - {id: settle, playbook: grill, purpose: 問う, needs: [request], provides: [decisions]}\n"
        "  - {id: assess, agent_work: invoking_agent, purpose: 同じagentが判断する, needs: [decisions], provides: [assessment]}\n"
        "  - {id: judge, skill: shared-judgment, purpose: 共有判断を適用する, needs: [assessment]}\n"
        "  - {id: check, script: scripts/check.py, purpose: 検査する}\n", encoding="utf-8")
    # 公開入口: beta（CONTRACT.md 無し、隣接 playbook.yml あり）
    beta = package / "skills" / "beta"
    beta.mkdir(parents=True)
    (beta / "SKILL.md").write_text("---\nname: beta\ndescription: beta の仕事\n---\n\n# beta\n\n自己完結の手順。\n", encoding="utf-8")
    (beta / "playbook.yml").write_text(
        "version: 2\nname: beta\nsteps:\n"
        "  - {id: inspect, agent_work: invoking_agent, purpose: 調べる}\n"
        "  - {id: judge, skill: shared-judgment, purpose: 共有判断を適用する}\n", encoding="utf-8")
    # 内部 skill: shared-judgment（alpha と beta が共有）
    internal = package / "internal" / "shared-judgment"
    internal.mkdir(parents=True)
    (internal / "SKILL.md").write_text("---\nname: shared-judgment\ndescription: 共有する判断責務\n---\n\n# shared-judgment\n\n判断規律。\n", encoding="utf-8")
    # skill でない package 共有 code
    (package / "lib").mkdir()
    (package / "lib" / "store.py").write_text("STORE = {}\n", encoding="utf-8")
    manifest = {"name": "pkg", "version": "1.0.0", "skills": ["./skills/alpha", "./skills/beta"],
                "metadata": {"harness": {"marketplace": "pkg", "contractVersion": 1,
                                         "playbooks": {"alpha": "./skills/alpha"},
                                         "internalPlugins": {"shared-judgment": "./internal/shared-judgment"},
                                         "implements": [{"id": "pkg/alpha", "version": 1, "kind": "playbook", "playbook": "alpha"}]}}}
    for rt in ("codex", "claude"):
        (package / f".{rt}-plugin").mkdir()
        data = dict(manifest)
        if rt == "codex":
            data = data | {"interface": {"capabilities": ["Skills"]}}
        (package / f".{rt}-plugin" / "plugin.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def expect(label: str, expected: str | None, mutate: Callable[[Path], None]) -> None:
    with tempfile.TemporaryDirectory(prefix="root-contract-") as temporary:
        root = Path(temporary) / "repo"
        write_fixture(root)
        mutate(root)
        try:
            validate_repository(root)
        except ContractError as exc:
            if expected is None or expected not in str(exc):
                fail(f"「{label}」が期待と違う理由で失敗: {exc}")
            print(f"Root negative: passed ({label})")
            return
        if expected is not None:
            fail(f"負例「{label}」を拒否できない")
        print(f"Root positive: passed ({label})")


def append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        fail(f"self-test fixture に置換対象が無い: {path}: {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def edit_manifest(root: Path, change: Callable[[dict], None], runtimes: tuple[str, ...] = ("codex", "claude")) -> None:
    for rt in runtimes:
        path = root / P / f".{rt}-plugin" / "plugin.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def edit_harness(root: Path, change: Callable[[dict], None]) -> None:
    edit_manifest(root, lambda m: change(m["metadata"]["harness"]))


def make_hook_sidecar(root: Path, *, create_hooks: bool) -> None:
    sidecar = root / P / "internal/session-hooks"
    sidecar.mkdir(parents=True)
    edit_harness(root, lambda h: h["internalPlugins"].__setitem__("session-hooks", "./internal/session-hooks"))
    for runtime in ("codex", "claude"):
        relative = f"./hooks/{runtime}-hooks.json"
        manifest = sidecar / f".{runtime}-plugin/plugin.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"name": "session-hooks", "version": "1.0.0", "hooks": relative}), encoding="utf-8")
        if create_hooks:
            write(sidecar / relative[2:], '{"hooks":{}}\n')


def alpha_playbook(root: Path, text: str) -> None:
    (root / P / "skills/alpha/playbook.yml").write_text(text, encoding="utf-8")


def self_test(repository: Path | None) -> None:
    if repository is not None:
        validate_repository(repository)
    A = f"{P}/skills/alpha"
    B = f"{P}/skills/beta"
    I = f"{P}/internal/shared-judgment"

    # 正例・境界例（配置と manifest）
    expect("正例", None, lambda r: None)
    expect("業務語としてのplaybookを許容", None, lambda r: write(r / A / "references/notes.md", "playbookという語を業務説明に使う。prepareは業務語。\n"))
    expect("SKILL.md を含まない package 共有 directory を許容", None, lambda r: write(r / P / "core/scripts/tool", "#!/bin/sh\n"))
    expect("Codex manifest の interface 差を不合格にしない", None, lambda r: edit_manifest(
        r, lambda m: m.__setitem__("interface", {"displayName": "Pkg"}), ("codex",)))
    expect("hook sidecar（SKILL.md 無し）を許容", None, lambda r: make_hook_sidecar(r, create_hooks=True))
    expect("playbooks 無し package（implements 無し）を許容", None, lambda r: (
        (r / A / "CONTRACT.md").unlink(),
        edit_harness(r, lambda h: (h.pop("playbooks"), h.pop("implements"))),
    ))
    expect("公開入口の隣接 playbook.yml 無しを許容", None, lambda r: (r / B / "playbook.yml").unlink())
    expect("YAML commentを許容", None, lambda r: replace(r / A / "playbook.yml", "requires:\n", "requires: # 公開依存\n  # grillへ質問を委譲\n"))
    expect("quoted # をcomment扱いしない", None, lambda r: replace(r / A / "playbook.yml", "purpose: 問う", "purpose: '問う # 記号'"))
    expect("SKILL frontmatter nameの末尾commentを許容", None, lambda r: replace(r / I / "SKILL.md", "name: shared-judgment", "name: shared-judgment # 内部名"))
    expect("SKILL frontmatterのquoted nameを許容", None, lambda r: replace(r / I / "SKILL.md", "name: shared-judgment", "name: 'shared-judgment'"))
    expect("block mappingを許容", None, lambda r: replace(r / A / "playbook.yml", "  - {plugin: grill, marketplace: grill}", "  - plugin: grill\n    marketplace: grill"))
    expect("別marketplaceの同名pluginをplaybookで呼べる", None, lambda r: (
        replace(r / A / "playbook.yml", "plugin: grill", "plugin: alpha"),
        replace(r / A / "playbook.yml", "marketplace: grill", "marketplace: another-market"),
        replace(r / A / "playbook.yml", "playbook: grill", "playbook: alpha"),
    ))
    expect("公開入口を skill: で呼べる", None, lambda r: replace(r / B / "playbook.yml", "skill: shared-judgment", "skill: alpha"))

    # 反例（配置と manifest）
    expect("skills の文字列宣言", "文字列 \"./skills/\" は不可", lambda r: edit_manifest(r, lambda m: m.__setitem__("skills", "./skills/")))
    expect("installationSurface", "installationSurface を置かない", lambda r: edit_harness(r, lambda h: h.__setitem__("installationSurface", "playbook-package")))
    expect("entryRoot", "entryRoot を置かない", lambda r: edit_harness(r, lambda h: h.__setitem__("entryRoot", "./skills/alpha")))
    expect("marketplace 宣言の欠落", "marketplace が両 marketplace の name と一致しない", lambda r: edit_harness(r, lambda h: h.pop("marketplace")))
    expect("marketplace 宣言の不一致", "marketplace が両 marketplace の name と一致しない", lambda r: edit_harness(r, lambda h: h.__setitem__("marketplace", "other")))
    expect("contractVersion の欠落", "contractVersion は整数", lambda r: edit_harness(r, lambda h: h.pop("contractVersion")))
    expect("contractVersion の文字列", "contractVersion は整数", lambda r: edit_harness(r, lambda h: h.__setitem__("contractVersion", "1")))
    expect("marketplace source が plugins 直下", "2階層", lambda r: (
        (r / ".claude-plugin/marketplace.json").write_text(json.dumps({"name": "pkg", "plugins": [{"name": "pkg", "version": "1.0.0", "source": "./plugins"}]}), encoding="utf-8"),
        (r / ".agents/plugins/marketplace.json").write_text(json.dumps({"name": "pkg", "plugins": [{"name": "pkg", "version": "1.0.0", "source": {"source": "local", "path": "./plugins"}}]}), encoding="utf-8"),
    ))
    expect("plugins 直下の manifest", "plugins/ 直下に manifest を置かない", lambda r: write(r / "plugins/.claude-plugin/plugin.json", "{}"))
    expect("marketplace name の runtime 間不一致", "marketplace の name が一致しない", lambda r: (r / ".claude-plugin/marketplace.json").write_text(
        json.dumps({"name": "other", "plugins": [{"name": "pkg", "version": "1.0.0", "source": "./plugins/pkg"}]}), encoding="utf-8"))
    expect("公開入口の SKILL name が directory 名と違う", "SKILL.md name が directory 名と一致しない", lambda r: replace(r / B / "SKILL.md", "name: beta", "name: apply-beta"))
    expect("skills/ 直下の未宣言 directory", "manifest が宣言しない directory", lambda r: write(r / P / "skills/gamma/SKILL.md", "---\nname: gamma\n---\n"))
    expect("manifest が宣言した入口が無い", "公開入口 directory が無い", lambda r: edit_manifest(r, lambda m: m["skills"].append("./skills/gamma")))
    expect("三階層の skills 宣言", "./skills/<entry> でなければならない", lambda r: edit_manifest(r, lambda m: m["skills"].append("./skills/cat/gamma")))
    expect("CONTRACT.md を持つ入口が playbooks に無い", "playbooks に無い入口が CONTRACT.md を持つ", lambda r: write(r / B / "CONTRACT.md", "# beta\n"))
    expect("playbooks の入口に CONTRACT.md が無い", "公開 playbook に CONTRACT.md が無い", lambda r: (r / A / "CONTRACT.md").unlink())
    expect("playbooks の入口に playbook.yml が無い", "公開 playbook に playbook.yml が無い", lambda r: (r / A / "playbook.yml").unlink())
    expect("playbooks の key が skills に無い", "playbooks の key が skills の要素名に無い", lambda r: edit_harness(r, lambda h: h["playbooks"].__setitem__("gamma", "./skills/gamma")))
    expect("playbooks の値が ./skills/<key> でない", "./skills/alpha でなければならない", lambda r: edit_harness(r, lambda h: h["playbooks"].__setitem__("alpha", "./playbooks/alpha")))
    expect("implements の kind が skill", "kind: playbook", lambda r: edit_harness(r, lambda h: h["implements"][0].__setitem__("kind", "skill")))
    expect("implements の id 不一致", "implements[0].id は pkg/alpha", lambda r: edit_harness(r, lambda h: h["implements"][0].__setitem__("id", "other/alpha")))
    expect("implements の欠落", "implements は playbooks の各 key につき1件", lambda r: edit_harness(r, lambda h: h.__setitem__("implements", [])))
    expect("playbooks 無しの implements", "playbooks が無い package に implements を置かない", lambda r: (
        (r / A / "CONTRACT.md").unlink(),
        edit_harness(r, lambda h: h.pop("playbooks")),
    ))
    expect("internalPlugins の値が ./internal/<name> でない", "./internal/shared-judgment でなければならない", lambda r: edit_harness(
        r, lambda h: h["internalPlugins"].__setitem__("shared-judgment", "./skills/shared-judgment")))
    expect("内部 skill の SKILL name が directory 名と違う", "内部 skill の SKILL.md name が directory 名と一致しない", lambda r: replace(r / I / "SKILL.md", "name: shared-judgment", "name: judgment"))
    expect("内部 skill 名が公開入口名と同じ", "公開入口の名前と同じ", lambda r: (
        edit_harness(r, lambda h: (h["internalPlugins"].__setitem__("beta", "./internal/beta"), h["internalPlugins"].pop("shared-judgment"))),
        (r / I).rename(r / P / "internal/beta"),
        replace(r / P / "internal/beta/SKILL.md", "name: shared-judgment", "name: beta"),
    ))
    expect("internal/ 直下の未宣言 directory", "internalPlugins が宣言しない directory", lambda r: write(r / P / "internal/extra/SKILL.md", "---\nname: extra\n---\n"))
    expect("internal/ 外の SKILL.md", "skills/<entry>/ か internal/<name>/ にだけ置く", lambda r: write(r / P / "core/SKILL.md", "---\nname: core\n---\n"))
    expect("入口配下の nested SKILL.md", "skills/<entry>/ か internal/<name>/ にだけ置く", lambda r: write(r / A / "skills/nested/SKILL.md", "---\nname: nested\n---\n"))
    expect("内部 skill の nested manifest", "nested runtime manifest を置かない", lambda r: write(r / I / ".claude-plugin/plugin.json", json.dumps({"name": "shared-judgment", "version": "1.0.0"})))
    expect("公開入口の nested manifest", "package root と hook sidecar root にだけ置く", lambda r: (
        write(r / A / ".codex-plugin/plugin.json", json.dumps({"name": "alpha", "version": "1.0.0", "hooks": "./hooks/x.json"})),
        write(r / A / ".claude-plugin/plugin.json", json.dumps({"name": "alpha", "version": "1.0.0", "hooks": "./hooks/x.json"})),
        write(r / A / "hooks/x.json", "{}"),
    ))
    expect("hook sidecar が SKILL.md を持つ", "hook sidecar は SKILL.md を持たない", lambda r: (
        make_hook_sidecar(r, create_hooks=True),
        write(r / P / "internal/session-hooks/SKILL.md", "---\nname: session-hooks\n---\n"),
    ))
    expect("内部 skill が CONTRACT.md を持つ", "内部 skill は CONTRACT.md を持たない", lambda r: write(r / I / "CONTRACT.md", "# 内部契約\n"))
    expect("hook sidecar が CONTRACT.md を持つ", "内部 skill は CONTRACT.md を持たない", lambda r: (
        make_hook_sidecar(r, create_hooks=True),
        write(r / P / "internal/session-hooks/CONTRACT.md", "# 契約\n"),
    ))
    expect("hook sidecar の identity 不一致", "runtime identity が一致しない", lambda r: (
        make_hook_sidecar(r, create_hooks=True),
        replace(r / P / "internal/session-hooks/.codex-plugin/plugin.json", '"version": "1.0.0"', '"version": "1.0.1"'),
    ))
    expect("存在しないhookを宣言", "hook 入口が regular file として実在しない", lambda r: make_hook_sidecar(r, create_hooks=False))
    expect("hookだけのsidecarをskillとして呼ばない", "実在skillに無い", lambda r: (
        make_hook_sidecar(r, create_hooks=True),
        replace(r / B / "playbook.yml", "skill: shared-judgment", "skill: session-hooks"),
    ))
    expect("manifest の runtime 間不一致", "一致しない", lambda r: edit_manifest(r, lambda m: m.__setitem__("version", "1.0.1"), ("codex",)))
    expect("marketplace と manifest の version 不一致", "identity が marketplace と一致しない", lambda r: edit_manifest(r, lambda m: m.__setitem__("version", "1.0.1")))
    expect("symlink", "symlink", lambda r: (r / P / "link").symlink_to(r / P / "skills"))

    # 隣接 playbook.yml
    expect("playbook.yml name が directory 名と違う", "name が directory 名と一致しない", lambda r: replace(r / A / "playbook.yml", "name: alpha", "name: alpha-flow"))
    expect("playbook.yml version 1", "version 2", lambda r: replace(r / A / "playbook.yml", "version: 2", "version: 1"))
    expect("requires に自 marketplace", "自 marketplace の要素がある", lambda r: (
        replace(r / A / "playbook.yml", "{plugin: grill, marketplace: grill}", "{plugin: shared-judgment, marketplace: pkg}"),
        replace(r / A / "playbook.yml", "playbook: grill", "playbook: shared-judgment"),
    ))
    expect("外部 package を skill: で参照", "playbook: からだけ", lambda r: replace(r / A / "playbook.yml", "playbook: grill", "skill: grill"))
    expect("requiresに無いplaybookを呼ぶ", "requires に宣言されていない", lambda r: replace(r / A / "playbook.yml", "  - {plugin: grill, marketplace: grill}", "  []"))
    expect("requiresだけで外部依存を使用しない", "対応する playbook step が無い", lambda r: replace(r / A / "playbook.yml", "playbook: grill", "agent_work: invoking_agent"))
    expect("requires に plugin と marketplace 以外", "plugin と marketplace だけ", lambda r: replace(r / A / "playbook.yml", "{plugin: grill, marketplace: grill}", "{plugin: grill, marketplace: grill, version: 1}"))
    expect("未宣言のskillを呼ぶ", "実在skillに無い", lambda r: replace(r / B / "playbook.yml", "skill: shared-judgment", "skill: does-not-exist"))
    expect("存在しないscript", "regular fileとして実在しない", lambda r: replace(r / A / "playbook.yml", "scripts/check.py", "scripts/missing.py"))
    expect("入口外のscript", "scripts/ 配下でなければならない", lambda r: replace(r / A / "playbook.yml", "scripts/check.py", "../beta/check.py"))
    expect("工程idの重複", "id が重複", lambda r: replace(r / A / "playbook.yml", "id: assess", "id: settle"))
    expect("実行種別欠落", "どれか1つだけ", lambda r: replace(r / B / "playbook.yml", "agent_work: invoking_agent, ", ""))
    expect("実行種別重複", "どれか1つだけ", lambda r: replace(r / B / "playbook.yml", "agent_work: invoking_agent, ", "agent_work: invoking_agent, script: scripts/x.py, "))
    expect("同一agent工程の不正な担当値", "invoking_agent", lambda r: replace(r / B / "playbook.yml", "agent_work: invoking_agent", "agent_work: another_agent"))
    expect("後方工程へのneed", "後方の未実行工程", lambda r: replace(r / A / "playbook.yml", "purpose: 問う, needs: [request], provides", "purpose: 問う, needs: [assessment], provides"))
    expect("未宣言入力へのneed", "inputsまたは先行provides", lambda r: replace(r / A / "playbook.yml", "needs: [request]", "needs: [unknown]"))
    expect("needsの誤型", "空でない文字列のlist", lambda r: replace(r / A / "playbook.yml", "needs: [decisions]", "needs: decisions"))
    expect("inputsの明示null", "空でない文字列のlist", lambda r: replace(r / A / "playbook.yml", "inputs: [request]", "inputs: null"))
    expect("providesの明示null", "空でない文字列のlist", lambda r: replace(r / A / "playbook.yml", "provides: [decisions]", "provides: null"))
    expect("条件付きneedの先行到達", None, lambda r: alpha_playbook(r,
        "version: 2\nname: alpha\ninputs: [request]\nsteps:\n"
        "  - {id: inspect, agent_work: invoking_agent, purpose: 調べる, needs: [request], provides: [evidence]}\n"
        "  - id: report\n    agent_work: invoking_agent\n    purpose: 報告する\n"
        "    conditional_needs: [{when: request.report, needs: [evidence]}]\n"))
    expect("条件付きneedの未宣言入力", "inputsまたは先行provides", lambda r: alpha_playbook(r,
        "version: 2\nname: alpha\ninputs: [request]\nsteps:\n"
        "  - id: inspect\n    agent_work: invoking_agent\n    purpose: 調べる\n"
        "    conditional_needs: [{when: request.report, needs: [unknown]}]\n"))
    expect("条件付きneedのwhen誤型", "when は空でない文字列", lambda r: alpha_playbook(r,
        "version: 2\nname: alpha\nsteps:\n"
        "  - id: inspect\n    agent_work: invoking_agent\n    purpose: 調べる\n"
        "    conditional_needs: [{when: null, needs: []}]\n"))
    expect("条件付きneedの空list", None, lambda r: alpha_playbook(r,
        "version: 2\nname: alpha\nsteps:\n"
        "  - id: inspect\n    agent_work: invoking_agent\n    purpose: 調べる\n"
        "    conditional_needs: []\n"))
    expect("条件付きneedのnull", "list でなければならない", lambda r: alpha_playbook(r,
        "version: 2\nname: alpha\nsteps:\n"
        "  - id: inspect\n    agent_work: invoking_agent\n    purpose: 調べる\n"
        "    conditional_needs: null\n"))
    expect("条件付きneed要素のneeds null", "空でない文字列のlist", lambda r: alpha_playbook(r,
        "version: 2\nname: alpha\nsteps:\n"
        "  - id: inspect\n    agent_work: invoking_agent\n    purpose: 調べる\n"
        "    conditional_needs: [{when: request.report, needs: null}]\n"))
    expect("不正YAML", "有効な YAML ではない", lambda r: append(r / A / "playbook.yml", "\ninvalid: [\n"))
    expect("本文のnameをfrontmatterとして扱わない", "name は空でない文字列", lambda r: replace(
        r / I / "SKILL.md", "name: shared-judgment\ndescription:", "description: nameなし\n---\n\nname: shared-judgment\n\n---\ndescription:"))
    expect("SKILL frontmatterのname欠落", "name は空でない文字列", lambda r: replace(r / I / "SKILL.md", "name: shared-judgment", "summary: nameなし"))

    # 禁止参照形（正例・反例・境界例）
    expect("入口相対 path の tool 契約を許容", None, lambda r: append(r / A / "SKILL.md", "\n`../../lib/store.py` を入口相対で使う。失敗時は終了code 1 で停止する。\n"))
    expect("設定 file 置き場の環境変数を許容", None, lambda r: append(r / A / "SKILL.md", "\n設定は `${XDG_CONFIG_HOME:-~/.config}/harness-plugins/alpha.config.yml` と `$HOME` 配下から読む。\n"))
    expect("README.md の token は対象外", None, lambda r: write(r / A / "README.md", "旧版は ${CLAUDE_PLUGIN_ROOT} を使った。\n"))
    expect("docs/ の token は対象外", None, lambda r: write(r / "docs/history.md", "旧版は `${.instructions}` と <!-- BEGIN shared:x --> を使った。\n"))
    expect("SKILL.md のマクロ参照", "禁止された参照形がある", lambda r: append(r / A / "SKILL.md", "\n${.instructions.execution.directive}\n"))
    expect("SKILL.md の root 解決 block", "CLAUDE_PLUGIN_ROOT", lambda r: append(
        r / A / "SKILL.md", '\nPLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"\nCFG_FILE=$(bash "${PLUGIN_ROOT}/scripts/prepare.sh" --root-only)\n'))
    expect("コードフェンス内の token も不合格", "BUNDLE_ROOT", lambda r: append(r / A / "SKILL.md", "\n```bash\nbash \"$BUNDLE_ROOT/scripts/prepare.sh\"\n```\n"))
    expect("references の同期 block", "<!-- BEGIN shared:", lambda r: write(r / A / "references/shared.md", "<!-- BEGIN shared:domain -->\n本文\n<!-- END shared:domain -->\n"))
    expect("references 配下の深い階層", "禁止された参照形がある", lambda r: write(r / A / "references/methods/deep.md", "${.document_type}\n"))
    expect("CONTRACT.md の token", "禁止された参照形がある", lambda r: append(r / A / "CONTRACT.md", "\n入口は ${BUNDLE_ROOT}/... で解決する。\n"))
    expect("内部 skill の token", "禁止された参照形がある", lambda r: append(r / I / "SKILL.md", "\n${.playbook.inputs}\n"))
    expect("playbook.yml 文字列値のマクロ", "playbook.yml:steps[0].input.document_type: ${.", lambda r: replace(
        r / A / "playbook.yml", "purpose: 問う, needs: [request]", "purpose: 問う, input: {document_type: '${.document_type}'}, needs: [request]"))
    expect("playbook.yml literal 値を許容", None, lambda r: replace(
        r / A / "playbook.yml", "purpose: 問う, needs: [request]", "purpose: 問う, input: {document_type: domain-rule}, needs: [request]"))
    print("Root contract self-test: passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("repository", nargs="?")
    args = parser.parse_args()
    if args.repository is None and not args.self_test:
        parser.error("repository の絶対パスが要る")
    try:
        repository = Path(args.repository) if args.repository is not None else None
        self_test(repository) if args.self_test else validate_repository(repository)
    except (OSError, UnicodeError, ContractError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
