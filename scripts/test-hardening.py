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
        self.assertEqual(len(actions), 3)
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
    def test_all_templates_resolve_from_plugin_root(self):
        plugin=ROOT/'plugins/skills/authoring/content-types'
        if not plugin.exists():self.skipTest('no content types')
        result=self.call('yq','-o=json','.',plugin/'assets/template-examples.yml');pairs=json.loads(result.stdout)['pairs'];self.assertEqual(len(pairs),28)
        for pair in pairs.values():
            for path in pair.values():self.assertTrue((plugin/path).is_file(),path)
    def test_semantic_runner_requires_evidence(self):
        repo=self.base/'eval';(repo/'evals').mkdir(parents=True)
        (repo/'SKILL.md').write_text('---\nname: fixture\ndescription: fixture\n---\nDo the specified job.')
        fixtures=repo/'evals/cases.json';fixtures.write_text(json.dumps({'cases':[{'id':'case','skill':'../SKILL.md','messages':[{'role':'user','content':'request'}],'criteria':[{'id':'meaning','meaning':'a meaningful explanation'}]}]}))
        adapter=repo/'adapter.py';adapter.write_text("import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({'model':r['model'],'output':{'criteria':[{'id':'meaning','pass':True,'quote':'absent','reason':'unsupported'}]} if 'candidate_output' in r else 'actual answer'}))\n")
        command=json.dumps(['python3',str(adapter)]);out=repo/'result.json'
        result=self.call('python3',ROOT/'scripts/evaluate-skills.py','--fixtures',fixtures,'--model-command',command,'--judge-command',command,'--model','generator','--judge-model','judge','--output',out)
        self.assertEqual(result.returncode,1);record=json.loads(out.read_text())['records'][0];self.assertEqual(record['status'],'error');self.assertIn('evidence',record['error']);self.assertIn('judge_input',record);self.assertIn('judgment',record);self.assertEqual(record['judgment']['output']['criteria'][0]['quote'],'absent')
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
        state=ROOT/'plugins/playbooks/authoring/write-doc/scripts/state.py'
        if not state.exists():self.skipTest('no write-doc state')
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

if __name__=='__main__':unittest.main()
