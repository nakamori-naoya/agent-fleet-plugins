#!/usr/bin/env python3
"""Development-only generation; runtime copies remain self-contained."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

VERSION = '2.0.0'
NAMES = ['resolve-dependency.py', 'resolve.sh', 'validate-distribution.py', 'prepare.sh', 'run-config.py', 'doctor.py', 'release.py', 'evaluate-skills.py', 'claude-eval-adapter.py', 'sync-runtime.py', 'test-hardening.py', 'validate.yml']



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
        expected = {'scripts/' + n for n in ['doctor.py', 'release.py', 'evaluate-skills.py', 'claude-eval-adapter.py', 'sync-runtime.py', 'test-hardening.py']}
        expected.add('.github/workflows/validate.yml')
        for f in repo.glob('plugins/**/playbook.yml'):
            expected.update(str((f.parent / 'scripts' / n).relative_to(repo)) for n in ['resolve.sh', 'resolve-dependency.py'])
        for f in repo.glob('plugins/**/scripts/prepare.sh'):
            if 'run-config.py' in f.read_text() or '共通入口' in f.read_text():
                expected.update(str((f.parent / n).relative_to(repo)) for n in ['prepare.sh', 'run-config.py'])
        if (repo / 'shared/playbook').is_dir():
            expected.update('shared/playbook/' + n for n in ['resolve.sh', 'resolve-dependency.py'])
        if (repo / 'shared/prepare.sh').exists():
            expected.update(['shared/prepare.sh', 'shared/run-config.py'])
        if (repo / 'plugins/.codex-plugin/plugin.json').exists():
            expected.add('scripts/validate-distribution.py')
        if set(data.get('targets', {})) != expected:
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
    for name in NAMES:
        if name == 'resolve-dependency.py':
            paths = list(repo.glob('plugins/**/scripts/resolve-dependency.py')) + ([repo / 'shared/playbook' / name] if (repo / 'shared/playbook').is_dir() else [])
        elif name == 'resolve.sh':
            paths = [f.parent / 'scripts/resolve.sh' for f in repo.glob('plugins/**/playbook.yml')] + ([repo / 'shared/playbook' / name] if (repo / 'shared/playbook').is_dir() else [])
        elif name == 'validate-distribution.py':
            paths = [repo / 'scripts' / name] if (repo / 'plugins/.codex-plugin/plugin.json').exists() else []
        elif name == 'prepare.sh':
            paths = [f for f in repo.glob('plugins/**/scripts/prepare.sh') if 'config resolution' in f.read_text() or '共通入口' in f.read_text()] + ([repo / 'shared/prepare.sh'] if (repo / 'shared/prepare.sh').exists() else [])
        elif name == 'validate.yml':
            paths = [repo / '.github/workflows/validate.yml']
        elif name in {'doctor.py', 'release.py', 'evaluate-skills.py', 'claude-eval-adapter.py', 'sync-runtime.py', 'test-hardening.py'}:
            paths = [repo / 'scripts' / name]
        else:
            paths = [f.parent / name for f in repo.glob('plugins/**/scripts/prepare.sh') if 'run-config.py' in f.read_text() or '共通入口' in f.read_text()]
            if (repo / 'shared/prepare.sh').exists():
                paths.append(repo / 'shared' / name)
        for path in paths:
            targets[str(path.relative_to(repo))] = (source / name).read_bytes()
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
