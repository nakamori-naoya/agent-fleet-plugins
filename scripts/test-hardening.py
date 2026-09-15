#!/usr/bin/env python3
"""Observable regression checks for shells, publication, state and maintenance CLIs."""
import concurrent.futures
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class Hardening(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hardening-')
        self.base = Path(self.temp.name).resolve()
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.base/'state'), XDG_CONFIG_HOME=str(self.base/'config'), HARNESS_PLUGIN_RUNTIME='codex')
    def tearDown(self):
        self.temp.cleanup()
    def call(self, *args, input=None):
        return subprocess.run(list(map(str,args)), input=input, text=True, capture_output=True, env=self.env, cwd=self.base, timeout=30)
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
    def test_doctor_rejects_external_skill_and_resolver_before_access(self):
        repo=self.base/'repo';package=repo/'plugins/p';(repo/'scripts').mkdir(parents=True)
        (repo/'scripts/validate.sh').write_text('#!/bin/bash\nexit 0\n');(package/'skill').mkdir(parents=True)
        external=self.base/'outside.md';external.write_text('---\nname: external-private-name\ndescription: fixture\n---\n')
        skill=package/'skill/SKILL.md';skill.symlink_to(external)
        for runtime,relative in [('codex','.agents/plugins/marketplace.json'),('claude','.claude-plugin/marketplace.json')]:
            manifest=package/f'.{runtime}-plugin/plugin.json';manifest.parent.mkdir(parents=True);manifest.write_text(json.dumps({'name':'p','version':'1.0.0','skills':['./skill']}))
            catalog=repo/relative;catalog.parent.mkdir(parents=True);catalog.write_text(json.dumps({'plugins':[{'name':'p','source':{'source':'local','path':'./plugins/p'} if runtime=='codex' else './plugins/p'}]}))
        result=self.call('python3',ROOT/'scripts/doctor.py','--repository',repo,'--distribution-only')
        self.assertEqual(result.returncode,1)
        self.assertNotIn('external-private-name',result.stdout)
        self.assertIn('repository-path-boundary',result.stdout)
        skill.unlink();skill.write_text('---\nname: fixture\ndescription: fixture\n---\n')
        (package/'scripts').mkdir()
        marker=self.base/'executed';script=self.base/'outside.sh';script.write_text('#!/bin/bash\ntouch "'+str(marker)+'"\n')
        (package/'scripts/resolve.sh').symlink_to(script)
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
    def test_config_survives_shell_and_cleanup_isolated(self):
        helper=ROOT/'shared/run-config.py'
        if not helper.exists(): self.skipTest('no runtime config')
        fake=self.base/'plugin';(fake/'scripts').mkdir(parents=True)
        (fake/'scripts/resolve.sh').write_text("#!/bin/bash\nprintf 'value: true\\n'\n")
        def create():
            r=self.call('python3',helper,'create','--root',fake,'--',self.base)
            self.assertEqual(r.returncode,0,r.stderr)
            return Path(r.stdout.strip())
        one,two=create(),create()
        self.assertEqual(self.call('bash','-c','cat "$1"','bash',one).stdout,'value: true\n')
        self.assertEqual(self.call('python3',helper,'cleanup','--config',one).returncode,0)
        self.assertFalse(one.exists());self.assertTrue(two.exists())
        self.assertEqual(self.call('python3',helper,'cleanup','--config',two).returncode,0)
        (fake/'scripts/resolve.sh').write_text('#!/bin/bash\nexit 4\n')
        r=self.call('python3',helper,'create','--root',fake,'--',self.base)
        self.assertEqual(r.returncode,2)
    def test_public_entries_are_unique_and_root_is_executable(self):
        catalog=json.loads((ROOT/'.agents/plugins/marketplace.json').read_text())
        for entry in catalog['plugins']:
            package=ROOT/entry['source']['path'];manifest=json.loads((package/'.codex-plugin/plugin.json').read_text())
            paths=manifest['skills'];paths=[paths] if isinstance(paths,str) else paths
            names=[]
            for relative in paths:
                directory=package/relative
                skills=[directory/'SKILL.md'] if (directory/'SKILL.md').exists() else list(directory.glob('*/SKILL.md'))
                self.assertTrue(skills)
                for skill in skills:
                    header=skill.read_text().split('---',2)[1]
                    names.append(next(line[6:] for line in header.splitlines() if line.startswith('name: ')))
                    owner=next((p for p in [skill.parent,*skill.parents] if (p/'scripts/prepare.sh').exists()),None)
                    if owner:
                        r=self.call('bash',owner/'scripts/prepare.sh','--root-only')
                        self.assertEqual(r.returncode,0,r.stderr)
                        self.assertEqual(Path(r.stdout.strip()).resolve(),owner.resolve())
                        blocks=re.findall(r'```bash\n(.*?)```',skill.read_text(),re.S)
                        if blocks and 'BUNDLE_ROOT=' in blocks[0]:
                            previous=self.env.get('CLAUDE_PLUGIN_ROOT');self.env['CLAUDE_PLUGIN_ROOT']=str(package)
                            try:
                                result=self.call('bash','-c',blocks[0]+'\nbash "$PLUGIN_ROOT/scripts/prepare.sh" --root-only')
                                self.assertEqual(result.returncode,0,result.stderr)
                                self.assertEqual(Path(result.stdout.strip()).resolve(),owner.resolve())
                            finally:
                                if previous is None:self.env.pop('CLAUDE_PLUGIN_ROOT',None)
                                else:self.env['CLAUDE_PLUGIN_ROOT']=previous
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
        internal=repo/'plugins/p/playbooks/internal'
        for runtime in ['codex','claude']:
            path=internal/f'.{runtime}-plugin/plugin.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'name':'fixture','version':'0.4.0','description':'same name internal playbook'}))
        internal_before={str(path):path.read_bytes() for path in internal.rglob('plugin.json')}
        checks=self.base/'checks.json';checks.write_text('{"codex":"unverified","claude":"unverified"}')
        r=self.call('python3',ROOT/'scripts/release.py','--repo',repo,'--plugin','fixture','--version','2.0.0','--notes','change','--breaking','yes','--migration','new run','--checks',checks,'--apply')
        self.assertEqual(r.returncode,0,r.stderr)
        for runtime in ['codex','claude']:
            path=repo/f'plugins/p/.{runtime}-plugin/plugin.json'
            d=json.loads(path.read_text());self.assertEqual(d['version'],'2.0.0');self.assertEqual(d['requires'],[{'plugin':'other','marketplace':'other'}])
        self.assertEqual(internal_before,{str(path):path.read_bytes() for path in internal.rglob('plugin.json')})
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
    def test_conditional_path_and_symlink_rejected(self):
        resolver=ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists():self.skipTest('no playbook')
        config={'deps':{},'playbook_root':str(self.base),'playbook':{'steps':[{'id':'later','when':'optional','script':'../escape'}]}}
        r=self.call('python3',resolver,'--check-steps',input=json.dumps(config));self.assertEqual(r.returncode,2);self.assertIn('path-format',r.stderr)
        target=self.base/'good';target.write_text('content');(self.base/'link').symlink_to(target)
        config['playbook']['steps'][0]['script']='link'
        self.assertEqual(self.call('python3',resolver,'--check-steps','later',input=json.dumps(config)).returncode,2)
    def test_dependency_mutation_and_contract_mismatch(self):
        resolver=ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists():self.skipTest('no dependency resolver')
        sys.dont_write_bytecode=True
        spec=importlib.util.spec_from_file_location('dependency',resolver);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        package=self.base/'dependency';(package/'.codex-plugin').mkdir(parents=True)
        manifest=package/'.codex-plugin/plugin.json';manifest.write_text(json.dumps({'name':'fixture','version':'1.0.0','metadata':{'harness':{'contractVersion':1}}}))
        skill=package/'SKILL.md';skill.write_text('---\nname: fixture\ndescription: fixture\n---\noriginal')
        dep=module.validate_candidate(package,'codex','fixture','fixture')
        config={'deps':{'fixture':dep},'playbook_root':str(self.base),'playbook':{'steps':[{'id':'invoke','skill':'fixture'}]}}
        self.assertEqual(self.call('python3',resolver,'--check-steps',input=json.dumps(config)).returncode,0)
        skill.write_text(skill.read_text()+' changed')
        result=self.call('python3',resolver,'--check-steps',input=json.dumps(config));self.assertEqual(result.returncode,2);self.assertIn('content-hash',result.stderr)
        manifest.write_text(json.dumps({'name':'fixture','version':'1.0.0','metadata':{'harness':{'contractVersion':999}}}))
        with self.assertRaises(SystemExit):module.validate_candidate(package,'codex','fixture','fixture')
    def test_dependency_manifest_rejected_before_external_json_read(self):
        resolver=ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists():self.skipTest('no dependency resolver')
        sys.dont_write_bytecode=True
        spec=importlib.util.spec_from_file_location('manifest_boundary',resolver);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
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
        resolver=ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists():self.skipTest('no dependency resolver')
        sys.dont_write_bytecode=True
        spec=importlib.util.spec_from_file_location('root_boundary',resolver);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
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
        resolver=ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists():self.skipTest('no dependency resolver')
        sys.dont_write_bytecode=True
        spec=importlib.util.spec_from_file_location('deleted_root',resolver);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        package=self.base/'deleted-package';(package/'.codex-plugin').mkdir(parents=True)
        (package/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'fixture','version':'1.0.0'}))
        (package/'SKILL.md').write_text('---\nname: fixture\ndescription: fixture\n---\ncontent')
        dep=module.validate_candidate(package,'codex','fixture','fixture')
        config={'deps':{'fixture':dep},'playbook_root':str(package),'playbook':{'steps':[]}}
        shutil.rmtree(package)
        result=self.call('python3',resolver,'--check-steps',input=json.dumps(config))
        self.assertEqual(result.returncode,2);self.assertIn('package-root-unavailable',result.stderr);self.assertNotIn('Traceback',result.stderr)
    def test_own_script_root_and_ancestor_identity_are_preserved(self):
        resolver=ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists():self.skipTest('no dependency resolver')
        for ancestor in [False, True]:
            with self.subTest(ancestor=ancestor):
                parent=self.base/('own-parent' if ancestor else 'own-package');package=parent/'playbook';package.mkdir(parents=True)
                (package/'script.sh').write_text('#!/bin/sh\nexit 0\n')
                config={'deps':{},'playbook_root':str(package),'playbook':{'steps':[{'id':'own','script':'script.sh'},{'id':'later','script':'not-created.sh','when':False}]}}
                self.assertEqual(self.call('python3',resolver,'--check-steps',input=json.dumps(config)).returncode,0)
                target=parent if ancestor else package;outside=self.base/('outside-own-parent' if ancestor else 'outside-own-package');target.rename(outside);target.symlink_to(outside,target_is_directory=True)
                for selected in [[],['later']]:
                    result=self.call('python3',resolver,'--check-steps',*selected,input=json.dumps(config))
                    self.assertEqual(result.returncode,2);self.assertIn('path-root-symlink',result.stderr)
    def test_dependency_entry_root_cannot_rebase_or_escape_package(self):
        resolver=ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists():self.skipTest('no dependency resolver')
        sys.dont_write_bytecode=True
        spec=importlib.util.spec_from_file_location('entry_boundary',resolver);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        package=self.base/'entry-package';(package/'.codex-plugin').mkdir(parents=True);entry=package/'entry';entry.mkdir()
        (package/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'fixture','version':'1.0.0','metadata':{'harness':{'entryRoot':'./entry'}}}))
        (package/'SKILL.md').write_text('---\nname: fixture\ndescription: fixture\n---\ncontent')
        (entry/'script.sh').write_text('#!/bin/sh\nexit 0\n');(entry/'playbook.yml').write_text('steps: []\n');(entry/'scripts').mkdir();(entry/'scripts/resolve.sh').write_text('#!/bin/sh\nexit 0\n');(entry/'scripts/prepare.sh').write_text('#!/bin/sh\nexit 0\n')
        dep=module.validate_candidate(package,'codex','fixture','fixture');config={'deps':{'fixture':dep},'playbook_root':str(package),'playbook':{'steps':[{'id':'script','plugin':'fixture','script':'script.sh'},{'id':'skill','skill':'fixture'},{'id':'child','playbook':'fixture'}]}}
        module.check_steps(config)
        outside=self.base/'entry-outside';entry.rename(outside);entry.symlink_to(outside,target_is_directory=True)
        reads=[];module.load_json=lambda *args: reads.append(args)
        with self.assertRaises(SystemExit):module.check_steps(config)
        self.assertEqual(reads,[])
        dep['root']=str(outside)
        with self.assertRaises(SystemExit):module.check_steps(config)
        self.assertEqual(reads,[])
    def test_all_templates_resolve_from_plugin_root(self):
        plugin=ROOT/'plugins/skills/authoring/content-types'
        if not plugin.exists():self.skipTest('no content types')
        result=self.call('yq','-o=json','.',plugin/'assets/template-examples.yml');pairs=json.loads(result.stdout)['pairs']
        manifest=json.loads((ROOT/'plugins/.claude-plugin/plugin.json').read_text());types=[t for i in manifest['metadata']['harness']['implements'] for t in i.get('types',[])]
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
    def test_state_rejects_symlink_ancestor_before_creating_files(self):
        state = ROOT/'plugins/playbooks/authoring/write-doc/scripts/state.py'
        if not state.exists():self.skipTest('no write-doc state')
        root = self.base/'state/harness-plugins/playbooks';root.mkdir(parents=True)
        outside = self.base/'outside';outside.mkdir()
        (root/'fixture').symlink_to(outside, target_is_directory=True)
        config = self.base/'config.json';config.write_text(json.dumps({'playbook':{'name':'fixture','steps':[{'id':'save','provides':['path']}]}}))
        result = self.call('python3',state,'init','--config',config,'--repo',self.base,'--run-id','run')
        self.assertEqual(result.returncode,2)
        self.assertIn('symlink',result.stderr)
        self.assertEqual(list(outside.iterdir()),[])
    def test_state_repo_retry_files_corruption_and_parallel_start(self):
        state=next((c for c in [ROOT/'shared/playbook/state.py',ROOT/'plugins/playbooks/authoring/write-doc/scripts/state.py'] if c.exists()),None)
        if state is None:self.skipTest('no playbook state')
        config=self.base/'resolved.json';config.write_text(json.dumps({'playbook':{'name':'fixture','steps':[{'id':'save','provides':['path']}]}}))
        a=self.base/'a';b=self.base/'b';a.mkdir();b.mkdir()
        def call(command,*extra):return self.call('python3',state,command,'--config',config,'--run-id','run',*extra)
        initialized=call('init','--repo',a);self.assertEqual(initialized.returncode,0)
        state_path=Path(json.loads(initialized.stdout)['state']);original=state_path.read_text()
        damaged=json.loads(original);damaged['status']='completed';state_path.write_text(json.dumps(damaged))
        self.assertEqual(call('init','--repo',a).returncode,2)
        state_path.write_text(original)
        self.assertEqual(call('init','--repo',b).returncode,2)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:call('start','--step','save'),range(2)))
        self.assertEqual(sorted(r.returncode for r in results),[0,2])
        self.assertEqual(call('complete','--step','save','--provide','path='+str(self.base/'missing')).returncode,2)
        self.assertEqual(call('fail','--step','save','--reason','fixture').returncode,0)
        self.assertEqual(json.loads(call('init','--repo',a).stdout)['status'],'needs_retry')
        self.assertEqual(call('retry').returncode,0);self.assertEqual(call('start','--step','save').returncode,0)
        artifact=self.base/'final';artifact.write_text('saved')
        self.assertEqual(call('complete','--step','save','--provide','path='+str(artifact)).returncode,0)
        status=json.loads(call('status').stdout);self.assertEqual(status['status'],'completed');self.assertEqual(status['steps'][0]['attempts'],2)
        Path(status['state']).write_text('{broken')
        self.assertEqual(call('init','--repo',a).returncode,2)


    # ---- 依存契約（外部はplaybookのみ／束縛）の負の試験 ----
    def _manifest(self, root, data):
        for runtime in ('claude', 'codex'):
            path = root / f'.{runtime}-plugin/plugin.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))

    def _skill(self, root, name):
        root.mkdir(parents=True, exist_ok=True)
        (root/'SKILL.md').write_text(f'---\nname: {name}\ndescription: fixture\n---\nfixture\n')

    def _entry_scripts(self, root, resolver=True):
        (root/'scripts').mkdir(parents=True, exist_ok=True)
        for name in ('resolve.sh', 'prepare.sh'):
            (root/'scripts'/name).write_text('#!/usr/bin/env bash\nexit 0\n')
            (root/'scripts'/name).chmod(0o755)
        if resolver:
            for name in ('resolve.sh', 'resolve-dependency.py'):
                (root/'scripts'/name).write_bytes((ROOT/'shared/playbook'/name).read_bytes())
            (root/'scripts/resolve.sh').chmod(0o755)
            (root/'scripts/validate-config.sh').write_text('#!/usr/bin/env bash\nexit 0\n')
            (root/'scripts/validate-config.sh').chmod(0o755)
            (root/'scripts/prepare.sh').write_text('#!/usr/bin/env bash\nexit 0\n')
            (root/'scripts/prepare.sh').chmod(0o755)

    def _provider(self, name, package_name, marketplace, contract, internal, implements=True,
                  types=None, decoy=False, contract_version=None):
        if contract_version is None:
            contract_version = 2 if contract == 'write-doc/write-doc' else 1
        package = self.base/name/'plugins'
        entry = package/'playbooks/pb'
        self._skill(entry, 'entry-skill')
        self._entry_scripts(entry, resolver=False)
        (entry/'playbook.yml').write_text(
            'version: 2\nname: pb\ndescription: fixture\ninstructions: {execution: {directive: fixture}}\n'
            f'requires: [{{plugin: {internal}, marketplace: {marketplace}}}]\n'
            'steps: [{id: work, skill: internal-skill, purpose: fixture}]\n')
        self._manifest(entry, {'name': 'pb', 'version': '1.0.0'})
        component = package/'skills/internal'
        self._skill(component, 'internal-skill')
        self._manifest(component, {'name': internal, 'version': '1.0.0'})
        harness = {'installationSurface': 'playbook-package', 'marketplace': marketplace,
                   'entryRoot': './playbooks/pb', 'playbooks': {'pb': './playbooks/pb'},
                   'internalPlugins': {internal: './skills/internal'},
                   'contractVersion': contract_version}
        if decoy:
            # entryRoot は契約と別の playbook を指す。契約が選んだ入口が勝たねばならない。
            decoy_root = package/'playbooks/decoy'
            self._skill(decoy_root, 'decoy-entry')
            self._entry_scripts(decoy_root, resolver=False)
            (decoy_root/'playbook.yml').write_text(
                'version: 2\nname: decoy\ndescription: fixture\n'
                'instructions: {execution: {directive: fixture}}\nrequires: []\n'
                'steps: [{id: work, purpose: fixture, skill: decoy-entry}]\n')
            self._manifest(decoy_root, {'name': 'decoy', 'version': '1.0.0'})
            harness['entryRoot'] = './playbooks/decoy'
            harness['playbooks']['decoy'] = './playbooks/decoy'
        if implements:
            declaration = {'id': contract, 'version': contract_version,
                           'kind': 'playbook', 'playbook': 'pb'}
            if types is not None:
                declaration['types'] = types
            harness['implements'] = [declaration]
        self._manifest(package, {'name': package_name, 'version': '1.0.0',
                                 'skills': ['./playbooks/pb'], 'metadata': {'harness': harness}})
        return package

    def _direct_provider(self, name='direct-provider', contract='write-doc/write-doc',
                         contract_version=2, plugin='write-doc', marketplace='write-doc'):
        """直接呼び出す公開入口。旧runtime scriptを持たない。"""
        package = self.base/name/'plugins'
        entry = package/f'playbooks/{plugin}'
        self._skill(entry, plugin)
        (entry/'playbook.yml').write_text(
            f'version: 2\nname: {plugin}\ndescription: fixture\n'
            'instructions: {execution: {directive: fixture}}\nrequires: []\n'
            'steps: [{id: author, skill: author-document, purpose: fixture}]\n')
        harness = {
            'installationSurface': 'playbook-package',
            'marketplace': marketplace,
            'entryRoot': f'./playbooks/{plugin}',
            'playbooks': {plugin: f'./playbooks/{plugin}'},
            'internalPlugins': {},
            'contractVersion': contract_version,
            'implements': [{
                'id': contract, 'version': contract_version,
                'kind': 'playbook', 'playbook': plugin,
            }],
        }
        if contract == 'write-doc/write-doc':
            harness['implements'][0]['types'] = ['north-star']
        self._manifest(package, {
            'name': plugin, 'version': '7.0.0',
            'skills': [f'./playbooks/{plugin}'], 'metadata': {'harness': harness},
        })
        return package

    def _consumer(self, steps, requires, name='consumer'):
        package = self.base/name/'plugins'
        entry = package/'playbooks/demo'
        self._skill(entry, 'demo')
        self._entry_scripts(entry)
        (entry/'playbook.yml').write_text(
            'version: 2\nname: demo\ndescription: fixture\ndocument_type: north-star\n'
            'instructions: {execution: {directive: fixture}}\n'
            'requires:\n' + ''.join(f'  - {{plugin: {p}, marketplace: {m}}}\n' for p, m in requires) +
            'steps:\n' + steps)
        self._manifest(entry, {'name': 'demo', 'version': '1.0.0'})
        component = package/'skills/local'
        self._skill(component, 'do-local')
        self._manifest(component, {'name': 'local-tool', 'version': '1.0.0'})
        self._manifest(package, {'name': 'demo-pack', 'version': '1.0.0', 'skills': ['./playbooks/demo'],
                                 'metadata': {'harness': {
                                     'installationSurface': 'playbook-package', 'marketplace': 'demo',
                                     'entryRoot': './playbooks/demo', 'playbooks': {'demo': './playbooks/demo'},
                                     'internalPlugins': {'local-tool': './skills/local'},
                                     'contractVersion': 1}}})
        return entry

    def _devmap(self, mapping):
        path = self.base/'devmap.json'
        path.write_text(json.dumps({'schema': 1, 'dependencies': {k: str(v) for k, v in mapping.items()}}))
        return path

    def test_internal_dependency_is_not_classified_external(self):
        """write-doc → content-types 相当。所属宣言ベースの判定が内部を内部と見る。"""
        resolver = ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists(): self.skipTest('no playbook resolver')
        package = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
        entry = package/'playbooks/pb'
        (entry/'scripts/resolve-dependency.py').write_bytes(resolver.read_bytes())
        result = self.call('python3', entry/'scripts/resolve-dependency.py',
                           '--plugin-root', entry, '--plugin', 'content-types', '--marketplace', 'write-doc')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['dependency_scope'], 'internal')
        self.assertEqual(payload['source_kind'], 'repository')

    def test_contract_v2_direct_provider_needs_no_legacy_runtime_scripts(self):
        """v2はSKILL.mdとplaybook.ymlだけで解決し、v1の必須script境界は維持する。"""
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._direct_provider()
        devmap = self._devmap({'write-doc/write-doc': provider})
        entry = self._consumer(
            '  - {id: document, playbook: write-doc, purpose: fixture,\n'
            '     input: {document_type: north-star}, provides: [status, path, reason]}\n',
            [('local-tool', 'demo'), ('write-doc', 'write-doc')], name='consumer-write')
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap),
                           XDG_CONFIG_HOME=str(self.base/'config'))
        accepted = subprocess.run(
            ['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer-write')],
            text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn('contract_version: 2', accepted.stdout)
        self.assertIn('entry: ' + str(provider/'playbooks/write-doc/SKILL.md'), accepted.stdout)

        # 契約v1でも直接呼び出しを宣言したgrillは旧runtime scriptを要求しない。
        grill = self._direct_provider(
            name='direct-grill', contract='grill/grill', contract_version=1,
            plugin='grill', marketplace='grill')
        self._devmap({'grill/grill': grill})
        grill_entry = self._consumer(
            '  - {id: settle, playbook: grill, purpose: fixture, '
            'provides: [decisions, open_questions]}\n',
            [('local-tool', 'demo'), ('grill', 'grill')], name='consumer-grill')
        grill_accepted = subprocess.run(
            ['bash', str(grill_entry/'scripts/resolve.sh'), str(self.base/'consumer-grill')],
            text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(grill_accepted.returncode, 0, grill_accepted.stderr)
        self.assertIn('entry: ' + str(grill/'playbooks/grill/SKILL.md'), grill_accepted.stdout)
        # 旧版のwrite-doc実装へは後方互換で倒さない。
        old_write_doc = self._provider(
            'old-write-doc', 'write-doc', 'write-doc', 'write-doc/write-doc',
            'content-types', contract_version=1)
        self._devmap({'write-doc/write-doc': old_write_doc})
        old_rejected = subprocess.run(
            ['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer-write')],
            text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(old_rejected.returncode, 2)
        self.assertIn('external-dependency-no-playbook', old_rejected.stderr)
        self._devmap({'write-doc/write-doc': provider})

        # 反例: v2でも公開入口そのものは必須。
        skill = provider/'playbooks/write-doc/SKILL.md'
        original = skill.read_text()
        skill.unlink()
        rejected = subprocess.run(
            ['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer-write')],
            text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('implements-entry-missing', rejected.stderr)
        skill.write_text(original)

        # 境界例: v1 providerから旧runtime scriptを省くことはできない。
        legacy = self._provider('legacy-provider', 'legacy-doc', 'legacy-docs',
                                'legacy-docs/legacy-doc', 'legacy-worker', contract_version=1)
        (legacy/'playbooks/pb/scripts/prepare.sh').unlink()
        resolver = entry/'scripts/resolve-dependency.py'
        legacy_check = subprocess.run(
            ['python3', str(resolver), '--plugin-root', str(entry),
             '--plugin', 'legacy-doc', '--marketplace', 'legacy-docs'],
            text=True, capture_output=True,
            env=dict(environment, HARNESS_PLUGIN_DEV_ROOTS=str(
                self._devmap({'legacy-docs/legacy-doc': legacy}))), timeout=60)
        self.assertEqual(legacy_check.returncode, 2)
        self.assertIn('implements-entry-missing', legacy_check.stderr)

        # 契約版とimplements版が食い違う宣言は受け付けない。
        manifest = provider/'.codex-plugin/plugin.json'
        data = json.loads(manifest.read_text())
        data['metadata']['harness']['implements'][0]['version'] = 1
        manifest.write_text(json.dumps(data))
        self._devmap({'write-doc/write-doc': provider})
        mismatch = subprocess.run(
            ['python3', str(resolver), '--plugin-root', str(entry),
             '--plugin', 'write-doc', '--marketplace', 'write-doc'],
            text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn('implements-version-invalid', mismatch.stderr)

    def test_internal_search_works_when_plugin_name_differs_from_marketplace(self):
        """stub-docs/stub-write-doc のように実体名 ≠ marketplace 名でも内部探索が働く。"""
        resolver = ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists(): self.skipTest('no playbook resolver')
        package = self._provider('stub', 'stub-write-doc', 'stub-docs', 'write-doc/write-doc', 'stub-save')
        entry = package/'playbooks/pb'
        (entry/'scripts/resolve-dependency.py').write_bytes(resolver.read_bytes())
        result = self.call('python3', entry/'scripts/resolve-dependency.py',
                           '--plugin-root', entry, '--plugin', 'stub-save', '--marketplace', 'stub-docs')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['dependency_scope'], 'internal')

    def test_external_skill_and_script_steps_are_rejected(self):
        """外部依存は playbook: でしか使えない。when 付きの非活性 step も落とす。"""
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc', 'content-types')
        devmap = self._devmap({'write-doc/write-doc': provider})
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap),
                           XDG_CONFIG_HOME=str(self.base/'config'))
        cases = [
            ('external-dependency-skill',
             '  - {id: work, skill: entry-skill, purpose: fixture, provides: [x]}\n'),
            ('external-dependency-skill',
             '  - {id: work, skill: entry-skill, when: never, purpose: fixture, provides: [x]}\n'),
            ('external-dependency-script',
             '  - {id: work, script: scripts/x.py, plugin: write-doc, purpose: fixture, provides: [x]}\n'),
            ('external-dependency-path',
             '  - {id: work, playbook: write-doc, purpose: fixture, provides: [x],\n'
             '     arguments: ["${.deps[\'write-doc\'].root}/scripts/save.sh"]}\n'),
        ]
        for code, steps in cases:
            entry = self._consumer(steps, [('local-tool', 'demo'), ('write-doc', 'write-doc')])
            result = subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer')],
                                    text=True, capture_output=True, env=environment, timeout=60)
            self.assertEqual(result.returncode, 2, code + ': ' + result.stdout)
            self.assertIn('[error:' + code + ']', result.stderr)

    def test_generic_rule_is_diagnosed_before_distribution_validator(self):
        """固有 validator が落とす設定でも、汎用の依存規則の診断が先に出る。"""
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc',
                                  'content-types')
        devmap = self._devmap({'write-doc/write-doc': provider})
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap),
                           XDG_CONFIG_HOME=str(self.base/'config'))

        def run(steps):
            entry = self._consumer(steps, [('local-tool', 'demo'), ('write-doc', 'write-doc')])
            validator = entry/'scripts/validate-config.sh'
            validator.write_text('#!/usr/bin/env bash\necho "[error] 配布物固有の言い分" >&2\nexit 2\n')
            validator.chmod(0o755)
            return subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer')],
                                  text=True, capture_output=True, env=environment, timeout=60)

        # 規則違反は、固有 validator が何を言おうと汎用の error code で観測できる。
        violation = run('  - {id: work, skill: entry-skill, purpose: fixture, provides: [x]}\n')
        self.assertEqual(violation.returncode, 2, violation.stdout)
        self.assertIn('[error:external-dependency-skill]', violation.stderr)
        self.assertNotIn('配布物固有の言い分', violation.stderr)
        # 規則に適う設定なら、これまでどおり固有 validator まで進んで落ちる。
        allowed = run('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x]}\n')
        self.assertEqual(allowed.returncode, 2, allowed.stdout)
        self.assertIn('配布物固有の言い分', allowed.stderr)

    def test_runtime_follows_cache_root_not_ambient_environment(self):
        """shell に CODEX_HOME があるだけで、claude の cache を codex と呼ばない。"""
        resolver = ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists(): self.skipTest('no playbook resolver')
        homes = {'claude': self.base/'claude-home', 'codex': self.base/'codex-home'}
        environment = dict(self.env, CLAUDE_CONFIG_DIR=str(homes['claude']),
                           CODEX_HOME=str(homes['codex']))
        for name in ('HARNESS_PLUGIN_RUNTIME', 'CLAUDE_PLUGIN_ROOT',
                     'CLAUDE_PLUGIN_CACHE', 'CODEX_PLUGIN_CACHE'):
            environment.pop(name, None)
        for expected, home in homes.items():
            relative = f'{home.name}/plugins/cache/write-doc/write-doc/1.0.0'
            package = self._provider(relative, 'write-doc', 'write-doc',
                                     'write-doc/write-doc', 'content-types')
            entry = package/'playbooks/pb'
            (entry/'scripts/resolve-dependency.py').write_bytes(resolver.read_bytes())
            result = subprocess.run(
                ['python3', str(entry/'scripts/resolve-dependency.py'), '--plugin-root', str(entry),
                 '--plugin', 'content-types', '--marketplace', 'write-doc'],
                text=True, capture_output=True, env=environment, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['runtime'], expected, result.stdout)

    def test_playbook_step_input_is_recorded_with_resolved_values(self):
        """静的scalarだけを補助表示し、完全なprovider入力と誤認させない。

        正本: playbook.ymlの静的propertyとsteps[].needs/provides。
        入力: 文字列literal/scalar property、実行時placeholder、非scalar値/property。
        正規化: ${.property}の静的scalarだけをlookupし、object/listを再帰解決しない。
        合格述語: 静的scalarだけがinput_resolvedに現れ、元inputは全型を保持する。
        診断: explainで未解決と解決対象外を区別し、完全inputではないと明示する。
        正例: nameとdocument_type。反例: ${output_directory}を解決済みとして掲載。
        境界例: 空配列、typed objectとそのproperty参照、false/0、${.missing}。
        意味評価: 実行時にneedsからどの値を組み立てるかは同じagentが判断する。
        """
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc',
                                  'content-types', types=['north-star'])
        devmap = self._devmap({'write-doc/write-doc': provider})
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap),
                           XDG_CONFIG_HOME=str(self.base/'config'))
        entry = self._consumer(
            '  - {id: work, playbook: write-doc, purpose: fixture, provides: [x],\n'
            '     input: {document_type: "${.document_type}", name: fixture.md, at: "${.missing}",\n'
            '             output_directory: "${output_directory}", material: "prefix-${final_markdown}",\n'
            '             questions: [], typed_material: {kind: text, content: fixture}, enabled: false, count: 0,\n'
            '             questions_property: "${.questions}", typed_material_property: "${.typed_material}"}}\n'
            '  - {id: local, skill: do-local, purpose: fixture, needs: [x],\n'
            '     input: {document_type: "${.document_type}"}}\n',
            [('local-tool', 'demo'), ('write-doc', 'write-doc')])
        playbook = entry/'playbook.yml'
        playbook.write_text(playbook.read_text().replace(
            'document_type: north-star\n',
            'document_type: north-star\nquestions: []\ntyped_material: {kind: text, content: fixture}\n',
            1))
        result = subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer')],
                                text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        steps = {step['id']: step for step in json.loads(
            subprocess.run(['yq', '-o=json', '-I=0', '.playbook.steps'], input=result.stdout,
                           text=True, capture_output=True, timeout=30).stdout)}
        # 参照形は残す。解けた分だけを併記し、解けない参照は載せない。
        self.assertEqual(steps['work']['input']['document_type'], '${.document_type}')
        self.assertEqual(steps['work']['input']['questions'], [])
        self.assertEqual(steps['work']['input']['typed_material'],
                         {'kind': 'text', 'content': 'fixture'})
        self.assertIs(steps['work']['input']['enabled'], False)
        self.assertEqual(steps['work']['input']['count'], 0)
        self.assertEqual(steps['work']['input_resolved'],
                         {'document_type': 'north-star', 'name': 'fixture.md'})
        for key in ('output_directory', 'material', 'questions', 'typed_material',
                    'enabled', 'count', 'questions_property', 'typed_material_property'):
            self.assertNotIn(key, steps['work']['input_resolved'])
        resolved_json = subprocess.run(
            ['yq', '-o=json', '-I=0', '.'], input=result.stdout,
            text=True, capture_output=True, timeout=30, check=True).stdout
        explanation = subprocess.run(
            ['python3', str(entry/'scripts/resolve-dependency.py'), '--explain-config'],
            input=resolved_json, text=True, capture_output=True, env=environment, timeout=30)
        self.assertEqual(explanation.returncode, 0, explanation.stderr)
        self.assertIn('providerへ渡す完全inputではない', explanation.stdout)
        self.assertIn('input.output_directory = (未解決・実行時)', explanation.stdout)
        self.assertIn('input.material = (未解決・実行時)', explanation.stdout)
        self.assertIn('input.questions = (解決対象外・inputに保持)', explanation.stdout)
        self.assertIn('input.typed_material = (解決対象外・inputに保持)', explanation.stdout)
        self.assertIn('input.questions_property = (解決対象外・inputに保持)', explanation.stdout)
        self.assertIn('input.typed_material_property = (解決対象外・inputに保持)', explanation.stdout)
        # playbook: 以外の step は対象外。
        self.assertNotIn('input_resolved', steps['local'])

    def test_bindings_reject_bad_key_missing_implements_and_drift(self):
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc',
                                  'content-types', types=['north-star'])
        naked = self._provider('naked', 'naked-doc', 'naked-docs', 'write-doc/write-doc',
                               'naked-save', implements=False)
        devmap = self._devmap({'write-doc/write-doc': provider, 'naked-docs/naked-doc': naked})
        config = self.base/'config'
        (config/'harness-plugins').mkdir(parents=True, exist_ok=True)
        bindings = config/'harness-plugins/dependencies.yml'
        entry = self._consumer(
            '  - {id: work, playbook: write-doc, purpose: fixture, provides: [x],\n'
            '     input: {document_type: "${.document_type}"}}\n',
            [('local-tool', 'demo'), ('write-doc', 'write-doc')])
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap), XDG_CONFIG_HOME=str(config))

        def resolve(*extra):
            return subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer'), *extra],
                                  text=True, capture_output=True, env=environment, timeout=60)

        bindings.write_text('version: 1\nbindings:\n  "write-doc": {marketplace: naked-docs, plugin: naked-doc}\n')
        self.assertIn('[error:binding-key-invalid]', resolve().stderr)
        bindings.write_text('version: 1\nbindings:\n  "write-doc/write-doc": {marketplace: naked-docs, plugin: naked-doc, path: /tmp}\n')
        self.assertIn('[error:binding-schema]', resolve().stderr)
        bindings.write_text('version: 1\nbindings:\n  "write-doc/write-doc": {marketplace: naked-docs, plugin: naked-doc}\n')
        self.assertIn('[error:binding-not-implemented]', resolve().stderr)
        bindings.write_text('version: 1\nbindings:\n  "demo/local-tool": {marketplace: demo, plugin: local-tool}\n')
        self.assertIn('[error:binding-internal-dependency]', resolve().stderr)
        bindings.unlink()
        first = resolve()
        self.assertEqual(first.returncode, 0, first.stderr)
        lock = re.search(r'bindings_lock: (\S+)', first.stdout).group(1)
        (provider/'playbooks/pb/SKILL.md').write_text('---\nname: entry-skill\ndescription: changed\n---\nchanged\n')
        self.assertIn('[error:binding-drift]', resolve('--bindings=' + lock).stderr)


    def test_contract_input_normalizes_paths_and_delegates_schema(self):
        """--input は symlink 越しの一時領域を受け、契約固有schemaは validate-input.sh へ委譲する。"""
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        package = self._provider('provider', 'legacy-doc', 'legacy-docs',
                                 'legacy-docs/legacy-doc', 'content-types',
                                 types=['north-star'], contract_version=1)
        entry = package/'playbooks/pb'
        self._entry_scripts(entry, resolver=True)
        # macOS 既定の TMPDIR と同じ形（祖先が symlink）を作る。
        real = self.base/'real-tmp'; real.mkdir()
        linked = self.base/'tmp'; linked.symlink_to(real, target_is_directory=True)
        output = real/'out.yml'
        payload = linked/'input.yml'
        payload.write_text('contract: legacy-docs/legacy-doc\nversion: 1\n'
                           'document_type: north-star\n'
                           f'output_to: {linked}/out.yml\n')
        environment = dict(self.env, XDG_CONFIG_HOME=str(self.base/'config'))

        def resolve():
            return subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base),
                                   '--input=' + str(payload)],
                                  text=True, capture_output=True, env=environment, timeout=60)

        first = resolve()
        self.assertEqual(first.returncode, 0, first.stderr)
        # 正規化後の絶対 path が載る。symlink 越しの綴りは残さない。
        self.assertIn('input_file: ' + str(real/'input.yml'), first.stdout)
        self.assertIn('output_to: ' + str(output), first.stdout)
        self.assertIn('document_type: north-star', first.stdout)

        # 契約固有 schema は provider の hook が拒否する。
        validator = entry/'scripts/validate-input.sh'
        validator.write_text('#!/usr/bin/env bash\necho "[error:input-schema] key=unknown" >&2\nexit 3\n')
        validator.chmod(0o755)
        rejected = resolve()
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('[error:input-schema]', rejected.stderr)
        self.assertIn('key=unknown', rejected.stderr)
        # hook が渡された path は正規化済みで、hook 自身が読める。
        validator.write_text('#!/usr/bin/env bash\ntest -f "$1" && grep -q "^contract:" "$1"\n')
        self.assertEqual(resolve().returncode, 0)
        # symlink の hook は実行しない。
        validator.unlink(); validator.symlink_to(self.base/'absent.sh')
        self.assertEqual(resolve().returncode, 0)
        validator.unlink()
        # 正規化しても能力検査は効く。
        payload.write_text('contract: legacy-docs/legacy-doc\nversion: 1\ndocument_type: strategy\n')
        self.assertIn('[error:input-capability-unsupported]', resolve().stderr)
        # 実在しない入力は正規化後に落ちる。
        payload.unlink()
        self.assertIn('[error:input-file-unsafe]', resolve().stderr)


    def test_external_root_follows_contract_not_entry_root(self):
        """外部依存の root は entryRoot ではなく契約が選んだ playbook である。"""
        resolver = ROOT/'shared/playbook/resolve-dependency.py'
        if not resolver.exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc',
                                  'content-types', decoy=True)
        devmap = self._devmap({'write-doc/write-doc': provider})
        entry = self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x]}\n',
                               [('local-tool', 'demo'), ('write-doc', 'write-doc')])
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap),
                           XDG_CONFIG_HOME=str(self.base/'config'))
        result = subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer')],
                                text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('root: ' + str(provider/'playbooks/pb'), result.stdout)
        self.assertNotIn(str(provider/'playbooks/decoy'), result.stdout)
        self.assertIn('entry: ' + str(provider/'playbooks/pb/SKILL.md'), result.stdout)
        self.assertIn('entry_skill: entry-skill', result.stdout)

    def test_external_reference_forms_are_parsed_once_and_narrowed(self):
        """ブラケット形・引用形・skills 形・依存先設定参照をすべて拒否する。"""
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc',
                                  'content-types')
        devmap = self._devmap({'write-doc/write-doc': provider})
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap),
                           XDG_CONFIG_HOME=str(self.base/'config'))

        def run(reference):
            steps = ('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x],\n'
                     '     arguments: ["' + reference + '"]}\n')
            entry = self._consumer(steps, [('local-tool', 'demo'), ('write-doc', 'write-doc')])
            return subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer')],
                                  text=True, capture_output=True, env=environment, timeout=60)

        rejected = [
            ('external-dependency-path', '${.deps[\'write-doc\'][\'root\']}/references/private.md'),
            ('external-dependency-path', '${.deps.\'write-doc\'.root}/../../LICENSE'),
            ('external-dependency-path', '${.deps.write-doc.skills.write-doc}'),
            ('external-dependency-path', '${.deps.write-doc.package_root}'),
            ('external-dependency-path', '${.deps.write-doc.entry}/../x'),
            ('external-dependency-config', '${write-doc:.private.setting}'),
        ]
        for code, reference in rejected:
            result = run(reference)
            self.assertEqual(result.returncode, 2, reference)
            self.assertIn('[error:' + code + ']', result.stderr, reference)
        for reference in ['${.deps.write-doc.root}/scripts/prepare.sh',
                          '${.deps[\'write-doc\'].root}/playbook.yml',
                          '${.deps.write-doc.entry}',
                          '${doc-render:.output.format}']:
            self.assertEqual(run(reference).returncode, 0, reference)

    def test_lock_snapshot_and_state_identity_survive_child_appends(self):
        """入口が束縛を snapshot し、子の追記が親の状態管理を壊さない。"""
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc',
                                  'content-types')
        other = self._provider('other', 'other-doc', 'other-docs', 'write-doc/write-doc', 'other-save')
        devmap = self._devmap({'write-doc/write-doc': provider, 'other-docs/other-doc': other})
        config = self.base/'config'; (config/'harness-plugins').mkdir(parents=True, exist_ok=True)
        bindings = config/'harness-plugins/dependencies.yml'
        bindings.write_text('version: 1\nbindings:\n'
                            '  "write-doc/write-doc": {marketplace: other-docs, plugin: other-doc}\n')
        entry = self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [path]}\n',
                               [('local-tool', 'demo'), ('write-doc', 'write-doc')])
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap), XDG_CONFIG_HOME=str(config))
        resolved = self.base/'resolved.yml'
        with resolved.open('w') as handle:
            first = subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer')],
                                   stdout=handle, text=True, stderr=subprocess.PIPE,
                                   env=environment, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)
        lock = Path(re.search(r'bindings_lock: (\S+)', resolved.read_text()).group(1))
        snapshot = json.loads(lock.read_text())
        self.assertEqual(snapshot['bindings']['write-doc/write-doc'],
                         {'marketplace': 'other-docs', 'plugin': 'other-doc'})

        # 元ファイルを書き換えても、子は snapshot の実体を使う。
        bindings.write_text('version: 1\nbindings:\n'
                            '  "write-doc/write-doc": {marketplace: write-doc, plugin: write-doc}\n')
        child = subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer'),
                                '--bindings=' + str(lock)],
                               text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('plugin: other-doc', child.stdout)

        # 子が新しい契約を lock へ追記しても、親の state identity は変わらない。
        state = ROOT/'shared/playbook/state.py'
        def call(command, *extra):
            return self.call('python3', state, command, '--config', resolved,
                             '--run-id', 'run', *extra)
        started = call('init', '--repo', self.base/'consumer')
        self.assertEqual(started.returncode, 0, started.stderr)
        appended = json.loads(lock.read_text())
        appended['entries']['grill/grill'] = {
            'marketplace': 'grill', 'plugin': 'grill', 'version': '1.0.0',
            'root': str(self.base), 'package_root': str(self.base),
            'content_hash': 'deadbeef', 'source_kind': 'dev-map'}
        lock.write_text(json.dumps(appended))
        self.assertEqual(call('status').returncode, 0, call('status').stderr)
        self.assertEqual(call('start', '--step', 'work').returncode, 0)
        # 自分の契約の実体が変われば、これまでどおり再開を拒否する。
        mutated = json.loads(lock.read_text())
        mutated['entries']['write-doc/write-doc']['content_hash'] = 'changed'
        lock.write_text(json.dumps(mutated))
        self.assertEqual(call('status').returncode, 2)

    def _diagnostic_repo(self, name):
        """doctor が最低限読む repository。marketplace 検査は別 test の領分。"""
        repo = self.base/name
        (repo/'scripts').mkdir(parents=True, exist_ok=True)
        (repo/'scripts/validate.sh').write_text('#!/bin/bash\nexit 0\n')
        return repo

    def test_doctor_skips_skills_that_require_an_override(self):
        """既定値の無い required な prompt parameter を持つ skill は、実行せず skip する。"""
        repo = self._diagnostic_repo('diagnostic')
        cases = [
            ('needs-override', 'version: 1\nprompt_parameters:\n  method:\n    type: string\n    required: true\n'),
            ('needs-nested', 'version: 1\nprompt_parameters:\n  database:\n    product: {type: string, required: true}\n    version: {type: string, required: true}\n'),
            ('has-default', 'version: 1\nprompt_parameters:\n  depth:\n    type: string\n    required: true\n    default: shallow\n'),
        ]
        for name, defaults in cases:
            plugin = repo/'plugins'/name
            (plugin/'config').mkdir(parents=True)
            (plugin/'config/defaults.yml').write_text(defaults)
            (plugin/'scripts').mkdir()
            (plugin/'scripts/resolve.sh').write_text(
                '#!/usr/bin/env bash\ntouch "' + str(self.base/'executed') + '.' + name + '"\nexit 2\n')
        result = self.call('python3', ROOT/'scripts/doctor.py', '--repository', repo, '--repo', repo)
        checks = {check['check']: check for check in json.loads(result.stdout)['checks']}
        skipped = checks['resolve:plugins/needs-override/scripts/resolve.sh']
        self.assertTrue(skipped['ok'], skipped)
        self.assertEqual(skipped['skipped'], 'requires-override')
        self.assertEqual(skipped['requires_override'], ['method'])
        self.assertEqual(checks['resolve:plugins/needs-nested/scripts/resolve.sh']['requires_override'],
                         ['database.product', 'database.version'])
        # skip は「実行して落ちた」ではない。resolve.sh は起動していない。
        self.assertFalse((self.base/'executed.needs-override').exists())
        self.assertFalse((self.base/'executed.needs-nested').exists())
        # 既定値があるなら上書きは要らない。これまでどおり実行し、結果で判断する。
        executed = checks['resolve:plugins/has-default/scripts/resolve.sh']
        self.assertFalse(executed['ok'])
        self.assertTrue((self.base/'executed.has-default').exists())

    def test_doctor_resolves_playbooks_against_sibling_checkouts(self):
        """playbook の解決は fixture ではなく、兄弟 checkout の実配布物に対して行う。"""
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._provider('write-doc-plugins', 'write-doc', 'write-doc',
                                  'write-doc/write-doc', 'content-types')
        self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x]}\n',
                       [('local-tool', 'demo'), ('write-doc', 'write-doc')])
        repo = self._diagnostic_repo('consumer')
        environment = dict(self.env, XDG_CONFIG_HOME=str(self.base/'config'))
        environment.pop('HARNESS_PLUGIN_DEV_ROOTS', None)
        environment.pop('HARNESS_PLUGIN_REAL_ROOTS', None)

        def diagnose(extra=None):
            result = subprocess.run(
                ['python3', str(ROOT/'scripts/doctor.py'), '--repository', str(repo), '--repo', str(repo)],
                text=True, capture_output=True, env=dict(environment, **(extra or {})), timeout=120)
            return {check['check']: check for check in json.loads(result.stdout)['checks']}

        label = 'resolve:plugins/playbooks/demo/scripts/resolve.sh'
        checks = diagnose()
        resolved = checks[label]
        self.assertTrue(resolved['ok'], resolved.get('detail'))
        self.assertTrue(any('[外部] write-doc → write-doc/write-doc' in line
                            for line in resolved['dependencies']), resolved['dependencies'])
        self.assertEqual(checks['dependency-bindings']['real_distribution'],
                         {'source': 'sibling-checkout',
                          'dependencies': {'write-doc/write-doc': str(provider)}})
        # 兄弟が無ければ、fixture へ倒さず理由付きで NG にする。
        absent = diagnose({'HARNESS_PLUGIN_SIBLING_ROOT': str(self.base/'nowhere')})
        self.assertFalse(absent[label]['ok'])
        self.assertIn('write-doc/write-doc', absent[label]['detail'])
        self.assertFalse(absent['dependency-bindings']['ok'])

    def test_internal_skill_reference_must_name_an_existing_skill(self):
        """内部依存への .skills.<名前> は、解決結果に実在する名前だけを許す。"""
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc',
                                  'content-types')
        devmap = self._devmap({'write-doc/write-doc': provider})
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap),
                           XDG_CONFIG_HOME=str(self.base/'config'))

        def run(reference):
            steps = ('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x],\n'
                     '     arguments: ["' + reference + '"]}\n')
            entry = self._consumer(steps, [('local-tool', 'demo'), ('write-doc', 'write-doc')])
            return subprocess.run(['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer')],
                                  text=True, capture_output=True, env=environment, timeout=60)

        allowed = run('${.deps.local-tool.skills.do-local}')
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        for reference in ['${.deps.local-tool.skills.do-remote}',
                          "${.deps['local-tool'].skills['do-remote']}",
                          '${.deps.local-tool.skills}']:
            rejected = run(reference)
            self.assertEqual(rejected.returncode, 2, reference)
            self.assertIn('[error:internal-skill-unknown]', rejected.stderr, reference)
        # lint も同じ関数で同じ判定をする。違うのは playbook.yml の外まで見ることだけ。
        entry = self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x]}\n',
                               [('local-tool', 'demo'), ('write-doc', 'write-doc')])
        (entry/'README.md').write_text('工程は ${.deps.local-tool.skills.do-remote} に従う\n')
        linted = subprocess.run(
            ['python3', str(ROOT/'scripts/lint-consumer-contract.py'), '--repo',
             str(self.base/'consumer'), '--runtime', 'claude', '--json'],
            text=True, capture_output=True, env=environment, timeout=120)
        self.assertEqual(linted.returncode, 1, linted.stdout + linted.stderr)
        findings = json.loads(linted.stdout)['findings']
        self.assertIn('internal-skill-unknown', [finding['code'] for finding in findings])

    def test_explain_names_the_implementation_behind_each_logical_name(self):
        """依存行は論理名と実体を並べ、束縛で実体が変わったときだけ出典層を添える。"""
        if not (ROOT/'shared/playbook/resolve.sh').exists(): self.skipTest('no playbook resolver')
        provider = self._provider('provider', 'write-doc', 'write-doc', 'write-doc/write-doc',
                                  'content-types')
        other = self._provider('other', 'other-doc', 'other-docs', 'write-doc/write-doc', 'other-save')
        devmap = self._devmap({'write-doc/write-doc': provider, 'other-docs/other-doc': other})
        config = self.base/'config'; (config/'harness-plugins').mkdir(parents=True, exist_ok=True)
        entry = self._consumer('  - {id: work, playbook: write-doc, purpose: fixture, provides: [x]}\n',
                               [('local-tool', 'demo'), ('write-doc', 'write-doc')])
        environment = dict(self.env, HARNESS_PLUGIN_DEV_ROOTS=str(devmap), XDG_CONFIG_HOME=str(config))

        def explain():
            result = subprocess.run(
                ['bash', str(entry/'scripts/resolve.sh'), str(self.base/'consumer'), '--explain'],
                text=True, capture_output=True, env=environment, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stderr

        plain = explain()
        self.assertIn('[外部] write-doc → write-doc/write-doc 1.0.0 [', plain)
        self.assertIn('[内部] local-tool → demo/local-tool 1.0.0 [', plain)
        self.assertNotIn('← personal', plain)
        (config/'harness-plugins/dependencies.yml').write_text(
            'version: 1\nbindings:\n'
            '  "write-doc/write-doc": {marketplace: other-docs, plugin: other-doc}\n')
        bound = explain()
        self.assertIn('[外部] write-doc → other-docs/other-doc 1.0.0 [', bound)
        self.assertIn('← personal', bound)

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
