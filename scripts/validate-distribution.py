#!/usr/bin/env python3
"""Validate catalog, manifest, source, and license distribution invariants."""

from __future__ import annotations

import json
import os
import stat
import sys
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


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} REPOSITORY_ROOT", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve(strict=True)
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
        source_root = (root / source).resolve(strict=True)
        if source_root == plugins_root or plugins_root not in source_root.parents:
            fail(f"plugin sourceがplugins配下ではありません: {name}")
        expected_roots.add(source_root)
        source_license = source_root / "LICENSE"
        require_regular_file(source_license)
        if source_license.read_bytes() != license_bytes:
            fail(f"source LICENSEがrepository rootと一致しません: {name}")
        for runtime in ("claude", "codex"):
            manifest = source_root / f".{runtime}-plugin/plugin.json"
            require_regular_file(manifest)
            identity = load_json(manifest)
            if identity.get("name") != name or identity.get("version") != version:
                fail(f"{runtime} manifestのname/versionがcatalogと一致しません: {name}")

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


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Distribution: failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
