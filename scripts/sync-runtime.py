#!/usr/bin/env python3
"""Development-only generation; maintenance-tool copies remain self-contained.

複製先は repository の保守領域だけである（`scripts/`、`shared/playbook/`、`.github/workflows/`）。
配布物 `plugins/<package>/` には何も複製しない。

  scripts/<REPO_SCRIPTS>                  全 repository
  scripts/validate-distribution.py        既存の runtime-manifest.json が targets に持つ repository、
                                          または scripts/ にその名の file が無い repository
  shared/playbook/resolve-dependency.py   shared/playbook/ を持つ repository（doctor / lint が読む）
  .github/workflows/validate.yml          全 repository
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

VERSION = '3.0.0'
NAMES = ['resolve-dependency.py', 'validate-distribution.py', 'doctor.py', 'release.py', 'evaluate-skills.py', 'claude-eval-adapter.py', 'sync-runtime.py', 'test-hardening.py', 'lint-consumer-contract.py', 'validate.yml']
REPO_SCRIPTS = ['doctor.py', 'release.py', 'evaluate-skills.py', 'claude-eval-adapter.py', 'sync-runtime.py', 'test-hardening.py', 'lint-consumer-contract.py']


def reject_tree_symlinks(root, relatives):
    """No generated-tree input/output may leave its explicit root via a symlink."""
    if not root.is_dir():
        raise ValueError('root is not a directory: ' + str(root))
    for relative in relatives:
        base = root / relative
        if base.is_symlink():
            raise ValueError('tree symlink refused: ' + str(base))
        for directory, dirs, files in os.walk(base, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise ValueError('tree symlink refused: ' + str(path))


def previous_targets(lock):
    if not lock.exists():
        return set()
    try:
        data = json.loads(lock.read_text())
    except ValueError:
        return set()
    return set(data.get('targets', {})) if isinstance(data, dict) else set()


def distributes_validator(repo, lock):
    """validate-distribution.py を配る repository の判定。既に自前の file を持つ repository へは配らない。"""
    if 'scripts/validate-distribution.py' in previous_targets(lock):
        return True
    return not (repo / 'scripts/validate-distribution.py').exists()


def expected_targets(repo, lock):
    expected = {'scripts/' + n for n in REPO_SCRIPTS}
    expected.add('.github/workflows/validate.yml')
    if distributes_validator(repo, lock):
        expected.add('scripts/validate-distribution.py')
    if (repo / 'shared/playbook').is_dir():
        expected.add('shared/playbook/resolve-dependency.py')
    return expected


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    p.add_argument('--source')
    p.add_argument('--check', action='store_true')
    a = p.parse_args()
    repo = Path(a.repo).resolve()
    reject_tree_symlinks(repo, ['plugins', 'scripts', 'shared', '.github'])
    lock = repo / 'shared/runtime-manifest.json'
    if a.source:
        source = Path(a.source).resolve(strict=True)
    elif (repo / 'shared/runtime-source').is_dir():
        source = repo / 'shared/runtime-source'
    elif a.check and lock.exists():
        data = json.loads(lock.read_text())
        if data.get('schema') != 1 or data.get('source', {}).get('version') != VERSION or set(data.get('source', {}).get('files', {})) != set(NAMES):
            raise ValueError('runtime manifest schema/source contract mismatch')
        if set(data.get('targets', {})) != expected_targets(repo, lock):
            raise ValueError('runtime manifest target inventory mismatch')
        errors = []
        for name, digest in data['targets'].items():
            path = repo / name
            source_name = path.name
            if digest != data['source']['files'][source_name] or not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                errors.append(name)
        print(json.dumps({'source': data['source'], 'changed': errors}))
        return bool(errors)
    else:
        p.error('--source requires the development source checkout')
    reject_tree_symlinks(source, ['.'])
    for name in NAMES:
        if not (source / name).is_file():
            raise ValueError('source is not a regular file: ' + name)
    targets = {}
    for relative in sorted(expected_targets(repo, lock)):
        targets[relative] = (source / Path(relative).name).read_bytes()
    changed = [path for path, content in targets.items() if not (repo / path).exists() or (repo / path).read_bytes() != content]
    manifest = {'schema': 1, 'source': {'repository': 'product-planning-plugins', 'path': 'shared/runtime-source', 'version': VERSION, 'files': {n: hashlib.sha256((source / n).read_bytes()).hexdigest() for n in NAMES}}, 'targets': {n: hashlib.sha256(c).hexdigest() for n, c in sorted(targets.items())}}
    if not lock.exists() or json.loads(lock.read_text()) != manifest:
        changed.append('shared/runtime-manifest.json')
    if not a.check:
        for relative, content in targets.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'version': VERSION, 'changed': changed}))
    return bool(changed) if a.check else 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
