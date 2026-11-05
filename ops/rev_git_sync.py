#!/usr/bin/env python3
# поток: rev
"""Push committed Rev branch only, with isolated identity and a full outgoing-object audit."""
import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
BRANCH='rev/recovery-20261103'
DEPLOY_BRANCH='rev/recovery-deploy'
REPOSITORY='git@github.com:sergkidman-ai/mp-analytics.git'
KEYDIR=Path('/root/.config/rev-recovery')
KEY=KEYDIR/'github-deploy-ed25519'
KNOWN_HOSTS=KEYDIR/'github-known_hosts'
PUBLIC=Path('/root/rev-recovery/github-deploy-ed25519.pub')
# GitHub's published Ed25519 fingerprint, checked against authenticated HTTPS meta.
GITHUB_ED25519='+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU'
DATA_SUFFIXES={'.dump','.gpg','.sqlite','.sqlite3','.db','.csv','.json','.jsonl','.xlsx','.xls','.zip','.gz','.tar','.pem','.key','.p12','.pfx','.bak'}
PRIVATE_DIRS={'.ssh','.aws','.config','.claude','backups','backup','data','logs','runtime','artifacts','private','secrets','snapshots'}
SECRET_PATTERNS=(
    rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',
    rb'\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b',
    rb'\b(?:sk-ant-api[0-9]+-[A-Za-z0-9_-]{20,}|sk-[a-fA-F0-9]{32,})\b',
    rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})\b',
    rb'(?:postgres(?:ql)?|https?)://[^\s/:]+:[^\s/@]+@',
)

class SyncError(RuntimeError):pass


def run(command,*,root=None,env=None,check=True,input_data=None):
    r=subprocess.run(command,cwd=root,env=env,input=input_data,stdin=subprocess.DEVNULL if input_data is None else None,capture_output=True,timeout=180)
    if check and r.returncode:raise SyncError(Path(command[0]).name+' failed; exit='+str(r.returncode)+' (credentials/output suppressed)')
    return r


