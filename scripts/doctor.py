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

def required_overrides(plugin_root):
    """既定値を持たないrequiredなprompt parameterを、dotted名で返す。

    依頼のたびに変わる値は同梱既定に実値を置かない規約なので、上書きが無ければ
    resolve.shは必ず落ちる。これは配布物の不具合ではなく「利用者の指定が要る」状態である。"""
    tree = typed_yaml(plugin_root / 'config/defaults.yml', '.prompt_parameters // {}')
    names = []
    def walk(node, path):
        if not isinstance(node, dict):
            return
        if any(key in node for key in ('type', 'enum', 'required', 'default')):
            if node.get('required') is True and 'default' not in node:
                names.append('.'.join(path))
            return
        for key in sorted(node):
            walk(node[key], path + [key])
    walk(tree if isinstance(tree, dict) else {}, [])
    return names

def own_marketplace(root):
    """自分のpackageが名乗るmarketplace。requiresの内外を分けるのに使う。"""
    for runtime in ('claude', 'codex'):
        manifest = root / ('plugins/.' + runtime + '-plugin/plugin.json')
        if not manifest.is_file() or manifest.is_symlink():
            continue
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        name = ((data.get('metadata') or {}).get('harness') or {}).get('marketplace')
        if isinstance(name, str) and name:
            return name
    return None

def external_contracts(plugin_root, marketplace):
    """同梱playbook.ymlが宣言する外部依存の契約IDを、書かれた順で返す。"""
    declared = typed_yaml(plugin_root / 'playbook.yml', '[.requires[]? | [.marketplace, .plugin]]')
    contracts = []
    for item in declared or []:
        if not isinstance(item, list) or len(item) != 2:
            continue
        market, plugin = item
        if not isinstance(market, str) or not isinstance(plugin, str) or market == marketplace:
            continue
        if market + '/' + plugin not in contracts:
            contracts.append(market + '/' + plugin)
    return contracts

def sibling_package(root, marketplace):
    """validate.shと同じ探索。実配布物は兄弟checkout ../<marketplace>-plugins/plugins にある。"""
    base = os.environ.get('HARNESS_PLUGIN_SIBLING_ROOT') or str(root.parent)
    return Path(base) / (marketplace + '-plugins') / 'plugins'

def diagnose_dependencies(root, repo, checks, run, workspace):
    """全playbook / skillの解決を、実配布物に対して確かめる。

    fixtureでは代用しない。依存先は次の順で探し、どちらでも見つからなければNGにする。
      1. HARNESS_PLUGIN_REAL_ROOTS（契約ID→package rootのJSON）
      2. 兄弟checkout <repo>/../<marketplace>-plugins/plugins
         （親directoryは HARNESS_PLUGIN_SIBLING_ROOT で差し替えられる）"""
    marketplace = own_marketplace(root)
    provided = os.environ.get('HARNESS_PLUGIN_REAL_ROOTS', '')
    runtime = os.environ.get('HARNESS_PLUGIN_RUNTIME') or 'claude'
    resolvers = sorted(root.glob('plugins/**/scripts/resolve.sh'))
    wanted = {}
    for resolver in resolvers:
        for contract in external_contracts(resolver.parent.parent, marketplace):
            wanted.setdefault(contract, sibling_package(root, contract.split('/', 1)[0]))
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
    used = set()
    declared = set()
    for resolver in resolvers:
        label = 'resolve:' + str(resolver.relative_to(root))
        plugin_root = resolver.parent.parent
        overrides = required_overrides(plugin_root)
        if overrides:
            # 実行しない。落ちるのが正しい配布物を、落ちたからNGとは呼ばない。
            checks.append({'check': label, 'ok': True, 'skipped': 'requires-override', 'requires_override': overrides, 'remedy': '単体で解決するときは ' + ' '.join('--override=' + name + '=<値>' for name in overrides) + ' を渡す'})
            continue
        contracts = external_contracts(plugin_root, marketplace)
        absent = [contract for contract in contracts if contract in missing]
        if absent:
            checks.append({'check': label, 'ok': False, 'detail': '実配布物が見つからない: ' + ', '.join(contract + '(' + str(missing[contract]) + ')' for contract in absent), 'remedy': '兄弟checkoutを置くか、契約ID→package rootのJSONをHARNESS_PLUGIN_REAL_ROOTSで渡す'})
            bindings['ok'] = False
            continue
        environment = dict(os.environ, HARNESS_PLUGIN_RUNTIME=runtime)
        if contracts and dev_map:
            environment['HARNESS_PLUGIN_DEV_ROOTS'] = dev_map
        result = run(label, ['bash', str(resolver), repo, '--explain'], env=environment)
        if not result:
            bindings['ok'] = False
            continue
        lines = result.stderr.splitlines()
        # 「依存:」節と「束縛:」節を捨てない。分類と束縛は診断の主目的である。
        checks[-1]['resolution_sources'] = [line for line in lines if '設定:' in line or 'scope:' in line]
        checks[-1]['dependencies'] = [line.strip() for line in lines if line.startswith('  [外部]') or line.startswith('  [内部]')]
        checks[-1]['bindings'] = [line.strip() for line in lines if line.startswith('# 束縛:') or line.strip().startswith('lock:') or ' → 採用 ' in line]
        entry = {'playbook': str(resolver.relative_to(root)), 'layer': None, 'file': None, 'lock': None, 'resolved': []}
        for line in lines:
            if line.startswith('# 束縛: '):
                layer, _, origin = line[len('# 束縛: '):].partition(' (')
                entry['layer'] = layer.strip()
                entry['file'] = origin.rstrip(')').strip() or None
            elif line.strip().startswith('lock: '):
                entry['lock'] = line.strip()[len('lock: '):]
            elif ' → 採用 ' in line:
                contract = line.strip().split(' : ', 1)[0].strip()
                entry['resolved'].append(line.strip())
                used.add(contract)
        if entry['file'] and entry['file'] != '無し':
            try:
                catalog = subprocess.run(['yq', '-o=json', '-I=0', '.bindings // {}', entry['file']], text=True, capture_output=True, timeout=30)
                if not catalog.returncode:
                    declared |= set(json.loads(catalog.stdout))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        bindings['entries'].append(entry)
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
        with tempfile.TemporaryDirectory(prefix='harness-doctor-') as workspace:
            diagnose_dependencies(root, a.repo, checks, run, Path(workspace))
    print(json.dumps({'schema': 1, 'mode': 'distribution-only' if a.distribution_only else 'full', 'read_only': True, 'checks': checks}, ensure_ascii=False, indent=2))
    return 0 if checks and all(c['ok'] for c in checks) else 1

if __name__ == '__main__':
    sys.exit(main())
