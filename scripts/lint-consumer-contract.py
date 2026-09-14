#!/usr/bin/env python3
"""消費側の文書・設定・scriptに、外部依存の内部の作りが漏れていないか静的に見る。

resolver は playbook.yml しか見ない。ここは SKILL.md・README・references・scripts・
`.harness-plugins` 配下の設定を含む全行を見る。両者が同じ規則を二重に強制する。

**除外機構は無い。例外コメントも無い。** 規則（外部pluginの公開面はplaybook 1枚だけ）は
例外を持たない規則であり、例外機構は合法な抜け道を作る。偽陽性が出たら検出語を直すか、
その記述自体を消す。

  lint-consumer-contract.py [--repo <path>] [--json]

exit 0 = 違反なし / 1 = 違反あり / 2 = 実行できない。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys


# manifest から生成できない語（保存モード名・CLI引数）だけを手で持つ。
# 内部plugin名・内部skill名・工程idは provider manifest から生成する（internal_vocabulary）。
#
# **手書きの語も、その持ち主が「外部依存として実在するとき」だけ有効にする。**
# 提供側 repository 自身の内部参照（自分の decision.py や自分の control.py）は
# 消費ではないので検出しない。所属 bundle 内の参照は §3 のとおり規則の対象外である。
FIXED_VOCABULARY = {
    "write-doc": ("save-vocabulary", [
        "replace-existing-target", "--template", "--output-dir",
        "write-doc.sh", "render-meta.py", '"decision":"exists"', "logical_update_target",
    ]),
    "agent-work-policy": ("policy-internal", [
        "control.py", "POLICY_ROOT", "--policy-root", "operation-contract.md",
    ]),
}

TEXT_SUFFIXES = {".md", ".yml", ".yaml", ".sh", ".py", ".json", ".txt"}
SKIP_DIRECTORIES = {".git", "__pycache__", "node_modules", ".harness-plugin-test-cache"}

# 相手の工程を名指しする形だけを見る（裸語検出はしない）。
STEP_INVOCATION = re.compile(r"--(?:check-steps|step)[=\s]+[\"']?([A-Za-z0-9._-]+)")
ASSIGN = re.compile(
    r"^\s*(?:export\s+)?(?P<var>[A-Za-z_][A-Za-z0-9_]*)=.*"
    r"\.deps(?:\.|\[[\"'])(?P<dep>[A-Za-z0-9_-]+)"
)


def load_resolver(repo: Path):
    candidates = [repo / "shared/playbook/resolve-dependency.py",
                  repo / "shared/runtime-source/resolve-dependency.py",
                  *sorted(repo.glob("plugins/**/scripts/resolve-dependency.py"))]
    for candidate in candidates:
        if candidate.is_file():
            sys.dont_write_bytecode = True
            spec = importlib.util.spec_from_file_location("harness_resolver", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


def yaml_load(path: Path):
    result = subprocess.run(["yq", "-o=json", "-I=0", ".", os.fspath(path)],
                            capture_output=True, text=True)
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def skill_name(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or lines[0] != "---":
        return None
    for line in lines[1:]:
        if line == "---":
            break
        if line.startswith("name: "):
            return line[6:].strip().strip("\"'")
    return None


def provider_step_ids(package_root: Path, runtime: str) -> set[str]:
    """公開playbookの工程idを集める。

    工程idは `type` `save` `draft` `review` のような普通の語なので、裸の単語として
    全文検索すると偽陽性しか出ない。呼び出し側は「相手の工程を名指しする形」でのみ照合する。"""
    manifest = package_root / f".{runtime}-plugin/plugin.json"
    if not manifest.is_file():
        return set()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    harness = (data.get("metadata") or {}).get("harness") or {}
    ids: set[str] = set()
    for _, relative in (harness.get("playbooks") or {}).items():
        playbook = yaml_load(package_root / relative / "playbook.yml")
        if isinstance(playbook, dict):
            for step in playbook.get("steps") or []:
                if isinstance(step, dict) and isinstance(step.get("id"), str):
                    ids.add(step["id"])
    return ids


def internal_vocabulary(package_root: Path, runtime: str) -> set[str]:
    """provider の manifest から内部plugin名・内部skill名を集める（工程idは含めない）。"""
    manifest = package_root / f".{runtime}-plugin/plugin.json"
    if not manifest.is_file():
        return set()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    harness = (data.get("metadata") or {}).get("harness") or {}
    names: set[str] = set()
    for plugin_name, relative in (harness.get("internalPlugins") or {}).items():
        names.add(plugin_name)
        component = package_root / relative
        # 1段下だけでは skills/apply-work-policy/SKILL.md のような配置を取りこぼす。
        for skill in sorted(component.rglob("SKILL.md")):
            if skill.is_file() and not skill.is_symlink():
                name = skill_name(skill)
                if name:
                    names.add(name)
    return names


def public_skill_names(package_root: Path, runtime: str) -> set[str]:
    manifest = package_root / f".{runtime}-plugin/plugin.json"
    if not manifest.is_file():
        return set()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    declared = data.get("skills")
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, list):
        declared = []
    names: set[str] = set()
    for relative in declared:
        member = package_root / str(relative)
        for skill in [member / "SKILL.md", *member.glob("*/SKILL.md")]:
            if skill.is_file():
                name = skill_name(skill)
                if name:
                    names.add(name)
    return names


def own_marketplace(repo: Path, runtime: str) -> tuple[str | None, dict]:
    manifest = repo / f"plugins/.{runtime}-plugin/plugin.json"
    if not manifest.is_file():
        return None, {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {}
    harness = (data.get("metadata") or {}).get("harness") or {}
    return harness.get("marketplace"), harness


def resolve_provider(module, playbook_root: Path, marketplace: str, plugin: str, runtime: str):
    if module is None:
        return None
    stderr = sys.stderr
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
    try:
        candidate = module.dev_candidate(f"{marketplace}/{plugin}", runtime, plugin)
        if candidate is None:
            candidate = module.repository_candidate(playbook_root, marketplace, runtime, plugin)
        if candidate is None:
            candidate = module.cache_candidate(marketplace, runtime, plugin, playbook_root)
        return candidate
    except SystemExit:
        return None
    except (OSError, ValueError, KeyError):
        return None
    finally:
        sys.stderr.close()
        sys.stderr = stderr


def target_files(repo: Path) -> list[Path]:
    """除外は設けない。提供側 repository も対象にする。"""
    roots = [repo / "plugins", repo / ".harness-plugins", repo / "scripts",
             repo / "tests", repo / "docs", repo / "releases"]
    files = [repo / "README.md", repo / "AGENTS.md", repo / "CHANGELOG.md"]
    for root in roots:
        if not root.is_dir():
            continue
        for directory, subdirectories, names in os.walk(root):
            subdirectories[:] = sorted(d for d in subdirectories if d not in SKIP_DIRECTORIES)
            for name in sorted(names):
                path = Path(directory) / name
                if path.suffix in TEXT_SUFFIXES or path.suffix == "":
                    files.append(path)
    # 走査しないのは2種だけで、どちらも「消費側が書いた記述」ではない。
    #   (1) 検出語の定義そのものを持つこのlint自身（自己参照）
    #   (2) sync-runtime.py が配った生成物。**basename では外さない。**
    #       runtime-manifest.json の targets に列挙され、かつ sha256 が正本の値と
    #       一致するファイルだけを外す。同名の消費側 script は走査対象のまま残る。
    myself = Path(__file__).resolve()
    generated: set[Path] = set()
    manifest = repo / "shared/runtime-manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            sources = data.get("source", {}).get("files", {})
            for relative, digest in (data.get("targets") or {}).items():
                target = repo / relative
                if not target.is_file() or target.is_symlink():
                    continue
                if sources.get(target.name) != digest:
                    continue
                if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                    generated.add(target.resolve())
        except (OSError, ValueError, AttributeError):
            generated = set()
    return [path for path in files
            if path.is_file() and not path.is_symlink()
            and path.resolve() != myself and path.resolve() not in generated]


def scan(repo: Path, runtime: str) -> list[dict]:
    module = load_resolver(repo)
    if module is None:
        raise ValueError("参照構文の共通解析に resolve-dependency.py が要る: " + str(repo))
    declared_market, harness = own_marketplace(repo, runtime)
    internals = set((harness.get("internalPlugins") or {}))
    external_logical: dict[str, dict] = {}
    # 内部依存は §3 のとおり外部規則の対象外だが、`.skills.<名前>` だけは実在を見る。
    # 値は解決できた公開skill名の集合、解決できなければ None（判定しない）。
    internal_logical: dict[str, set[str] | None] = {}
    unresolved: set[str] = set()
    for playbook_file in sorted(repo.glob("plugins/**/playbook.yml")):
        data = yaml_load(playbook_file)
        if not isinstance(data, dict):
            continue
        for requirement in data.get("requires") or []:
            if not isinstance(requirement, dict):
                continue
            plugin, marketplace = requirement.get("plugin"), requirement.get("marketplace")
            if not isinstance(plugin, str) or not isinstance(marketplace, str):
                continue
            if marketplace == declared_market:
                # 自 package の内部依存。外部規則（§3）の対象外だが、
                # 内部 skill 名を指す形は、その名前が実在するかだけ見る。
                if plugin not in internal_logical:
                    candidate = resolve_provider(module, playbook_file.parent, marketplace, plugin, runtime)
                    internal_logical[plugin] = set(candidate["skills"]) if candidate else None
                continue
            if plugin in external_logical:
                continue
            candidate = resolve_provider(module, playbook_file.parent, marketplace, plugin, runtime)
            if candidate is None:
                unresolved.add(f"{marketplace}/{plugin}")
                external_logical[plugin] = {"vocabulary": set(), "steps": set(), "skills": set(), "resolved": False}
            else:
                package_root = Path(candidate["package_root"])
                external_logical[plugin] = {
                    "vocabulary": internal_vocabulary(package_root, runtime),
                    "steps": provider_step_ids(package_root, runtime),
                    "skills": public_skill_names(package_root, runtime),
                    "resolved": True,
                }

    vocabulary: dict[str, str] = {}
    public_skills: dict[str, str] = {}
    # 手書き語は、その持ち主が外部依存として実在するときだけ効かせる。
    fixed: list[tuple[str, str, str]] = []
    for name, info in external_logical.items():
        for word in info["vocabulary"]:
            vocabulary.setdefault(word, name)
        for word in info["skills"]:
            public_skills.setdefault(word, name)
        code, words = FIXED_VOCABULARY.get(name, (None, []))
        for word in words:
            fixed.append((code, word, name))
    step_ids: dict[str, str] = {}
    for name, info in external_logical.items():
        for word in info.get("steps") or ():
            step_ids.setdefault(word, name)

    findings: list[dict] = []

    def report(code: str, path: Path, line: int, detail: str) -> None:
        findings.append({"code": code, "file": str(path), "line": line, "detail": detail})

    # (1) playbook.yml の steps 宣言
    for playbook_file in sorted(repo.glob("plugins/**/playbook.yml")):
        data = yaml_load(playbook_file)
        if not isinstance(data, dict):
            continue
        for step in data.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if isinstance(step.get("skill"), str) and step["skill"] in public_skills:
                report("external-dependency-skill", playbook_file, 0,
                       f"step={step.get('id')} skill={step['skill']} plugin={public_skills[step['skill']]}")
            if isinstance(step.get("plugin"), str) and step["plugin"] in external_logical:
                report("external-dependency-script", playbook_file, 0,
                       f"step={step.get('id')} plugin={step['plugin']}")

    for path in target_files(repo):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        tainted: dict[str, tuple[int, str]] = {}
        for number, line in enumerate(lines, 1):
            assignment = ASSIGN.search(line)
            if assignment and assignment.group("dep") in external_logical:
                tainted[assignment.group("var")] = (number, assignment.group("dep"))
            for name, segments, suffix, raw in module.dep_references(line):
                if name in external_logical:
                    code = module.dep_reference_violation(segments, suffix, True)
                elif name in internal_logical:
                    code = module.dep_reference_violation(
                        segments, suffix, False, internal_logical[name])
                else:
                    continue
                if code:
                    report(code, path, number, raw)
            for match in module.CONFIG_REFERENCE.finditer(line):
                if match.group("name") in external_logical:
                    report("external-dependency-config", path, number, match.group(0))
            for variable, (origin, dep) in tainted.items():
                if re.search(r"[\"'$]?\$\{?" + re.escape(variable) + r"\}?/", line) and number != origin:
                    report("external-dependency-exec", path, number,
                           f"{variable}（{origin}行で .deps.{dep} から代入）: " + line.strip()[:120])
            # 工程idは名指しの形（--check-steps <id> / --step <id> /
            # ${.deps.<外部>...} と同じ行）でだけ照合する。
            for invoked in STEP_INVOCATION.findall(line):
                if invoked in step_ids:
                    report("provider-internal-name", path, number,
                           f"{invoked}（{step_ids[invoked]} の工程id）")
            for name, _segments, _suffix, _raw in module.dep_references(line):
                if name not in external_logical:
                    continue
                for word, owner in step_ids.items():
                    if owner == name and re.search(
                            r"(?<![A-Za-z0-9_-])" + re.escape(word) + r"(?![A-Za-z0-9_-])", line):
                        report("provider-internal-name", path, number,
                               f"{word}（{owner} の工程id）")
            for word, owner in vocabulary.items():
                if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(word) + r"(?![A-Za-z0-9_-])", line):
                    report("provider-internal-name", path, number, f"{word}（{owner} の内部名）")
            for code, word, owner in fixed:
                if word in line:
                    report(code, path, number, f"{word}（{owner} の内部語）")

    # 外部playbookの設定ファイル（<repo>/.harness-plugins/<外部名>.config.yml と
    # scopes/*/<外部名>.config.yml）に相手の工程を並べ直すのも「内部の作りの想定」である。
    settings = repo / ".harness-plugins"
    if settings.is_dir():
        for config in sorted(settings.rglob("*.config.yml")):
            owner = config.name[: -len(".config.yml")]
            declared = step_ids and {word for word, holder in step_ids.items() if holder == owner}
            if not declared:
                continue
            data = yaml_load(config)
            if not isinstance(data, dict):
                continue
            for step in data.get("steps") or []:
                if isinstance(step, dict) and step.get("id") in declared:
                    report("provider-internal-name", config, 0,
                           f"{step['id']}（{owner} の工程id）")

    if unresolved:
        findings.append({
            "code": "provider-unresolved",
            "file": str(repo),
            "line": 0,
            "detail": "内部名を生成できない外部依存: " + ", ".join(sorted(unresolved)),
        })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--runtime", default=os.environ.get("HARNESS_PLUGIN_RUNTIME", "claude"),
                        choices=["claude", "codex"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print("[error] repository directory が無い: " + str(repo), file=sys.stderr)
        return 2
    findings = scan(repo, args.runtime)
    if args.json:
        print(json.dumps({"schema": 1, "repo": str(repo), "findings": findings},
                         ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(f"[{finding['code']}] {finding['file']}:{finding['line']} {finding['detail']}")
        print(f"consumer contract lint: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print("[error] " + str(exc), file=sys.stderr)
        raise SystemExit(2)