def git(root,*args,env=None,check=True):
    environment=dict(env) if env is not None else {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    environment['GIT_TERMINAL_PROMPT']='0'
    return run(['git','--no-replace-objects',*args],root=root,env=environment,check=check)


def fingerprint(key):
    try:payload=base64.b64decode(key.split()[1],validate=True)
    except Exception:raise SyncError('Malformed SSH host key')
    return base64.b64encode(hashlib.sha256(payload).digest()).decode().rstrip('=')


def setup():
    os.umask(0o077);KEYDIR.mkdir(mode=0o700,parents=True,exist_ok=True)
    with urllib.request.urlopen('https://api.github.com/meta',timeout=20) as response:meta=json.load(response)
    keys=[k for k in meta.get('ssh_keys',[]) if k.startswith('ssh-ed25519 ')]
    if len(keys)!=1 or fingerprint(keys[0])!=GITHUB_ED25519 or meta.get('ssh_key_fingerprints',{}).get('SHA256_ED25519')!=GITHUB_ED25519:
        raise SyncError('GitHub host key differs from independently pinned official fingerprint; manual review required')
    expected='github.com '+keys[0]+'\n'
    if KNOWN_HOSTS.exists() and KNOWN_HOSTS.read_text()!=expected:raise SyncError('Existing isolated known_hosts differs; not overwritten')
    if not KNOWN_HOSTS.exists():KNOWN_HOSTS.write_text(expected);KNOWN_HOSTS.chmod(0o600)
    if KEY.exists():
        if KEY.stat().st_mode&0o077:raise SyncError('Existing deploy private key must be 0600')
    else:run(['ssh-keygen','-q','-t','ed25519','-N','','-C','rev-cloud-001-deploy','-f',str(KEY)])
    KEY.chmod(0o600)
    derived=b' '.join(run(['ssh-keygen','-y','-f',str(KEY)]).stdout.split()[:2])+b' rev-cloud-001-deploy\n'
    if PUBLIC.exists() and PUBLIC.read_bytes().split()[:2]!=derived.split()[:2]:raise SyncError('Existing owner public-key file differs; not overwritten')
    PUBLIC.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not PUBLIC.exists():PUBLIC.write_bytes(derived);PUBLIC.chmod(0o644)
    return {'ok':True,'private_key_created_or_reused':True,'public_key_file':str(PUBLIC),'github_host_key_verified':True}


def ssh_env():
    if not KEY.is_file() or KEY.stat().st_mode&0o077:raise SyncError('Isolated deploy key absent or permissions are not 0600')
    parts=KNOWN_HOSTS.read_text().split()
    if len(parts)!=3 or parts[0]!='github.com' or parts[1]!='ssh-ed25519' or fingerprint(' '.join(parts[1:]))!=GITHUB_ED25519:
        raise SyncError('Pinned GitHub known_hosts absent or invalid')
    command=['ssh','-F','/dev/null','-i',str(KEY),'-o','IdentityAgent=none','-o','IdentitiesOnly=yes',
             '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(KNOWN_HOSTS),
             '-o','HostKeyAlgorithms=ssh-ed25519','-o','ConnectTimeout=15']
    env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_SSH_COMMAND=shlex.join(command),GIT_TERMINAL_PROMPT='0',GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null')
    return env


def path_violation(name):
    p=Path(name);lower=p.name.lower()
    if any(part.lower() in PRIVATE_DIRS for part in p.parts):return 'private_or_data_directory'
    if lower.startswith('brief_'):return 'private_working_brief'
    if lower=='.env' or (lower.startswith('.env.') and lower not in ('.env.example','.env.sample','.env.template')):return 'environment_file'
    if p.suffix.lower() in DATA_SUFFIXES:return 'data_archive_or_key'
    if p.suffix.lower()=='.sql' and not p.parts[0]=='migrations':return 'sql_outside_migrations'
    if lower in ('id_rsa','id_ed25519','credentials','authorized_keys'):return 'credential_file'
    return None


def content_violation(blob):
    if b'\0' in blob:return 'binary_object'
    if any(re.search(pattern,blob) for pattern in SECRET_PATTERNS):return 'credential_pattern'
    if re.search(rb'^-- PostgreSQL database dump(?:\r?\n|$)',blob,re.M) or re.search(rb'^COPY .* FROM stdin;',blob,re.M):return 'database_dump'
    assignment=rb"[\"']?[A-Za-z_-]*(?:api[_-]?key|token|password|secret|authorization)[A-Za-z_-]*[\"']?\s*(?:=|:)\s*[\"']([^\r\n\"']{8,})[\"']"
    for value in re.findall(assignment,blob,re.I):
        if value in (b'PGPASSWORD',b'PGPASSFILE',b'access_token',b'refresh_token',b'client_secret',b'client_id'):continue
        if not re.search(rb'example|placeholder|replace|changeme|dummy|fake|your_|not-a-|redacted|xxxxx',value,re.I):return 'credential_assignment'
    bare=rb'^(?:export\s+)?[A-Za-z_]*(?:API_KEY|TOKEN|PASSWORD|SECRET)[A-Za-z_]*\s*=\s*([A-Za-z0-9_./+=:-]{8,})\s*$'
    for value in re.findall(bare,blob,re.M|re.I):
        if not re.search(rb'example|placeholder|replace|changeme|dummy|fake|your_|redacted|xxxxx',value,re.I):return 'credential_assignment'
    return None


def branch_head(root):
    graft=Path(git(root,'rev-parse','--git-path','info/grafts').stdout.decode().strip())
    if not graft.is_absolute():graft=Path(root)/graft
    if graft.exists() and graft.stat().st_size:raise SyncError('Legacy grafts present; complete history cannot be audited')
    if git(root,'rev-parse','--is-shallow-repository').stdout.strip()==b'true':raise SyncError('Shallow history cannot be audited')
    if git(root,'branch','--show-current').stdout.decode().strip()!=BRANCH:raise SyncError('Only the fixed Rev recovery branch is permitted')
    return git(root,'rev-parse','--verify','refs/heads/'+BRANCH).stdout.decode().strip()


def audit(root,base,head):
    base_ancestor=not git(root,'merge-base','--is-ancestor',base,head,check=False).returncode
    commits=git(root,'rev-list','--reverse',base+'..'+head).stdout.decode().splitlines()
    objects=set(git(root,'rev-list','--objects','--no-object-names',base+'..'+head).stdout.decode().splitlines())
    issues=set();blobs={}
    # Inspect all added/modified paths, including renames of an already-public blob.
    for commit in commits:
        changed=git(root,'diff-tree','--root','-m','--no-commit-id','--name-only','--diff-filter=ACMRT','-r','-z',commit).stdout.split(b'\0')
        for raw in changed:
            if raw:
                name=raw.decode('utf-8','surrogateescape');reason=path_violation(name)
                if reason:issues.add((reason,name))
        tree=git(root,'ls-tree','-r','-z',commit).stdout.split(b'\0')
        for item in tree:
            if not item:continue
            info,raw=item.split(b'\t',1);_mode,kind,oid=info.decode().split()
            if kind=='blob' and oid in objects:blobs.setdefault(oid,set()).add(raw.decode('utf-8','surrogateescape'))
        if content_violation(git(root,'cat-file','commit',commit).stdout):issues.add(('credential_in_commit_message','[commit]'))
    for oid,names in blobs.items():
        size=int(git(root,'cat-file','-s',oid).stdout)
        if size>2*1024*1024:reason='oversized_outgoing_blob'
        else:reason=content_violation(git(root,'cat-file','blob',oid).stdout)
        if reason:
            for name in names:issues.add((reason,name))
    report={'base':base,'base_is_ancestor':base_ancestor,'head':head,'outgoing_commits':len(commits),'outgoing_blobs_checked':len(blobs),
            'blocked':bool(issues),'issue_count':len(issues),'issues':[{'reason':r,'path':n} for r,n in sorted(issues)]}
    return report


def sync(root=ROOT,check_only=False,base='refs/remotes/origin/main'):
    root=Path(root).resolve();head=branch_head(root)
    # Offline audit is useful before credentials exist; it never changes Git state.
    if check_only:return audit(root,git(root,'rev-parse','--verify',base).stdout.decode().strip(),head)
    env=ssh_env()
    if git(root,'config','--local','--get-regexp',r'^url\..*\.(insteadof|pushinsteadof)$',check=False).returncode==0:
        raise SyncError('Local URL rewrite configured; isolated sync refuses it')
    remote=git(root,'ls-remote',REPOSITORY,'refs/heads/main','refs/heads/'+BRANCH,env=env).stdout.decode().splitlines()
    refs={line.split()[1]:line.split()[0] for line in remote}
    if refs.get('refs/heads/'+BRANCH)==head:return {'ok':True,'already_synced':True,'head':head}
    baseline=refs.get('refs/heads/'+BRANCH) or refs.get('refs/heads/main')
    if not baseline:raise SyncError('Remote baseline missing; no initial/unrelated push allowed')
    if git(root,'cat-file','-e',baseline+'^{commit}',check=False).returncode:
        git(root,'fetch','--no-tags','--no-write-fetch-head','--recurse-submodules=no',REPOSITORY,baseline,env=env)
    report=audit(root,baseline,head)
    if refs.get('refs/heads/'+BRANCH) and not report['base_is_ancestor']:
        raise SyncError('Remote recovery branch diverged; force push is prohibited')
    if report['blocked']:return report
    # Explicit immutable source SHA, fixed destination, ordinary fast-forward only.
    git(root,'push','--porcelain','--no-follow-tags','--recurse-submodules=no',REPOSITORY,head+':refs/heads/'+BRANCH,env=env)
    confirmed=git(root,'ls-remote',REPOSITORY,'refs/heads/'+BRANCH,env=env).stdout.decode().split()
    if not confirmed or confirmed[0]!=head:raise SyncError('Push result not confirmed; next run must read remote before retry')
    result={'ok':True,'head':head,'outgoing_commits':report['outgoing_commits'],'outgoing_blobs_checked':report['outgoing_blobs_checked'],'remote_confirmed':True,'utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    os.umask(0o077);(KEYDIR/'github-last-sync.json').write_text(json.dumps(result,indent=2))
    return result



EXPORT_COLLECTORS={
    'catalog_recovery.py','feedback_autosend.py','feedback_collect_all.py','feedback_preflight.py',
    'feedback_recovery.py','feedback_send.py','ozon_answer_owner.py','ozon_feedbacks.py',
    'ozon_question_answers.py','ozon_review_answers.py','ozon_reviews.py','wb_feedbacks.py',
    'yandex_feedback_coverage.py','yandex_feedbacks.py','yandex_questions.py'}
EXPORT_REPORTS={
    'archive_question_policy.py','archive_questions.py','brand_notes.py','compatibility_evidence.py',
    'deepseek_search.py','feedback_llm.py','feedback_today.py','feedback_web.py','llm_batch.py',
    'llm_client.py','llm_routing.py','publish_gate.py','recovery_archive_publish.py','recovery_budget.py',
    'recovery_drafts.py','recovery_history.py','recovery_review.py',
    'rev_archive_fact_recovery.py','rev_delivery_recovery.py'}


def export_allowed(name):
    p=Path(name)
    if path_violation(name):return False
    if name in ('docs/REV_RULES.md','docs/rev/RECOVERY_RUNBOOK.md'):return True
    if len(p.parts)!=2:return False
    directory,filename=p.parts
    if directory=='collectors':return filename in EXPORT_COLLECTORS
    if directory=='reports':return filename in EXPORT_REPORTS
    if directory=='feedback_bot':return p.suffix in ('.py','.sql','.service','.timer') or filename in ('requirements.txt','requirements-recovery.txt')
    if directory=='migrations':return bool(re.fullmatch(r'7[0-9]{2}_[A-Za-z0-9_]+\.sql',filename))
    if directory=='ops':return filename.startswith('rev_') and (p.suffix in ('.py','.sh') or filename.endswith('.env.example'))
    if directory=='tests':return p.suffix=='.py' and (filename.startswith('test_rev_') or filename=='test_publish_gate.py')
    return False


def tree_entries(root,commit):
    result={}
    for item in git(root,'ls-tree','-r','-z',commit).stdout.split(b'\0'):
        if item:
            info,path=item.split(b'\t',1);mode,kind,oid=info.decode().split()
            result[path.decode('utf-8','surrogateescape')]={'mode':mode,'type':kind,'sha':oid}
    return result


def build_export_tree(root,source,base):
    """Overlay only committed Rev differences onto the complete public main tree."""
    before=tree_entries(root,base);after=tree_entries(root,source);changes=[];skipped=[];issues=[]
    for name in sorted(set(before)|set(after)):
        if before.get(name)==after.get(name):continue
        if not export_allowed(name):skipped.append(name);continue
        entry=after.get(name)
        if entry:
            if entry['type']!='blob' or entry['mode'] not in ('100644','100755'):
                issues.append({'path':name,'reason':'nonregular_export_file'});continue
            if int(git(root,'cat-file','-s',entry['sha']).stdout)>2*1024*1024:reason='oversized_export_blob'
            else:reason=content_violation(git(root,'cat-file','blob',entry['sha']).stdout)
            if reason:issues.append({'path':name,'reason':reason});continue
        changes.append(dict(path=name,**(entry or {'mode':'0','type':'blob','sha':None})))
    if issues:return {'blocked':True,'issues':issues,'source':source,'base':base,'skipped_paths':skipped}
    # A dedicated temporary index leaves the source index/worktree/refs untouched.
    with tempfile.TemporaryDirectory(prefix='rev-export-') as directory:
        env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        env['GIT_INDEX_FILE']=str(Path(directory)/'index')
        git(root,'read-tree',base,env=env)
        updates=b''.join((entry['mode']+' '+(entry['sha'] or '0'*40)+'\t'+entry['path']).encode('utf-8','surrogateescape')+b'\0' for entry in changes)
        if updates:run(['git','--no-replace-objects','update-index','-z','--index-info'],root=root,env=env,input_data=updates)
        tree=git(root,'write-tree',env=env).stdout.decode().strip()
    return {'blocked':False,'source':source,'base':base,'tree':tree,'changes':changes,'change_count':len(changes),'skipped_paths':skipped}


def ensure_remote_object(root,oid,env):
    if git(root,'cat-file','-e',oid+'^{commit}',check=False).returncode:
        git(root,'fetch','--no-tags','--no-write-fetch-head','--recurse-submodules=no',REPOSITORY,oid,env=env)


def export_commit_message(source,base):
    return 'Rev deployment snapshot\n\nSource committed recovery SHA: '+source+'\nPublic main baseline: '+base+'\n'


def export(root=ROOT,build_only=False,base='refs/remotes/origin/main'):
    root=Path(root).resolve();source=branch_head(root)
    if build_only:
        baseline=git(root,'rev-parse','--verify',base).stdout.decode().strip()
        return build_export_tree(root,source,baseline)
    env=ssh_env()
    if git(root,'config','--local','--get-regexp',r'^url\..*\.(insteadof|pushinsteadof)$',check=False).returncode==0:
        raise SyncError('Local URL rewrite configured; isolated export refuses it')
    lines=git(root,'ls-remote',REPOSITORY,'refs/heads/main','refs/heads/'+DEPLOY_BRANCH,env=env).stdout.decode().splitlines()
    refs={line.split()[1]:line.split()[0] for line in lines}
    baseline=refs.get('refs/heads/main')
    if not baseline:raise SyncError('Public main baseline missing')
    parent=refs.get('refs/heads/'+DEPLOY_BRANCH) or baseline
    for oid in set((baseline,parent)):ensure_remote_object(root,oid,env)
    plan=build_export_tree(root,source,baseline)
    if plan['blocked']:return plan
    if git(root,'rev-parse',parent+'^{tree}').stdout.decode().strip()==plan['tree']:
        return {'ok':True,'already_synced':True,'source':source,'remote_head':parent,'tree':plan['tree']}
    # This generated deployment commit contains already committed, audited source only.
    commit_env=dict(env,GIT_AUTHOR_NAME='Rev deployment sync',GIT_AUTHOR_EMAIL='rev-sync@localhost',GIT_COMMITTER_NAME='Rev deployment sync',GIT_COMMITTER_EMAIL='rev-sync@localhost')
    commit=run(['git','--no-replace-objects','commit-tree',plan['tree'],'-p',parent],root=root,env=commit_env,input_data=export_commit_message(source,baseline).encode()).stdout.decode().strip()
    git(root,'push','--porcelain','--no-follow-tags','--recurse-submodules=no',REPOSITORY,commit+':refs/heads/'+DEPLOY_BRANCH,env=env)
    result=git(root,'ls-remote',REPOSITORY,'refs/heads/'+DEPLOY_BRANCH,env=env).stdout.decode().split()
    if not result or result[0]!=commit:raise SyncError('Export push unconfirmed; next run reads remote tree before retry')
    receipt={'ok':True,'source':source,'remote_head':commit,'tree':plan['tree'],'base':baseline,'exported_changes':plan['change_count'],'remote_confirmed':True}
    os.umask(0o077);(KEYDIR/'github-last-export.json').write_text(json.dumps(receipt,indent=2))
    return receipt

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(ROOT));parser.add_argument('--setup',action='store_true');parser.add_argument('--check',action='store_true');parser.add_argument('--base',default='refs/remotes/origin/main');parser.add_argument('--export',action='store_true');parser.add_argument('--build-export-tree',action='store_true')
    args=parser.parse_args()
    try:
        result=setup() if args.setup else export(args.root,args.build_export_tree,args.base) if args.export or args.build_export_tree else sync(args.root,args.check,args.base)
        print(json.dumps(result));raise SystemExit(2 if result.get('blocked') else 0)
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc) if isinstance(exc,SyncError) else type(exc).__name__}));raise SystemExit(1)
if __name__=='__main__':main()
