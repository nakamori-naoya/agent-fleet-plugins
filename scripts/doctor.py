#!/usr/bin/env python3
"""Read-only CLI, publication, dependency and configuration diagnostics."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile



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

def typed_yaml(path, expression):
    """設定YAMLの一部をtyped JSONで読む。読めなければNone。"""
    if not path.is_file() or path.is_symlink():
        return None
    try:
        result = subprocess.run(['yq', '-o=json', '-I=0', expression, str(path)], text=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None

def own_marketplace(root):
    """自分の repository が名乗る marketplace。requires の内外を分けるのに使う。両 catalog の name が正本。"""
    for relative in ('.claude-plugin/marketplace.json', '.agents/plugins/marketplace.json'):
        catalog = root / relative
        if not catalog.is_file() or catalog.is_symlink():
            continue
        try:
            data = json.loads(catalog.read_text())
        except (OSError, ValueError):
            continue
        name = data.get('name') if isinstance(data, dict) else None
        if isinstance(name, str) and name:
            return name
    return None

def external_contracts(plugin_root, marketplace):
    """隣接 playbook.yml が宣言する外部依存を、書かれた順で (契約ID, marketplace, plugin) として返す。"""
    declared = typed_yaml(plugin_root / 'playbook.yml', '[.requires[]? | [.marketplace, .plugin]]')
    contracts = []
    for item in declared or []:
        if not isinstance(item, list) or len(item) != 2:
            continue
        market, plugin = item
        if not isinstance(market, str) or not isinstance(plugin, str) or market == marketplace:
            continue
        contract = market + '/' + plugin
        if contract not in [c[0] for c in contracts]:
            contracts.append((contract, market, plugin))
    return contracts

def sibling_package(root, marketplace):
    """validate.shと同じ探索。実配布物は兄弟checkout ../<marketplace>-plugins/plugins/<package> にある。"""
    base = os.environ.get('HARNESS_PLUGIN_SIBLING_ROOT') or str(root.parent)
    return Path(base) / (marketplace + '-plugins') / 'plugins'

def sibling_package_root(root, marketplace, plugin):
    """兄弟 checkout の marketplace catalog から package root を引く。catalog が無ければ plugins/<plugin> を仮定する。"""
    plugins = sibling_package(root, marketplace)
    catalog = plugins.parent / '.claude-plugin/marketplace.json'
    if catalog.is_file() and not catalog.is_symlink():
        try:
            for entry in json.loads(catalog.read_text()).get('plugins', []):
                if isinstance(entry, dict) and entry.get('name') == plugin and isinstance(entry.get('source'), str):
                    return (plugins.parent / entry['source']).resolve()
        except (OSError, ValueError):
            pass
    return plugins / plugin

def entry_playbooks(root):
    """公開入口と内部 skill の隣接 playbook.yml（plugin-package-contract.md の配置）。"""
    return sorted(root.glob('plugins/*/skills/*/playbook.yml')) + sorted(root.glob('plugins/*/internal/*/playbook.yml'))

def diagnose_dependencies(root, repo, checks, run, workspace):
    """全公開入口 / 内部 skill の外部依存を、実配布物に対して resolver で解決する。

    fixtureでは代用しない。依存先は次の順で探し、どちらでも見つからなければNGにする。
      1. HARNESS_PLUGIN_REAL_ROOTS（契約ID→package rootのJSON）
      2. 兄弟checkout <repo>/../<marketplace>-plugins（親directoryは HARNESS_PLUGIN_SIBLING_ROOT で差し替えられる）
    束縛は resolver の --create-lock で dependencies.yml（personal / project / scope）から1層を選び、
    同じ run の各契約へ --bindings で渡す。"""
    marketplace = own_marketplace(root)
    resolver = root / 'shared/playbook/resolve-dependency.py'
    provided = os.environ.get('HARNESS_PLUGIN_REAL_ROOTS', '')
    runtime = os.environ.get('HARNESS_PLUGIN_RUNTIME') or 'claude'
    playbooks = entry_playbooks(root)
    wanted = {}
    for playbook in playbooks:
        for contract, market, plugin in external_contracts(playbook.parent, marketplace):
            wanted.setdefault(contract, sibling_package_root(root, market, plugin))
    missing = {contract: path for contract, path in wanted.items() if not path.is_dir()}
    dev_map = provided
    if not provided and wanted and not missing:
        # 一時のdev-mapは診断のためだけに作る。repositoryには何も書かない。
        path = workspace / 'real-roots.json'
        path.write_text(json.dumps({'schema': 1, 'dependencies': {contract: str(package.resolve()) for contract, package in sorted(wanted.items())}}, ensure_ascii=False) + '\n')
        dev_map = str(path)
    bindings = {'check': 'dependency-bindings', 'ok': True, 'runtime': runtime, 'entries': [], 'unused_bindings': [], 'remedy': ''}
    if wanted:
        bindings['real_distribution'] = {'source': 'HARNESS_PLUGIN_REAL_ROOTS', 'path': provided} if provided else {'source': 'sibling-checkout', 'dependencies': {contract: str(package) for contract, package in sorted(wanted.items())}}
        if not resolver.is_file() or resolver.is_symlink():
            checks.append({'check': 'dependency-resolver', 'ok': False, 'detail': 'shared/playbook/resolve-dependency.py が無い', 'remedy': 'sync-runtime.py で保守用 tool を配る'})
            bindings['ok'] = False
    used = set()
    declared = set()
    for index, playbook in enumerate(playbooks):
        entry = playbook.parent
        label = 'resolve:' + str(entry.relative_to(root))
        contracts = external_contracts(entry, marketplace)
        if not contracts:
            continue
        absent = [contract for contract, _, _ in contracts if contract in missing]
        if absent:
            checks.append({'check': label, 'ok': False, 'detail': '実配布物が見つからない: ' + ', '.join(contract + '(' + str(missing[contract]) + ')' for contract in absent), 'remedy': '兄弟checkoutを置くか、契約ID→package rootのJSONをHARNESS_PLUGIN_REAL_ROOTSで渡す'})
            bindings['ok'] = False
            continue
        if not resolver.is_file():
            continue
        environment = dict(os.environ, HARNESS_PLUGIN_RUNTIME=runtime)
        if dev_map:
            environment['HARNESS_PLUGIN_DEV_ROOTS'] = dev_map
        lock = workspace / ('bindings-%d.lock' % index)
        scope = str(Path(repo) / '.harness-plugins/scopes' / entry.name)
        created = run(label + ':bindings', ['python3', str(resolver), '--create-lock', '--entry', entry.name, '--repo-root', repo, '--scope-root', scope, '--lock', str(lock)], env=environment)
        if not created:
            bindings['ok'] = False
            continue
        snapshot = json.loads(lock.read_text())
        record = {'playbook': str(playbook.relative_to(root)), 'layer': snapshot.get('bindings_layer'), 'file': snapshot.get('bindings_file'), 'lock': str(lock), 'resolved': []}
        declared |= set(snapshot.get('bindings') or {})
        deps = {}
        for contract, market, plugin in contracts:
            result = run(label + ':' + contract, ['python3', str(resolver), '--plugin-root', str(entry), '--plugin', plugin, '--marketplace', market, '--contract', contract, '--bindings', str(lock)], env=environment)
            if not result:
                bindings['ok'] = False
                continue
            candidate = json.loads(result.stdout)
            deps[plugin] = candidate
            record['resolved'].append(contract + ' → ' + candidate['marketplace'] + '/' + candidate['plugin'] + ' ' + candidate['version'] + ' [' + candidate['source_kind'] + ']: ' + candidate['root'])
            used.add(contract)
        steps = typed_yaml(playbook, '.')
        if deps and isinstance(steps, dict):
            config = json.dumps({'deps': deps, 'playbook_root': str(entry), 'playbook': steps}, ensure_ascii=False)
            if not run(label + ':steps', ['python3', str(resolver), '--check-steps'], stdin=config, env=environment):
                bindings['ok'] = False
        bindings['entries'].append(record)
    # 未使用束縛は resolve では警告しない。全依存を辿れる doctor だけが報告する。
    bindings['unused_bindings'] = sorted(declared - used)
    if bindings['unused_bindings']:
        bindings['remedy'] = 'dependencies.ymlに、どの実行でも使われていない束縛がある'
    checks.append(bindings)

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
    def run(label, argv, stdin=None, env=None):
        try:
            result = subprocess.run(argv, input=stdin, text=True, capture_output=True, timeout=60, env=env)
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
                if not isinstance(declared, list):
                    raise ValueError('skills must be a list of ./skills/<entry>')
                for relative in declared:
                    path = package / relative
                    if path.is_symlink() or not path.resolve().is_relative_to(package):
                        raise ValueError('skill path boundary')
                    files = [path / 'SKILL.md']
                    if not files[0].is_file():
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
        with tempfile.TemporaryDirectory(prefix='harness-doctor-') as workspace:
            diagnose_dependencies(root, a.repo, checks, run, Path(workspace).resolve())
    print(json.dumps({'schema': 1, 'mode': 'distribution-only' if a.distribution_only else 'full', 'read_only': True, 'checks': checks}, ensure_ascii=False, indent=2))
    return 0 if checks and all(c['ok'] for c in checks) else 1

if __name__ == '__main__':
    sys.exit(main())
