#!/usr/bin/env python3
"""Observable regression checks for publication, dependency resolution and maintenance CLIs.

fixture は plugin-package-contract.md の配置（plugins/<package>/skills/<entry>、internal/<name>、
package root だけの manifest、公開 playbook の CONTRACT.md）で作る。
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT/'shared/playbook/resolve-dependency.py'

class Hardening(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hardening-')
        self.base = Path(self.temp.name).resolve()
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.base/'state'), XDG_CONFIG_HOME=str(self.base/'config'), HARNESS_PLUGIN_RUNTIME='codex')
        for name in ('HARNESS_PLUGIN_DEV_ROOTS', 'HARNESS_PLUGIN_REAL_ROOTS', 'HARNESS_PLUGIN_SIBLING_ROOT'):
            self.env.pop(name, None)
    def tearDown(self):
        self.temp.cleanup()
    def call(self, *args, input=None, env=None):
        return subprocess.run(list(map(str,args)), input=input, text=True, capture_output=True, env=env or self.env, cwd=self.base, timeout=60)

    # ---- fixture（新配置） ----
    def _manifest(self, root, data):
        for runtime in ('claude', 'codex'):
            path = root / f'.{runtime}-plugin/plugin.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))

    def _catalog(self, repo, marketplace, package_name, version='1.0.0'):
        source = f'./plugins/{package_name}'
        (repo/'.claude-plugin').mkdir(parents=True, exist_ok=True)
        (repo/'.agents/plugins').mkdir(parents=True, exist_ok=True)
        (repo/'.claude-plugin/marketplace.json').write_text(json.dumps(
            {'name': marketplace, 'plugins': [{'name': package_name, 'version': version, 'source': source}]}))
        (repo/'.agents/plugins/marketplace.json').write_text(json.dumps(
            {'name': marketplace, 'plugins': [{'name': package_name, 'version': version, 'source': {'source': 'local', 'path': source}}]}))

    def _skill(self, root, name):
        root.mkdir(parents=True, exist_ok=True)
        (root/'SKILL.md').write_text(f'---\nname: {name}\ndescription: fixture\n---\nfixture\n')

    def _public_playbook(self, entry, name, steps, requires=''):
        self._skill(entry, name)
        (entry/'CONTRACT.md').write_text(f'# {name} 公開契約\n\n入力と出力。\n')
        (entry/'playbook.yml').write_text(
            f'version: 2\nname: {name}\ndescription: fixture\n'
            f'requires: [{requires}]\nsteps: [{steps}]\n')

    def _provider(self, name, package_name, marketplace, contract, internal, implements=True,
                  types=None, decoy=False, contract_version=None, version='1.0.0'):
        """公開 playbook `pb` と内部 skill `<internal>` を持つ provider package。"""
        if contract_version is None:
            contract_version = 2 if contract == 'write-doc/write-doc' else 1
        repo = self.base/name
        package = repo/'plugins'/package_name
        self._public_playbook(package/'skills/pb', 'pb', f'{{id: work, skill: {internal}, purpose: fixture}}')
        self._skill(package/'internal'/internal, internal)
        harness = {'marketplace': marketplace, 'contractVersion': contract_version,
                   'playbooks': {'pb': './skills/pb'},
                   'internalPlugins': {internal: f'./internal/{internal}'}}
        skills = ['./skills/pb']
        declarations = []
        if implements:
            declaration = {'id': contract, 'version': contract_version, 'kind': 'playbook', 'playbook': 'pb'}
            if types is not None:
                declaration['types'] = types
            declarations.append(declaration)
        if decoy:
            # 契約IDが違う別の公開 playbook。契約が選んだ入口（pb）が勝たねばならない。
            self._public_playbook(package/'skills/decoy', 'decoy', '{id: work, agent_work: invoking_agent, purpose: fixture}')
            harness['playbooks']['decoy'] = './skills/decoy'
            skills.append('./skills/decoy')
            declarations.append({'id': f'{marketplace}/decoy', 'version': contract_version, 'kind': 'playbook', 'playbook': 'decoy'})
        if declarations:
            harness['implements'] = declarations
        self._manifest(package, {'name': package_name, 'version': version, 'skills': skills, 'metadata': {'harness': harness}})
        self._catalog(repo, marketplace, package_name, version)
        return package

    def _direct_provider(self, name='direct-provider', contract='write-doc/write-doc',
                         contract_version=2, plugin='write-doc', marketplace='write-doc'):
        """内部 skill を持たない公開 playbook 1本の provider。"""
        repo = self.base/name
        package = repo/'plugins'/plugin
        self._public_playbook(package/'skills'/plugin, plugin, '{id: author, agent_work: invoking_agent, purpose: fixture}')
        declaration = {'id': contract, 'version': contract_version, 'kind': 'playbook', 'playbook': plugin}
        if contract == 'write-doc/write-doc':
            declaration['types'] = ['north-star']
        harness = {'marketplace': marketplace, 'contractVersion': contract_version,
                   'playbooks': {plugin: f'./skills/{plugin}'}, 'implements': [declaration]}
        self._manifest(package, {'name': plugin, 'version': '7.0.0', 'skills': [f'./skills/{plugin}'], 'metadata': {'harness': harness}})
        self._catalog(repo, marketplace, plugin, '7.0.0')
        return package

    def _consumer(self, steps, requires, name='consumer'):
        """公開入口 `demo` と内部 skill `local-tool` を持つ consumer package。requires は外部だけ。"""
        repo = self.base/name
        package = repo/'plugins/demo-pack'
        entry = package/'skills/demo'
        self._skill(entry, 'demo')
        (entry/'playbook.yml').write_text(
            'version: 2\nname: demo\ndescription: fixture\ndocument_type: north-star\n'
            'requires:\n' + ''.join(f'  - {{plugin: {p}, marketplace: {m}}}\n' for p, m in requires) +
            'steps:\n' + steps)
        self._skill(package/'internal/local-tool', 'local-tool')
        self._manifest(package, {'name': 'demo-pack', 'version': '1.0.0', 'skills': ['./skills/demo'],
                                 'metadata': {'harness': {'marketplace': 'demo', 'contractVersion': 1,
                                                          'internalPlugins': {'local-tool': './internal/local-tool'}}}})
        self._catalog(repo, 'demo', 'demo-pack')
        (repo/'scripts').mkdir(exist_ok=True)
        (repo/'scripts/validate.sh').write_text('#!/bin/bash\nexit 0\n')
        return entry

    def _devmap(self, mapping):
        path = self.base/'devmap.json'
        path.write_text(json.dumps({'schema': 1, 'dependencies': {k: str(v) for k, v in mapping.items()}}))
        return path

    def _resolve(self, entry, plugin, marketplace, env, *extra):
        return subprocess.run(['python3', str(RESOLVER), '--plugin-root', str(entry), '--plugin', plugin,
                               '--marketplace', marketplace, *extra],
                              text=True, capture_output=True, env=env, timeout=60)

    def _check_steps(self, entry, requires, env):
        """resolver で外部依存を解き、隣接 playbook.yml の steps を --check-steps へ渡す。"""
        deps = {}
        for plugin, marketplace in requires:
            resolved = self._resolve(entry, plugin, marketplace, env)
            if resolved.returncode:
                return resolved
            deps[plugin] = json.loads(resolved.stdout)
        playbook = json.loads(subprocess.run(['yq', '-o=json', '-I=0', '.', str(entry/'playbook.yml')],
                                             text=True, capture_output=True, timeout=30, check=True).stdout)
        config = json.dumps({'deps': deps, 'playbook_root': str(entry), 'playbook': playbook})
        return subprocess.run(['python3', str(RESOLVER), '--check-steps'], input=config,
                              text=True, capture_output=True, env=env, timeout=60)

    # ---- 保守 CLI ----
    def test_sync_rejects_source_and_target_symlinks_without_external_writes(self):
        source = self.base/'source';source.mkdir()
        manifest = json.loads((ROOT/'shared/runtime-manifest.json').read_text())
        for name in manifest['source']['files']:
            (source/name).write_text('fixture source ' + name)
        repo = self.base/'repo';(repo/'scripts').mkdir(parents=True)
        outside = self.base/'outside';outside.write_text('preserve')
        link = repo/'scripts/doctor.py';link.symlink_to(outside)
        result = self.call('python3',ROOT/'scripts/sync-runtime.py','--repo',repo,'--source',source)
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        self.assertEqual(outside.read_text(),'preserve')
        self.assertEqual(list((repo/'scripts').iterdir()),[link])
        link.unlink()
        (source/'doctor.py').unlink();(source/'doctor.py').symlink_to(outside)
        result = self.call('python3',ROOT/'scripts/sync-runtime.py','--repo',repo,'--source',source)
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        self.assertEqual(outside.read_text(),'preserve')
        self.assertEqual(list((repo/'scripts').iterdir()),[])
    def test_sync_does_not_write_into_distributed_packages(self):
        source = self.base/'source';source.mkdir()
        manifest = json.loads((ROOT/'shared/runtime-manifest.json').read_text())
        for name in manifest['source']['files']:
            (source/name).write_text('fixture source ' + name)
        repo = self.base/'repo';(repo/'scripts').mkdir(parents=True);(repo/'shared/playbook').mkdir(parents=True)
        entry = repo/'plugins/pkg/skills/alpha';entry.mkdir(parents=True)
        (entry/'playbook.yml').write_text('version: 2\nname: alpha\nsteps: [{id: a, agent_work: invoking_agent, purpose: x}]\n')
        result = self.call('python3',ROOT/'scripts/sync-runtime.py','--repo',repo,'--source',source)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        targets = json.loads((repo/'shared/runtime-manifest.json').read_text())['targets']
        self.assertFalse(any(name.startswith('plugins/') for name in targets), targets)
        self.assertEqual(sorted(p.name for p in entry.iterdir()), ['playbook.yml'])
        self.assertIn('shared/playbook/resolve-dependency.py', targets)
        self.assertIn('scripts/validate-distribution.py', targets)
        self.assertEqual(self.call('python3',ROOT/'scripts/sync-runtime.py','--repo',repo,'--check').returncode,0)
    def test_sync_keeps_a_repository_owned_distribution_validator(self):
        source = self.base/'source';source.mkdir()
        manifest = json.loads((ROOT/'shared/runtime-manifest.json').read_text())
        for name in manifest['source']['files']:
            (source/name).write_text('fixture source ' + name)
        repo = self.base/'repo';(repo/'scripts').mkdir(parents=True)
        own = repo/'scripts/validate-distribution.py';own.write_text('# repository-owned validator\n')
        result = self.call('python3',ROOT/'scripts/sync-runtime.py','--repo',repo,'--source',source)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(own.read_text(),'# repository-owned validator\n')
        self.assertNotIn('scripts/validate-distribution.py', json.loads((repo/'shared/runtime-manifest.json').read_text())['targets'])
    def test_doctor_rejects_external_skill_and_symlinked_script_before_access(self):
        repo=self.base/'repo';package=repo/'plugins/p';(repo/'scripts').mkdir(parents=True)
        (repo/'scripts/validate.sh').write_text('#!/bin/bash\nexit 0\n');(package/'skills/skill').mkdir(parents=True)
        external=self.base/'outside.md';external.write_text('---\nname: external-private-name\ndescription: fixture\n---\n')
        skill=package/'skills/skill/SKILL.md';skill.symlink_to(external)
        self._manifest(package,{'name':'p','version':'1.0.0','skills':['./skills/skill'],'metadata':{'harness':{'marketplace':'p','contractVersion':1}}})
        self._catalog(repo,'p','p')
        result=self.call('python3',ROOT/'scripts/doctor.py','--repository',repo,'--distribution-only')
        self.assertEqual(result.returncode,1)
        self.assertNotIn('external-private-name',result.stdout)
        self.assertIn('repository-path-boundary',result.stdout)
        skill.unlink();skill.write_text('---\nname: skill\ndescription: fixture\n---\n')
        (package/'skills/skill/scripts').mkdir()
        marker=self.base/'executed';script=self.base/'outside.sh';script.write_text('#!/bin/bash\ntouch "'+str(marker)+'"\n')
        (package/'skills/skill/scripts/tool.sh').symlink_to(script)
        result=self.call('python3',ROOT/'scripts/doctor.py','--repository',repo,'--repo',self.base)
        self.assertEqual(result.returncode,1)
        self.assertFalse(marker.exists())
        self.assertEqual(len(json.loads(result.stdout)['checks']),1)
    def test_functional_ci_actions_are_pinned(self):
        workflow = (ROOT/'.github/workflows/validate.yml').read_text()
        actions = re.findall(r'uses:\s+([^\s#]+)', workflow)
        self.assertGreaterEqual(len(actions), 3)
        for action in actions:
            self.assertRegex(action, r'^[A-Za-z0-9_-]+/[A-Za-z0-9_-]+@[0-9a-f]{40}$')
    def test_public_entries_are_unique_and_declared(self):
        catalog=json.loads((ROOT/'.agents/plugins/marketplace.json').read_text())
        for entry in catalog['plugins']:
            package=ROOT/entry['source']['path'];manifest=json.loads((package/'.codex-plugin/plugin.json').read_text())
            paths=manifest['skills'];self.assertIsInstance(paths,list)
            names=[]
            for relative in paths:
                skill=package/relative/'SKILL.md'
                self.assertTrue(skill.is_file(),skill)
                header=skill.read_text().split('---',2)[1]
                names.append(next(line[6:] for line in header.splitlines() if line.startswith('name: ')))
            self.assertEqual(len(names),len(set(names)))
            all_names=[]
            for skill in package.rglob('SKILL.md'):
                header=skill.read_text().split('---',2)[1]
                all_names += [line[6:] for line in header.splitlines() if line.startswith('name: ')]
            self.assertEqual(len(all_names),len(set(all_names)))
    def test_generated_manifest_tampering_is_rejected(self):
        lock=ROOT/'shared/runtime-manifest.json'
        if not lock.exists():self.skipTest('no generated runtime')
        fixture=self.base/'copy';shutil.copytree(ROOT,fixture,ignore=shutil.ignore_patterns('.git','__pycache__'))
        r=self.call('python3',fixture/'scripts/sync-runtime.py','--check')
        self.assertEqual(r.returncode,0,r.stderr+r.stdout)
        path=fixture/'shared/runtime-manifest.json';data=json.loads(path.read_text());data['targets']={};data['source']['version']='0.0.0';path.write_text(json.dumps(data))
        self.assertNotEqual(self.call('python3',fixture/'scripts/sync-runtime.py','--check').returncode,0)
    def test_doctor_does_not_mutate_repository(self):
        before={str(p.relative_to(ROOT)):p.stat().st_mtime_ns for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts}
        r=self.call('python3',ROOT/'scripts/doctor.py','--distribution-only')
        self.assertEqual(r.returncode,0,r.stderr+r.stdout)
        self.assertTrue(json.loads(r.stdout)['read_only'])
        after={str(p.relative_to(ROOT)):p.stat().st_mtime_ns for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts}
        self.assertEqual(before,after)
    def test_release_updates_all_matching_declarations_only(self):
        repo=self.base/'release';(repo/'scripts').mkdir(parents=True);(repo/'plugins/p/.codex-plugin').mkdir(parents=True);(repo/'plugins/p/.claude-plugin').mkdir();(repo/'.agents/plugins').mkdir(parents=True);(repo/'.claude-plugin').mkdir()
        for runtime in ['codex','claude']:(repo/f'plugins/p/.{runtime}-plugin/plugin.json').write_text(json.dumps({'name':'fixture','version':'1.0.0','requires':[{'plugin':'other','marketplace':'other'}]}))
        for p in ['.agents/plugins/marketplace.json','.claude-plugin/marketplace.json']:(repo/p).write_text(json.dumps({'plugins':[{'name':'fixture','version':'1.0.0','source':{'source':'local','path':'./plugins/p'} if p.startswith('.agents') else './plugins/p'}]}))
        sidecar=repo/'plugins/p/internal/sidecar'
        for runtime in ['codex','claude']:
            path=sidecar/f'.{runtime}-plugin/plugin.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'name':'fixture','version':'0.4.0','hooks':'./hooks/x.json','description':'same name hook sidecar'}))
        sidecar_before={str(path):path.read_bytes() for path in sidecar.rglob('plugin.json')}
        checks=self.base/'checks.json';checks.write_text('{"codex":"unverified","claude":"unverified"}')
        r=self.call('python3',ROOT/'scripts/release.py','--repo',repo,'--plugin','fixture','--version','2.0.0','--notes','change','--breaking','yes','--migration','new run','--checks',checks,'--apply')
        self.assertEqual(r.returncode,0,r.stderr)
        for runtime in ['codex','claude']:
            path=repo/f'plugins/p/.{runtime}-plugin/plugin.json'
            d=json.loads(path.read_text());self.assertEqual(d['version'],'2.0.0');self.assertEqual(d['requires'],[{'plugin':'other','marketplace':'other'}])
        self.assertEqual(sidecar_before,{str(path):path.read_bytes() for path in sidecar.rglob('plugin.json')})
        self.assertTrue((repo/'releases/fixture-2.0.0.json').exists())
        missing=repo/'plugins/p/.claude-plugin/plugin.json';missing.unlink()
        before={str(p):p.read_bytes() for p in repo.rglob('*.json')}
        r=self.call('python3',ROOT/'scripts/release.py','--repo',repo,'--plugin','fixture','--version','3.0.0','--notes','change','--breaking','yes','--migration','new run','--checks',checks,'--apply')
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(before,{str(p):p.read_bytes() for p in repo.rglob('*.json')})
        for version in ['1.0.0-alpha..1','1.0.0-.','1.0.0-01']:
            result=self.call('python3',ROOT/'scripts/release.py','--repo',repo,'--plugin','fixture','--version',version,'--notes','x','--breaking','x','--migration','x','--checks',checks)
            self.assertEqual(result.returncode,2)
            self.assertIn('prerelease' if version.endswith('-01') else 'semantic version',result.stderr)
    def test_all_templates_resolve_from_plugin_root(self):
        plugin=ROOT/'plugins/write-doc/skills/write-doc'
        if not (plugin/'assets/template-examples.yml').exists():self.skipTest('no write-doc templates')
        result=self.call('yq','-o=json','.',plugin/'assets/template-examples.yml');pairs=json.loads(result.stdout)['pairs']
        manifest=json.loads((ROOT/'plugins/write-doc/.claude-plugin/plugin.json').read_text());types=[t for i in manifest['metadata']['harness']['implements'] for t in i.get('types',[])]
        self.assertEqual(sorted(pairs),sorted(types))
        for pair in pairs.values():
            for path in pair.values():self.assertTrue((plugin/path).is_file(),path)
    def test_semantic_runner_records_assessment_but_only_operational_errors_fail(self):
        repo=self.base/'eval';(repo/'evals').mkdir(parents=True)
        (repo/'SKILL.md').write_text('---\nname: fixture\ndescription: fixture\n---\nDo the specified job.')
        fixtures=repo/'evals/cases.json';fixtures.write_text(json.dumps({'cases':[{'id':'case','skill':'../SKILL.md','messages':[{'role':'user','content':'request'}],'criteria':[{'id':'meaning','meaning':'a meaningful explanation'}]}]}))
        adapter=repo/'adapter.py';adapter.write_text("import json,sys\nr=json.load(sys.stdin)\nmode=r.get('settings',{}).get('mode')\nif mode=='adapter-error': raise SystemExit(7)\nif 'candidate_output' in r:\n print(json.dumps({'model':r['model'],'output':[] if mode=='invalid-response' else {'criteria':[{'id':'meaning','pass':mode!='semantic-fail','quote':7 if mode=='invalid-type' else ('absent' if mode=='invalid-evidence' else 'actual answer'),'reason':'independent assessment'}]}}))\nelse: print(json.dumps({'model':r['model'],'output':'actual answer'}))\n")
        command=json.dumps(['python3',str(adapter)]);out=repo/'result.json'
        common=['python3',ROOT/'scripts/evaluate-skills.py','--fixtures',fixtures,'--model-command',command,'--judge-command',command,'--model','generator','--judge-model','judge','--output',out]
        result=self.call(*common,'--settings',json.dumps({'mode':'semantic-fail'}))
        self.assertEqual(result.returncode,0);report=json.loads(out.read_text());record=report['records'][0];self.assertEqual(report['schema'],2);self.assertEqual(record['status'],'recorded');self.assertFalse(record['judgment']['output']['criteria'][0]['pass'])
        result=self.call(*common,'--settings',json.dumps({'mode':'invalid-evidence'}))
        self.assertEqual(result.returncode,1);record=json.loads(out.read_text())['records'][0];self.assertEqual(record['status'],'error');self.assertIn('evidence',record['error'])
        for mode in ('invalid-type','invalid-response'):
            result=self.call(*common,'--settings',json.dumps({'mode':mode}))
            self.assertEqual(result.returncode,1);record=json.loads(out.read_text())['records'][0];self.assertEqual(record['status'],'error')
        result=self.call(*common,'--settings',json.dumps({'mode':'adapter-error'}))
        self.assertEqual(result.returncode,1);record=json.loads(out.read_text())['records'][0];self.assertEqual(record['status'],'error');self.assertIn('adapter failed',record['error'])

    # ---- resolver の境界（symlink・改変・削除） ----
    def test_conditional_path_and_symlink_rejected(self):
        if not RESOLVER.exists():self.skipTest('no playbook resolver')
        config={'deps':{},'playbook_root':str(self.base),'playbook':{'steps':[{'id':'later','when':'optional','script':'../escape'}]}}
        r=self.call('python3',RESOLVER,'--check-steps',input=json.dumps(config));self.assertEqual(r.returncode,2);self.assertIn('path-format',r.stderr)
        target=self.base/'good';target.write_text('content');(self.base/'link').symlink_to(target)
        config['playbook']['steps'][0]['script']='link'
        self.assertEqual(self.call('python3',RESOLVER,'--check-steps','later',input=json.dumps(config)).returncode,2)
    def _load_resolver(self, label):
        sys.dont_write_bytecode=True
        spec=importlib.util.spec_from_file_location(label,RESOLVER);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module
    def test_dependency_mutation_and_contract_mismatch(self):
        if not RESOLVER.exists():self.skipTest('no dependency resolver')
        module=self._load_resolver('dependency')
        package=self.base/'dependency';(package/'.codex-plugin').mkdir(parents=True)
        manifest=package/'.codex-plugin/plugin.json';manifest.write_text(json.dumps({'name':'fixture','version':'1.0.0','metadata':{'harness':{'contractVersion':1}}}))
        skill=package/'SKILL.md';skill.write_text('---\nname: fixture\ndescription: fixture\n---\noriginal')
        dep=module.validate_candidate(package,'codex','fixture','fixture')
        config={'deps':{'fixture':dep},'playbook_root':str(self.base),'playbook':{'steps':[{'id':'invoke','skill':'fixture'}]}}
        self.assertEqual(self.call('python3',RESOLVER,'--check-steps',input=json.dumps(config)).returncode,0)
        skill.write_text(skill.read_text()+' changed')
        result=self.call('python3',RESOLVER,'--check-steps',input=json.dumps(config));self.assertEqual(result.returncode,2);self.assertIn('content-hash',result.stderr)
        manifest.write_text(json.dumps({'name':'fixture','version':'1.0.0','metadata':{'harness':{'contractVersion':999}}}))
        with self.assertRaises(SystemExit):module.validate_candidate(package,'codex','fixture','fixture')
    def test_dependency_manifest_rejected_before_external_json_read(self):
        if not RESOLVER.exists():self.skipTest('no dependency resolver')
        module=self._load_resolver('manifest_boundary')
        package=self.base/'dependency';(package/'.codex-plugin').mkdir(parents=True)
        manifest=package/'.codex-plugin/plugin.json';manifest.write_text(json.dumps({'name':'fixture','version':'1.0.0'}))
        (package/'SKILL.md').write_text('---\nname: fixture\ndescription: fixture\n---\ncontent')
        dep=module.validate_candidate(package,'codex','fixture','fixture')
        config={'deps':{'fixture':dep},'playbook_root':str(self.base),'playbook':{'steps':[]}}
        external=self.base/'external.json';external.write_bytes(manifest.read_bytes())
        original_load=module.load_json;reads=[]
        def tracked_load(path,code):
            reads.append(str(path.resolve()))
            return original_load(path,code)
        module.load_json=tracked_load
        manifest.unlink();manifest.symlink_to(external)
        with self.assertRaises(SystemExit) as rejected:module.check_steps(config)
        self.assertEqual(rejected.exception.code,2);self.assertEqual(reads,[])
        manifest.unlink();manifest.write_bytes(external.read_bytes())
        config['deps']['fixture']['manifest']=str(external)
        with self.assertRaises(SystemExit):module.check_steps(config)
        self.assertEqual(reads,[])
        config['deps']['fixture']['manifest']=str(manifest)
        runtime_dir=manifest.parent;runtime_dir.rename(package/'original-manifest-dir');runtime_dir.symlink_to(package/'original-manifest-dir',target_is_directory=True)
        with self.assertRaises(SystemExit):module.check_steps(config)
        with self.assertRaises(SystemExit):module.validate_candidate(package,'codex','fixture','fixture')
        self.assertEqual(reads,[])
    def test_dependency_package_and_ancestor_symlink_rejected_before_read(self):
        if not RESOLVER.exists():self.skipTest('no dependency resolver')
        module=self._load_resolver('root_boundary')
        for replace_ancestor in [False, True]:
            with self.subTest(ancestor=replace_ancestor):
                parent=self.base/('ancestor-case' if replace_ancestor else 'package-case')
                package=parent/'package';(package/'.codex-plugin').mkdir(parents=True)
                (package/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'fixture','version':'1.0.0'}))
                (package/'SKILL.md').write_text('---\nname: fixture\ndescription: fixture\n---\ncontent')
                dep=module.validate_candidate(package,'codex','fixture','fixture')
                config={'deps':{'fixture':dep},'playbook_root':str(package),'playbook':{'steps':[]}}
                target=parent if replace_ancestor else package
                outside=self.base/('outside-ancestor' if replace_ancestor else 'outside-package')
                target.rename(outside);target.symlink_to(outside,target_is_directory=True)
                original_load=module.load_json;reads=[]
                def tracked_load(path,code):
                    reads.append(str(path.resolve()))
                    return original_load(path,code)
                module.load_json=tracked_load
                try:
                    with self.assertRaises(SystemExit) as rejected:module.check_steps(config)
                    self.assertEqual(rejected.exception.code,2);self.assertEqual(reads,[])
                finally:module.load_json=original_load
    def test_dependency_deleted_package_is_structured_rejection(self):
        if not RESOLVER.exists():self.skipTest('no dependency resolver')
        module=self._load_resolver('deleted_root')
        package=self.base/'deleted-package';(package/'.codex-plugin').mkdir(parents=True)
        (package/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'fixture','version':'1.0.0'}))
        (package/'SKILL.md').write_text('---\nname: fixture\ndescription: fixture\n---\ncontent')
        dep=module.validate_candidate(package,'codex','fixture','fixture')
        config={'deps':{'fixture':dep},'playbook_root':str(package),'playbook':{'steps':[]}}
        shutil.rmtree(package)
        result=self.call('python3',RESOLVER,'--check-steps',input=json.dumps(config))
        self.assertEqual(result.returncode,2);self.assertIn('package-root-unavailable',result.stderr);self.assertNotIn('Traceback',result.stderr)
    def test_own_script_root_and_ancestor_identity_are_preserved(self):
        if not RESOLVER.exists():self.skipTest('no dependency resolver')
        for ancestor in [False, True]:
            with self.subTest(ancestor=ancestor):
                parent=self.base/('own-parent' if ancestor else 'own-package');package=parent/'playbook';package.mkdir(parents=True)
                (package/'script.sh').write_text('#!/bin/sh\nexit 0\n')
                config={'deps':{},'playbook_root':str(package),'playbook':{'steps':[{'id':'own','script':'script.sh'},{'id':'later','script':'not-created.sh','when':False}]}}
                self.assertEqual(self.call('python3',RESOLVER,'--check-steps',input=json.dumps(config)).returncode,0)
                target=parent if ancestor else package;outside=self.base/('outside-own-parent' if ancestor else 'outside-own-package');target.rename(outside);target.symlink_to(outside,target_is_directory=True)
                for selected in [[],['later']]:
                    result=self.call('python3',RESOLVER,'--check-steps',*selected,input=json.dumps(config))
                    self.assertEqual(result.returncode,2);self.assertIn('path-root-symlink',result.stderr)
    def test_plugin_root_must_be_an_absolute_symlink_free_directory(self):
        if not RESOLVER.exists():self.skipTest('no dependency resolver')
        package = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
        entry = package/'skills/pb'
        relative = subprocess.run(['python3', str(RESOLVER), '--plugin-root', 'plugins/write-doc/skills/pb',
                                   '--plugin', 'content-types', '--marketplace', 'write-doc'],
                                  text=True, capture_output=True, env=self.env, cwd=self.base/'provider', timeout=60)
        self.assertEqual(relative.returncode, 2);self.assertIn('plugin-root-not-absolute', relative.stderr)
        link = self.base/'link-entry';link.symlink_to(entry, target_is_directory=True)
        linked = self._resolve(link, 'content-types', 'write-doc', self.env)
        self.assertEqual(linked.returncode, 2);self.assertIn('plugin-root-symlink', linked.stderr)

    # ---- 依存契約（外部は公開 playbook のみ／束縛） ----
    def test_internal_dependency_is_not_classified_external(self):
        """同 package の内部 skill は内部と分類する。所属は marketplace 宣言を持つ package manifest で決まる。"""
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        package = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
        result = self._resolve(package/'skills/pb', 'content-types', 'write-doc', self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['dependency_scope'], 'internal')
        self.assertEqual(payload['source_kind'], 'repository')
        self.assertEqual(payload['root'], str(package/'internal/content-types'))
    def test_internal_search_works_when_plugin_name_differs_from_marketplace(self):
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        package = self._provider('stub', 'stub-write-doc', 'stub-docs', 'write-doc/write-doc', 'stub-save')
        result = self._resolve(package/'skills/pb', 'stub-save', 'stub-docs', self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['dependency_scope'], 'internal')
    def test_external_provider_resolves_through_contract_entry_files(self):
        """外部依存は implements の契約IDが指す公開 playbook（SKILL.md / playbook.yml / CONTRACT.md）へ解決する。"""
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        provider = self._direct_provider()
        devmap = self._devmap({'write-doc/write-doc': provider})
        entry = self._consumer(
            '  - {id: document, playbook: write-doc, purpose: fixture,\n'
            '     input: {document_type: north-star}, provides: [status, path, reason]}\n',
            [('write-doc', 'write-doc')], name='consumer-write')
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap))
        accepted = self._resolve(entry, 'write-doc', 'write-doc', environment)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        payload = json.loads(accepted.stdout)
        self.assertEqual(payload['dependency_scope'], 'external')
        self.assertEqual(payload['contract_version'], 2)
        self.assertEqual(payload['root'], str(provider/'skills/write-doc'))
        self.assertEqual(payload['entry'], str(provider/'skills/write-doc/SKILL.md'))
        self.assertEqual(payload['entry_skill'], 'write-doc')
        checked = self._check_steps(entry, [('write-doc', 'write-doc')], environment)
        self.assertEqual(checked.returncode, 0, checked.stderr)

        # 契約v1の grill も同じ3 file だけで解決する。
        grill = self._direct_provider(name='direct-grill', contract='grill/grill', contract_version=1, plugin='grill', marketplace='grill')
        grill_entry = self._consumer(
            '  - {id: settle, playbook: grill, purpose: fixture, provides: [decisions, open_questions]}\n',
            [('grill', 'grill')], name='consumer-grill')
        grill_accepted = self._resolve(grill_entry, 'grill', 'grill', dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(self._devmap({'grill/grill': grill}))))
        self.assertEqual(grill_accepted.returncode, 0, grill_accepted.stderr)
        self.assertEqual(json.loads(grill_accepted.stdout)['entry'], str(grill/'skills/grill/SKILL.md'))

        # 旧版の write-doc 実装（契約v1）へは倒さない。
        old_write_doc = self._provider('old-write-doc', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types', contract_version=1)
        old_rejected = self._resolve(entry, 'write-doc', 'write-doc', dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(self._devmap({'write-doc/write-doc': old_write_doc}))))
        self.assertEqual(old_rejected.returncode, 2)
        self.assertIn('external-dependency-no-playbook', old_rejected.stderr)
        self._devmap({'write-doc/write-doc': provider})

        # 反例: 公開入口の SKILL.md と CONTRACT.md はどちらも必須。
        for missing in ('SKILL.md', 'CONTRACT.md'):
            path = provider/'skills/write-doc'/missing
            original = path.read_text()
            path.unlink()
            rejected = self._resolve(entry, 'write-doc', 'write-doc', environment)
            self.assertEqual(rejected.returncode, 2, missing)
            self.assertIn('implements-entry-missing', rejected.stderr)
            path.write_text(original)

        # 契約版と implements 版が食い違う宣言は受け付けない。
        for runtime in ('claude', 'codex'):
            manifest = provider/f'.{runtime}-plugin/plugin.json'
            data = json.loads(manifest.read_text())
            data['metadata']['harness']['implements'][0]['version'] = 1
            manifest.write_text(json.dumps(data))
        mismatch = self._resolve(entry, 'write-doc', 'write-doc', environment)
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn('implements-version-invalid', mismatch.stderr)
    def test_external_skill_and_script_steps_are_rejected(self):
        """外部依存は playbook: でしか使えない。when 付きの非活性 step も落とす。"""
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(self._devmap({'write-doc/write-doc': provider})))
        cases = [
            ('external-dependency-skill',
             '  - {id: work, skill: pb, purpose: fixture, provides: [x]}\n'),
            ('external-dependency-skill',
             '  - {id: work, skill: pb, when: never, purpose: fixture, provides: [x]}\n'),
            ('external-dependency-script',
             '  - {id: work, script: scripts/x.py, plugin: write-doc, purpose: fixture, provides: [x]}\n'),
            ('external-dependency-path',
             '  - {id: work, playbook: write-doc, purpose: fixture, provides: [x],\n'
             '     arguments: ["${.deps[\'write-doc\'].root}/scripts/save.sh"]}\n'),
        ]
        for code, steps in cases:
            entry = self._consumer(steps, [('write-doc', 'write-doc')])
            result = self._check_steps(entry, [('write-doc', 'write-doc')], environment)
            self.assertEqual(result.returncode, 2, code + ': ' + result.stdout)
            self.assertIn('[error:' + code + ']', result.stderr)
        # 同 package の公開入口・内部 skill を skill: で呼ぶのは内部契約であり、resolver は拒まない。
        entry = self._consumer('  - {id: local, skill: local-tool, purpose: fixture}\n'
                               '  - {id: work, playbook: write-doc, purpose: fixture, provides: [x]}\n',
                               [('write-doc', 'write-doc')])
        accepted = self._check_steps(entry, [('write-doc', 'write-doc')], environment)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
    def test_runtime_follows_cache_root_not_ambient_environment(self):
        """shell に CODEX_HOME があるだけで、claude の cache を codex と呼ばない。"""
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        homes = {'claude': self.base/'claude-home', 'codex': self.base/'codex-home'}
        environment = dict(self.env, CLAUDE_CONFIG_DIR=str(homes['claude']), CODEX_HOME=str(homes['codex']))
        for name in ('HARNESS_PLUGIN_RUNTIME', 'CLAUDE_PLUGIN_ROOT', 'CLAUDE_PLUGIN_CACHE', 'CODEX_PLUGIN_CACHE'):
            environment.pop(name, None)
        for expected, home in homes.items():
            relative = f'{home.name}/plugins/cache/write-doc/write-doc/1.0.0'
            package = self._provider(relative, 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
            result = self._resolve(package/'skills/pb', 'content-types', 'write-doc', environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['runtime'], expected, result.stdout)
    def test_bindings_reject_bad_key_missing_implements_and_drift(self):
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types', types=['north-star'])
        naked = self._provider('naked', 'naked-doc', 'naked-docs', 'write-doc/write-doc', 'naked-save', implements=False)
        devmap = self._devmap({'write-doc/write-doc': provider, 'naked-docs/naked-doc': naked})
        config = self.base/'config'
        (config/'harness-plugins').mkdir(parents=True, exist_ok=True)
        bindings = config/'harness-plugins/dependencies.yml'
        entry = self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x],\n'
                               '     input: {document_type: north-star}}\n', [('write-doc', 'write-doc')])
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap), XDG_CONFIG_HOME=str(config))
        lock = self.base/'run.lock'

        def create_lock():
            lock.unlink(missing_ok=True)
            return subprocess.run(['python3', str(RESOLVER), '--create-lock', '--entry', 'demo', '--repo-root', str(self.base/'consumer'), '--lock', str(lock)],
                                  text=True, capture_output=True, env=environment, timeout=60)

        def resolve():
            return self._resolve(entry, 'write-doc', 'write-doc', environment, '--bindings', str(lock))

        bindings.write_text('version: 1\nbindings:\n  "write-doc": {marketplace: naked-docs, plugin: naked-doc}\n')
        self.assertIn('[error:binding-key-invalid]', create_lock().stderr)
        bindings.write_text('version: 1\nbindings:\n  "write-doc/write-doc": {marketplace: naked-docs, plugin: naked-doc, path: /tmp}\n')
        self.assertIn('[error:binding-schema]', create_lock().stderr)
        bindings.write_text('version: 1\nbindings:\n  "write-doc/write-doc": {marketplace: naked-docs, plugin: naked-doc}\n')
        self.assertEqual(create_lock().returncode, 0)
        self.assertIn('[error:binding-not-implemented]', resolve().stderr)
        bindings.write_text('version: 1\nbindings:\n  "demo/local-tool": {marketplace: demo, plugin: local-tool}\n')
        self.assertEqual(create_lock().returncode, 0)
        bound_internal = self._resolve(entry, 'local-tool', 'demo', environment, '--bindings', str(lock))
        self.assertIn('[error:binding-internal-dependency]', bound_internal.stderr)
        bindings.unlink()
        self.assertEqual(create_lock().returncode, 0)
        first = resolve()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('write-doc/write-doc', json.loads(lock.read_text())['entries'])
        (provider/'skills/pb/SKILL.md').write_text('---\nname: pb\ndescription: changed\n---\nchanged\n')
        self.assertIn('[error:binding-drift]', resolve().stderr)
    def test_external_root_follows_contract_not_other_playbooks(self):
        """外部依存の root は契約IDが指す公開 playbook であり、同 package の別 playbook ではない。"""
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types', decoy=True)
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(self._devmap({'write-doc/write-doc': provider})))
        entry = self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x]}\n', [('write-doc', 'write-doc')])
        result = self._resolve(entry, 'write-doc', 'write-doc', environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['root'], str(provider/'skills/pb'))
        self.assertNotEqual(payload['root'], str(provider/'skills/decoy'))
        self.assertEqual(payload['entry'], str(provider/'skills/pb/SKILL.md'))
        self.assertEqual(payload['entry_skill'], 'pb')
    def test_external_reference_forms_are_all_rejected(self):
        """外部依存への ${.deps...} 参照はブラケット形・引用形・root/entry 形を含めすべて拒否する。"""
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(self._devmap({'write-doc/write-doc': provider})))

        def run(reference):
            steps = ('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x],\n'
                     '     arguments: ["' + reference + '"]}\n')
            entry = self._consumer(steps, [('write-doc', 'write-doc')])
            return self._check_steps(entry, [('write-doc', 'write-doc')], environment)

        rejected = [
            ('external-dependency-path', '${.deps[\'write-doc\'][\'root\']}/references/private.md'),
            ('external-dependency-path', '${.deps.\'write-doc\'.root}/../../LICENSE'),
            ('external-dependency-path', '${.deps.write-doc.skills.write-doc}'),
            ('external-dependency-path', '${.deps.write-doc.package_root}'),
            ('external-dependency-path', '${.deps.write-doc.root}/playbook.yml'),
            ('external-dependency-path', '${.deps.write-doc.entry}'),
            ('external-dependency-config', '${write-doc:.private.setting}'),
        ]
        for code, reference in rejected:
            result = run(reference)
            self.assertEqual(result.returncode, 2, reference)
            self.assertIn('[error:' + code + ']', result.stderr, reference)
        # 依存でない名前の設定参照は resolver の対象外（禁止参照形の検査は root validator が担う）。
        self.assertEqual(run('${doc-render:.output.format}').returncode, 0)
    def test_lock_snapshot_survives_bindings_file_change(self):
        """入口が束縛を snapshot し、元 file が書き換わっても同じ run は同じ実体を見る。"""
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
        other = self._provider('other', 'other-doc', 'other-docs', 'write-doc/write-doc', 'other-save')
        devmap = self._devmap({'write-doc/write-doc': provider, 'other-docs/other-doc': other})
        config = self.base/'config'; (config/'harness-plugins').mkdir(parents=True, exist_ok=True)
        bindings = config/'harness-plugins/dependencies.yml'
        bindings.write_text('version: 1\nbindings:\n  "write-doc/write-doc": {marketplace: other-docs, plugin: other-doc}\n')
        entry = self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [path]}\n', [('write-doc', 'write-doc')])
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap), XDG_CONFIG_HOME=str(config))
        lock = self.base/'run.lock'
        created = subprocess.run(['python3', str(RESOLVER), '--create-lock', '--entry', 'demo', '--repo-root', str(self.base/'consumer'), '--lock', str(lock)],
                                 text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(created.returncode, 0, created.stderr)
        snapshot = json.loads(lock.read_text())
        self.assertEqual(snapshot['bindings_layer'], 'personal')
        self.assertEqual(snapshot['bindings']['write-doc/write-doc'], {'marketplace': 'other-docs', 'plugin': 'other-doc'})
        first = self._resolve(entry, 'write-doc', 'write-doc', environment, '--bindings', str(lock))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['plugin'], 'other-doc')
        # 元ファイルを書き換えても、同じ lock を渡す限り snapshot の実体を使う。
        bindings.write_text('version: 1\nbindings:\n  "write-doc/write-doc": {marketplace: write-doc, plugin: write-doc}\n')
        child = self._resolve(entry, 'write-doc', 'write-doc', environment, '--bindings', str(lock))
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout)['plugin'], 'other-doc')
    def test_doctor_resolves_playbooks_against_sibling_checkouts(self):
        """外部依存の解決は fixture ではなく、兄弟 checkout の実配布物に対して行う。"""
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        provider = self._provider('write-doc-plugins', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
        self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x]}\n', [('write-doc', 'write-doc')])
        repo = self.base/'consumer'
        (repo/'shared/playbook').mkdir(parents=True)
        shutil.copy(RESOLVER, repo/'shared/playbook/resolve-dependency.py')
        environment = dict(self.env, XDG_CONFIG_HOME=str(self.base/'config'))

        def diagnose(extra=None):
            result = subprocess.run(
                ['python3', str(ROOT/'scripts/doctor.py'), '--repository', str(repo), '--repo', str(repo)],
                text=True, capture_output=True, env=dict(environment, **(extra or {})), timeout=120)
            return {check['check']: check for check in json.loads(result.stdout)['checks']}

        label = 'resolve:plugins/demo-pack/skills/demo'
        checks = diagnose()
        resolved = checks[label + ':write-doc/write-doc']
        self.assertTrue(resolved['ok'], resolved.get('detail'))
        self.assertTrue(checks[label + ':steps']['ok'], checks[label + ':steps'].get('detail'))
        bindings = checks['dependency-bindings']
        self.assertTrue(bindings['ok'], bindings)
        self.assertEqual(bindings['real_distribution'], {'source': 'sibling-checkout', 'dependencies': {'write-doc/write-doc': str(provider)}})
        self.assertTrue(any(line.startswith('write-doc/write-doc → write-doc/write-doc 1.0.0') for line in bindings['entries'][0]['resolved']), bindings['entries'])
        # 兄弟が無ければ、fixture へ倒さず理由付きで NG にする。
        absent = diagnose({'HARNESS_PLUGIN_SIBLING_ROOT': str(self.base/'nowhere')})
        self.assertFalse(absent[label]['ok'])
        self.assertIn('write-doc/write-doc', absent[label]['detail'])
        self.assertFalse(absent['dependency-bindings']['ok'])
    def test_consumer_lint_flags_provider_internal_names(self):
        """消費側の文書に、外部依存の内部名（統合前の内部 skill 名を含む）が漏れたら落とす。"""
        if not RESOLVER.exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(self._devmap({'write-doc/write-doc': provider})))
        entry = self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x]}\n', [('write-doc', 'write-doc')])
        repo = self.base/'consumer'
        (repo/'shared/playbook').mkdir(parents=True)
        shutil.copy(RESOLVER, repo/'shared/playbook/resolve-dependency.py')
        clean = subprocess.run(['python3', str(ROOT/'scripts/lint-consumer-contract.py'), '--repo', str(repo), '--runtime', 'codex', '--json'],
                               text=True, capture_output=True, env=environment, timeout=120)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        (entry/'README.md').write_text('保存は content-types と author-document に任せる。\n')
        linted = subprocess.run(['python3', str(ROOT/'scripts/lint-consumer-contract.py'), '--repo', str(repo), '--runtime', 'codex', '--json'],
                                text=True, capture_output=True, env=environment, timeout=120)
        self.assertEqual(linted.returncode, 1, linted.stdout + linted.stderr)
        codes = {finding['code'] for finding in json.loads(linted.stdout)['findings']}
        self.assertEqual(codes, {'provider-internal-name', 'save-vocabulary'})
    def test_consumer_lint_parses_skill_frontmatter_as_yaml(self):
        """quoted/commented nameを受理し、本文の偽nameをidentityにしない。"""
        lint_path = ROOT/'runtime-source/lint-consumer-contract.py'
        if not lint_path.is_file():
            lint_path = ROOT/'scripts/lint-consumer-contract.py'
        spec = importlib.util.spec_from_file_location('consumer_lint_frontmatter', lint_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        skill = self.base/'SKILL.md'
        skill.write_text(
            '---\nname: "quoted-name" # YAML comment\ndescription: fixture\n---\n'
            'name: body-decoy\n', encoding='utf-8')
        self.assertEqual(module.skill_name(skill), 'quoted-name')
        skill.write_text('---\nname: [not, a, string]\n---\nname: body-decoy\n', encoding='utf-8')
        self.assertIsNone(module.skill_name(skill))

if __name__=='__main__':unittest.main()
