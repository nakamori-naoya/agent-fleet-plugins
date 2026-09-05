#!/usr/bin/env python3
"""Read-only CLI, publication, dependency and configuration diagnostics."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys



def reject_repository_symlinks(root):
    """Validate repository-controlled paths before any content read or subprocess."""
    if not root.is_dir():
        raise ValueError('repository is not a directory')
    for relative in ['scripts', 'plugins', '.agents', '.claude-plugin']:
        base = root / relative
        if base.is_symlink():
            raise ValueError('repository path is a symlink: ' + str(base))
        for directory, dirs, files in os.walk(base, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise ValueError('repository path is a symlink: ' + str(path))

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repository', default=str(Path(__file__).resolve().parents[1]))
    p.add_argument('--repo', default=str(Path.cwd()), help='target project whose config will be resolved')
    p.add_argument('--distribution-only', action='store_true', help='skip project/dependency resolution explicitly')
    a = p.parse_args()
    root = Path(a.repository).resolve()
    checks = []
    try:
        reject_repository_symlinks(root)
    except ValueError as exc:
        print(json.dumps({'schema': 1, 'read_only': True, 'checks': [{'check': 'repository-path-boundary', 'ok': False, 'detail': str(exc), 'remedy': '診断対象の配布treeからsymlinkを除去する'}]}, ensure_ascii=False, indent=2))
        return 1
    def run(label, argv, stdin=None):
        try:
            result = subprocess.run(argv, input=stdin, text=True, capture_output=True, timeout=60)
            ok = result.returncode == 0
            checks.append({'check': label, 'argv': argv, 'ok': ok, 'detail': result.stderr[-1000:] if not ok else '', 'remedy': '' if ok else '必要なCLIまたは宣言/設定を修正して再実行する'})
            return result if ok else None
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append({'check': label, 'argv': argv, 'ok': False, 'detail': str(exc), 'remedy': 'CLIをPATHへ導入する。yqはMike Farah v4を使う'})
    for name, argv, stdin in [('python3', ['python3', '-c', 'import sys; assert sys.version_info >= (3, 10)'], None), ('bash', ['bash', '-n', str(root / 'scripts/validate.sh')], None), ('git', ['git', '--version'], None), ('jq', ['jq', '-e', '.test == true'], '{"test":true}'), ('rg', ['rg', '--version'], None)]:
        run(name, argv, stdin)
    if any(root.glob('plugins/**/*.yml')):
        result = run('yq-v4-syntax', ['yq', '-o=json', '-I=0', '.'], 'test: true\n')
        if result:
            try:
                if json.loads(result.stdout) != {'test': True}:
                    raise ValueError('yq output mismatch')
            except ValueError:
                checks[-1].update(ok=False, detail='yq -o=json did not return the expected typed JSON', remedy='Mike Farah yq v4をPATHの先頭へ導入する')
    catalogs = [root / '.agents/plugins/marketplace.json', root / '.claude-plugin/marketplace.json']
    for runtime, catalog in zip(['codex', 'claude'], catalogs):
        try:
            entries = json.loads(catalog.read_text())['plugins']
            names = set()
            for entry in entries:
                source = entry['source']['path'] if runtime == 'codex' else entry['source']
                package = (root / source).resolve()
                if not package.is_relative_to(root):
                    raise ValueError('package escapes repository')
                manifest = json.loads((package / f'.{runtime}-plugin/plugin.json').read_text())
                declared = manifest['skills']
                if isinstance(declared, str):
                    declared = [declared]
                for relative in declared:
                    path = package / relative
                    if path.is_symlink() or not path.resolve().is_relative_to(package):
                        raise ValueError('skill path boundary')
                    files = [path / 'SKILL.md'] if (path / 'SKILL.md').exists() else list(path.glob('*/SKILL.md'))
                    if not files:
                        raise ValueError('public skill missing')
                    for skill in files:
                        text = skill.read_text()
                        header = text.split('---', 2)[1]
                        name = next(line[6:] for line in header.splitlines() if line.startswith('name: '))
                        if name in names:
                            raise ValueError('duplicate public skill: ' + name)
                        names.add(name)
            checks.append({'check': runtime + '-public-skills', 'ok': True, 'skills': sorted(names)})
        except (OSError, ValueError, KeyError, IndexError, StopIteration) as exc:
            checks.append({'check': runtime + '-public-skills', 'ok': False, 'detail': str(exc), 'remedy': '公開manifestのskillsとSKILL.mdを修復する'})
    if not a.distribution_only:
        for resolver in sorted(root.glob('plugins/**/scripts/resolve.sh')):
            result = run('resolve:' + str(resolver.relative_to(root)), ['bash', str(resolver), a.repo, '--explain'])
            if result:
                checks[-1]['resolution_sources'] = [line for line in result.stderr.splitlines() if '設定:' in line or 'scope:' in line]
    print(json.dumps({'schema': 1, 'mode': 'distribution-only' if a.distribution_only else 'full', 'read_only': True, 'checks': checks}, ensure_ascii=False, indent=2))
    return 0 if checks and all(c['ok'] for c in checks) else 1

if __name__ == '__main__':
    sys.exit(main())
