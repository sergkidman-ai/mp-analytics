# поток: rev
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import rev_git_sync as sync


def command(root,*args):
    return subprocess.run(['git',*args],cwd=root,capture_output=True,check=True).stdout.decode().strip()


class GitSyncTests(unittest.TestCase):
    def make_repo(self,tmp):
        root=Path(tmp);command(root,'init','-q');command(root,'config','user.name','Tests');command(root,'config','user.email','tests@example.invalid')
        (root/'app.py').write_text('print(1)\n');command(root,'add','app.py');command(root,'commit','-qm','base')
        base=command(root,'rev-parse','HEAD');command(root,'checkout','-qb',sync.BRANCH)
        return root,base

    def test_audit_finds_environment_even_if_deleted_before_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,base=self.make_repo(tmp)
            (root/'.env').write_text('PLACEHOLDER=test\n');command(root,'add','.env');command(root,'commit','-qm','bad')
            (root/'.env').unlink();command(root,'add','-u');command(root,'commit','-qm','remove')
            report=sync.audit(root,base,command(root,'rev-parse','HEAD'))
            self.assertTrue(report['blocked']);self.assertIn('environment_file',{x['reason'] for x in report['issues']})

    def test_uncommitted_customer_file_is_never_added_or_audited_as_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,base=self.make_repo(tmp)
            (root/'app.py').write_text('print(2)\n');command(root,'add','app.py');command(root,'commit','-qm','fix')
            (root/'customers.csv').write_text('private data\n')
            report=sync.audit(root,base,sync.branch_head(root))
            self.assertFalse(report['blocked']);self.assertEqual(report['outgoing_commits'],1)
            self.assertEqual(command(root,'status','--porcelain'),'?? customers.csv')

    def test_wrong_branch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,_=self.make_repo(tmp);command(root,'checkout','-qb','unrelated')
            with self.assertRaises(sync.SyncError):sync.branch_head(root)

    def test_paths_and_realistic_secret_patterns_are_rejected(self):
        for name in ('.env','backup/db.sql','secrets/x','docs/BRIEF_REV.md','customers.csv','private.pem','state.sqlite'):
            self.assertIsNotNone(sync.path_violation(name),name)
        self.assertIsNone(sync.path_violation('feedback_bot/recovery_runtime.py'))
        self.assertEqual(sync.content_violation(('sk-'+'a'*32).encode()),'credential_pattern')
        self.assertEqual(sync.content_violation(b'-----BEGIN '+b'OPENSSH PRIVATE KEY-----'),'credential_pattern')
        self.assertIsNone(sync.content_violation(b'token = os.getenv("API_TOKEN")'))
        self.assertEqual(sync.content_violation(('OZON_API_KEY = '+repr('abc'+'12345'+'6789')).encode()),'credential_assignment')
        self.assertIsNone(sync.content_violation(b'API_TOKEN = "replace_me_here"'))

    def test_push_is_fixed_sha_branch_and_does_not_follow_tags(self):
        head='a'*40;base='b'*40;calls=[]
        def fake_git(root,*args,**kwargs):
            calls.append(args)
            if args[0]=='config':return subprocess.CompletedProcess(args,1,b'',b'')
            if args[0]=='ls-remote':
                out=(base+'\trefs/heads/main\n') if sum(x[0]=='ls-remote' for x in calls)==1 else (head+'\trefs/heads/'+sync.BRANCH+'\n')
                return subprocess.CompletedProcess(args,0,out.encode(),b'')
            return subprocess.CompletedProcess(args,0,b'',b'')
        with tempfile.TemporaryDirectory() as tmp,patch.object(sync,'KEYDIR',Path(tmp)),patch.object(sync,'branch_head',return_value=head),patch.object(sync,'ssh_env',return_value={}),patch.object(sync,'audit',return_value={'blocked':False,'base_is_ancestor':True,'outgoing_commits':1,'outgoing_blobs_checked':1}),patch.object(sync,'git',side_effect=fake_git):
            self.assertTrue(sync.sync(tmp)['remote_confirmed'])
        push=[x for x in calls if x[0]=='push'];self.assertEqual(len(push),1)
        self.assertEqual(push[0][-2:],(sync.REPOSITORY,head+':refs/heads/'+sync.BRANCH))
        self.assertIn('--no-follow-tags',push[0]);self.assertFalse(any('force' in arg for arg in push[0]))
        self.assertFalse(any(x[0] in ('add','commit') for x in calls))

    def test_export_overlays_public_main_keeps_dependencies_and_ignores_dirty_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,oldbase=self.make_repo(tmp)
            (root/'core').mkdir();(root/'core/catalog.py').write_text('catalog = 1\n')
            command(root,'add','core');command(root,'commit','-qm','public dependency');base=command(root,'rev-parse','HEAD')
            (root/'feedback_bot').mkdir();(root/'feedback_bot/recovery_runtime.py').write_text('answer = 1\n')
            (root/'docs').mkdir();(root/'docs/BRIEF_REV.md').write_text('private operator memory')
            (root/'reports').mkdir();(root/'reports/inventory.py').write_text('unrelated flow')
            command(root,'add','.');command(root,'commit','-qm','mixed source')
            source=command(root,'rev-parse','HEAD')
            (root/'feedback_bot/recovery_runtime.py').write_text('answer = 999\n')
            status=command(root,'status','--porcelain')
            plan=sync.build_export_tree(root,source,base)
            self.assertFalse(plan['blocked']);self.assertEqual([x['path'] for x in plan['changes']],['feedback_bot/recovery_runtime.py'])
            self.assertEqual(command(root,'show',plan['tree']+':core/catalog.py'),'catalog = 1')
            self.assertEqual(command(root,'show',plan['tree']+':feedback_bot/recovery_runtime.py'),'answer = 1')
            names=command(root,'ls-tree','-r','--name-only',plan['tree']).splitlines()
            self.assertNotIn('docs/BRIEF_REV.md',names);self.assertNotIn('reports/inventory.py',names)
            self.assertEqual(command(root,'status','--porcelain'),status)
            self.assertEqual(command(root,'rev-parse','HEAD'),source)

    def test_private_provenance_markdown_is_excluded_from_public_export(self):
        for name in ('docs/rev/brand_notes_2026-09-21.md',
                     'docs/rev/recovered_manual_rules_2026-11-04.md',
                     'docs/rev/nested/manual_rules.md',
                     'docs/handoff/archive/rev_2026-09-10.md','docs/reports/rev_gold_set.md'):
            self.assertFalse(sync.export_allowed(name),name)
        self.assertTrue(sync.export_allowed('docs/rev/RECOVERY_RUNBOOK.md'))
        with tempfile.TemporaryDirectory() as tmp:
            root,base=self.make_repo(tmp);notes=root/'docs/rev';notes.mkdir(parents=True)
            (notes/'brand_notes_2026-09-21.md').write_text('private expert provenance')
            (notes/'RECOVERY_RUNBOOK.md').write_text('public operating instructions')
            command(root,'add','.');command(root,'commit','-qm','private and public documentation')
            plan=sync.build_export_tree(root,command(root,'rev-parse','HEAD'),base)
            self.assertFalse(plan['blocked'])
            self.assertEqual([entry['path'] for entry in plan['changes']],['docs/rev/RECOVERY_RUNBOOK.md'])
            self.assertNotIn('docs/rev/brand_notes_2026-09-21.md',command(root,'ls-tree','-r','--name-only',plan['tree']).splitlines())

    def test_export_includes_both_recovery_dependencies(self):
        for name in ('reports/rev_archive_fact_recovery.py','reports/rev_delivery_recovery.py'):
            self.assertTrue(sync.export_allowed(name),name)
        self.assertFalse(sync.export_allowed('reports/inventory.py'))

    def test_export_audits_committed_payload_before_tree_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,base=self.make_repo(tmp);(root/'feedback_bot').mkdir()
            (root/'feedback_bot/example.py').write_text('API_KEY = '+repr('sk-'+'a'*32))
            command(root,'add','.');command(root,'commit','-qm','secret fixture')
            plan=sync.build_export_tree(root,command(root,'rev-parse','HEAD'),base)
            self.assertTrue(plan['blocked']);self.assertNotIn('tree',plan)

    def test_export_commit_extends_remote_deploy_head_and_pins_source_message(self):
        source='a'*40;base='b'*40;parent='c'*40;tree='d'*40;commit='e'*40;calls=[]
        def fake_git(root,*args,**kwargs):
            calls.append(args)
            if args[0]=='config':return subprocess.CompletedProcess(args,1,b'',b'')
            if args[0]=='ls-remote':
                n=sum(x[0]=='ls-remote' for x in calls)
                data=(base+'\trefs/heads/main\n'+parent+'\trefs/heads/'+sync.DEPLOY_BRANCH+'\n') if n==1 else commit+'\trefs/heads/'+sync.DEPLOY_BRANCH+'\n'
                return subprocess.CompletedProcess(args,0,data.encode(),b'')
            if args[0]=='rev-parse':return subprocess.CompletedProcess(args,0,b'other-tree\n',b'')
            return subprocess.CompletedProcess(args,0,b'',b'')
        with tempfile.TemporaryDirectory() as tmp,patch.object(sync,'KEYDIR',Path(tmp)),patch.object(sync,'branch_head',return_value=source),patch.object(sync,'ssh_env',return_value={}),patch.object(sync,'git',side_effect=fake_git),patch.object(sync,'build_export_tree',return_value={'blocked':False,'tree':tree,'change_count':1}),patch.object(sync,'run',return_value=subprocess.CompletedProcess([],0,(commit+'\n').encode(),b'')) as run:
            self.assertTrue(sync.export(tmp)['remote_confirmed'])
        args,kwargs=run.call_args
        self.assertIn(parent,args[0]);self.assertIn(source,kwargs['input_data'].decode())
        push=[x for x in calls if x[0]=='push'][0]
        self.assertEqual(push[-1],commit+':refs/heads/'+sync.DEPLOY_BRANCH)
        self.assertFalse(any('force' in arg for arg in push))

    def test_mismatched_https_host_key_stops_before_key_generation(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return json.dumps({'ssh_keys':['ssh-ed25519 '+base64.b64encode(b'wrong').decode()]}).encode()
        with tempfile.TemporaryDirectory() as tmp,patch.object(sync,'KEYDIR',Path(tmp)),patch.object(sync.urllib.request,'urlopen',return_value=Response()),patch.object(sync,'run') as run:
            with self.assertRaises(sync.SyncError):sync.setup()
            run.assert_not_called()

if __name__=='__main__':unittest.main()
