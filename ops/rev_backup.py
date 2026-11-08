#!/usr/bin/env python3
# поток: rev
"""Consistent encrypted Rev recovery bundle and verified off-server transfer."""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from dotenv import dotenv_values
import requests

ROOT=Path(__file__).resolve().parents[1]
CRITICAL=('compat_ref','raw_feedback','feedback_moderation','approved_answers','brand_notes','rev_expert_evidence',
          'rev_publication_attempt','feedback_send_attempts','rev_edit_session','rev_telegram_inbox',
          'rev_telegram_checkpoint','rev_archive_publication','rev_history_analysis','feedback_llm_queue',
          'feedback_llm_batch','feedback_llm_result','rev_llm_budget','rev_card_reprocess')
UNITS=('feedback-moderation.service','feedback-recovery-cycle.service','feedback-recovery-cycle.timer',
       'feedback-recovery-review.service','feedback-recovery-review.timer','feedback-rev-backup.service',
       'feedback-rev-backup.timer')
class BackupError(RuntimeError):pass
MIN_FREE_BYTES=512*1024*1024


def atomic_json(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.new')
    with temporary.open('w') as stream:
        os.chmod(temporary,0o600);json.dump(value,stream,indent=2);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def check_http(response,phase):
    if response.ok:return
    reasons=[]
    try:
        error=response.json().get('error',{})
        if isinstance(error,str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,80}',error):reasons.append(error)
        for item in error.get('errors',[]) if isinstance(error,dict) else []:
            value=item.get('reason','')
            if re.fullmatch(r'[A-Za-z0-9_:-]{1,80}',value):reasons.append(value)
    except (ValueError,TypeError,AttributeError):pass
    raise BackupError(phase+'; HTTP '+str(response.status_code)+'; reason='+(','.join(reasons) or 'not_provided'))


def drive_token(cfg):
    auth=json.loads(private_file(cfg['REV_BACKUP_DRIVE_AUTH_FILE']).read_text())
    response=requests.post('https://oauth2.googleapis.com/token',data={
        'grant_type':'refresh_token','refresh_token':auth['refresh_token'],
        'client_id':auth['client_id'],'client_secret':auth['client_secret']},timeout=(10,30))
    check_http(response,'Drive token refresh')
    token=response.json().get('access_token')
    if not token:raise BackupError('Drive token refresh returned no access token')
    return {'Authorization':'Bearer '+token}


def drive_inventory(cfg):
    headers=drive_token(cfg);folder=cfg['REV_BACKUP_DRIVE_FOLDER_ID'];out=[];page=None
    while True:
        params={'q':"'"+folder+"' in parents and trashed=false",'pageSize':1000,
            'fields':'nextPageToken,files(id,name,size,createdTime,sha256Checksum,md5Checksum,parents,appProperties)', 'orderBy':'createdTime'}
        if page:params['pageToken']=page
        r=requests.get('https://www.googleapis.com/drive/v3/files',headers=headers,params=params,timeout=(10,30))
        check_http(r,'Drive list owned backups');data=r.json();out.extend(data.get('files',[]));page=data.get('nextPageToken')
        if not page:return out


def download_drive(item,destination,cfg):
    if cfg['REV_BACKUP_DRIVE_FOLDER_ID'] not in item.get('parents',[]) or item.get('appProperties',{}).get('project')!='mp-analytics-rev':
        raise BackupError('Download is outside the Rev backup scope')
    destination=Path(destination)
    if destination.exists():
        if destination.stat().st_size==int(item['size']) and sha256(destination)==item.get('sha256Checksum'):return destination
        raise BackupError('Existing downloaded file differs; not overwritten')
    r=requests.get('https://www.googleapis.com/drive/v3/files/'+item['id'],headers=drive_token(cfg),params={'alt':'media'},stream=True,timeout=(10,90))
    check_http(r,'Drive download backup');partial=destination.with_suffix(destination.suffix+'.download')
    with partial.open('xb') as stream:
        os.chmod(partial,0o600)
        for block in r.iter_content(1024*1024):stream.write(block)
    if partial.stat().st_size!=int(item['size']) or sha256(partial)!=item.get('sha256Checksum'):
        raise BackupError('Downloaded backup SHA-256/size differs from Drive')
    os.replace(partial,destination);return destination


def run(command,*,env=None,timeout=1800,space_guard=None):
    if space_guard is None:
        result=subprocess.run(command,env=env,capture_output=True,timeout=timeout)
    else:
        process=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        deadline=time.monotonic()+timeout
        try:
            while True:
                if shutil.disk_usage(space_guard).free<MIN_FREE_BYTES:
                    raise BackupError('Backup writer stopped to preserve free disk space')
                remaining=deadline-time.monotonic()
                if remaining<=0:raise subprocess.TimeoutExpired(command,timeout)
                try:
                    stdout,stderr=process.communicate(timeout=min(.5,remaining));break
                except subprocess.TimeoutExpired:continue
            result=subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
        except BaseException:
            process.terminate()
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.communicate()
            raise
    if result.returncode:
        raise BackupError(Path(command[0]).name+' failed; exit='+str(result.returncode))
    return result.stdout


def private_file(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_mode&0o077:raise BackupError('Private configuration/key must be a regular 0600 file')
    return path


def config(path=None):
    values={}
    p=Path(path or '/root/.config/rev-recovery/backup.env')
    if p.exists():values.update({k:v for k,v in dotenv_values(private_file(p)).items() if v is not None})
    values.update({k:v for k,v in os.environ.items() if k.startswith('REV_BACKUP_')})
    return values


def connection(root):
    path=private_file(Path(root)/'.env')
    dsn=dotenv_values(path).get('DATABASE_URL')
    if not dsn:raise BackupError('DATABASE_URL missing from project configuration')
    params=parse_dsn(dsn)
    if not re.fullmatch(r'rev_recovery_[A-Za-z0-9_]+',params.get('dbname','')):
        raise BackupError('Backup is restricted to the separate Rev recovery database')
    return dsn,params


def pg_environment(params,dbname=None):
    env=os.environ.copy()
    mapping={'host':'PGHOST','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD',
             'sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','connect_timeout':'PGCONNECT_TIMEOUT'}
    for key,name in mapping.items():
        if key in params:env[name]=params[key]
    env['PGDATABASE']=dbname or params['dbname']
    env['PGOPTIONS']='-c timezone=UTC'
    return env


def fingerprint(cur,critical=CRITICAL):
    cur.execute("SET LOCAL timezone='UTC'")
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    tables={}
    for (name,) in cur.fetchall():
        cur.execute(sql.SQL('SELECT count(*) FROM public.{}').format(sql.Identifier(name)))
        record={'rows':cur.fetchone()[0]}
        if name in critical:
            cur.execute(sql.SQL("SELECT md5(COALESCE(string_agg(h,'' ORDER BY h),'')) FROM (SELECT md5(to_jsonb(t)::text) h FROM public.{} t) x").format(sql.Identifier(name)))
            record['digest']=cur.fetchone()[0]
        tables[name]=record
    return tables


def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def md5(path):
    h=hashlib.md5()
    with open(path,'rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def key_path(cfg,create=False):
    path=Path(cfg.get('REV_BACKUP_PASSPHRASE_FILE','/root/.config/rev-recovery/backup.key'))
    if not path.exists() and create:
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with path.open('xb') as stream:os.chmod(path,0o600);stream.write(os.urandom(32).hex().encode()+b'\n')
    return private_file(path)


def encrypt(source,dest,key,space_guard=None):
    run(['gpg','--batch','--yes','--pinentry-mode','loopback','--passphrase-file',str(private_file(key)),
         '--symmetric','--cipher-algo','AES256','--compress-algo','none','--output',str(dest),str(source)],space_guard=space_guard)


def decrypt(source,dest,key):
    run(['gpg','--batch','--yes','--pinentry-mode','loopback','--passphrase-file',str(private_file(key)),
         '--output',str(dest),'--decrypt',str(source)])


def code_snapshot(root,dest):
    """Capture deployed code and private provenance notes for the encrypted bundle only."""
    allowed=[]
    for directory in ('core','collectors','reports','feedback_bot','migrations','ops'):
        base=Path(root)/directory
        if base.exists():
            for p in base.rglob('*'):
                if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts and (
                    p.suffix in ('.py','.sql','.service','.timer') or p.name in ('requirements.txt','requirements-recovery.txt')):
                    allowed.append(p)
    for name in ('requirements.txt','AGENTS.md','CLAUDE.md','docs/REV_RULES.md','docs/BRIEF_REV.md','docs/rev/RECOVERY_RUNBOOK.md',
                 'docs/handoff/archive/rev_2026-09-10.md','docs/reports/rev_gold_set.md',
                 'feedback_bot/rev-backup-public.caddy','web/rev_backup_public/index.html',
                 'web/rev_backup_public/privacy/index.html','web/rev_backup_public/terms/index.html',
                 'web/rev_backup_public/robots.txt'):
        p=Path(root)/name
        if p.is_file() and not p.is_symlink():allowed.append(p)
    # Provenance sources are private: include all nested Rev Markdown only in this
    # encrypted recovery payload; Git deployment export has its own strict whitelist.
    notes=Path(root)/'docs/rev'
    if notes.exists():
        for p in notes.rglob('*.md'):
            linked_parent=any(parent.is_symlink() for parent in p.parents if parent==notes or notes in parent.parents)
            if p.is_file() and not p.is_symlink() and not linked_parent:allowed.append(p)
    if not allowed:raise BackupError('No Rev source files found')
    with tarfile.open(dest,'w') as archive:
        for p in sorted(set(allowed)):archive.add(p,arcname=str(p.relative_to(root)),recursive=False)


def checked_ssh_settings(cfg):
    host=cfg.get('REV_BACKUP_SSH_HOST','');user=cfg.get('REV_BACKUP_SSH_USER','')
    remote=cfg.get('REV_BACKUP_SSH_DIR','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*',host) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',user):
        raise BackupError('Invalid SSH backup host/user')
    if not remote.startswith('/') or not re.fullmatch(r'/[A-Za-z0-9_./-]+',remote) or '..' in Path(remote).parts:
        raise BackupError('SSH backup directory must be an absolute dedicated path without traversal')
    key=private_file(cfg['REV_BACKUP_SSH_KEY'])
    hosts=Path(cfg['REV_BACKUP_SSH_KNOWN_HOSTS'])
    if not hosts.is_file():raise BackupError('Verified SSH known_hosts file is required')
    port=str(int(cfg.get('REV_BACKUP_SSH_PORT','22')))
    options=['-i',str(key),'-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
             '-o','UserKnownHostsFile='+str(hosts),'-o','ConnectTimeout=15']
    return user+'@'+host,remote,port,options


def upload_ssh(path,cfg):
    target,directory,port,options=checked_ssh_settings(cfg)
    host=target.rsplit('@',1)[-1]
    local=set(run(['hostname','-I'],timeout=10).decode().split())|{'127.0.0.1','::1'}
    remote={entry[4][0] for entry in socket.getaddrinfo(host,None)}
    local={ipaddress.ip_address(value) for value in local}
    remote={ipaddress.ip_address(value) for value in remote}
    if host in (socket.gethostname(),'localhost') or local&remote:
        raise BackupError('Offsite destination must be a different server')
    run(['ssh','-p',port,*options,target,'mkdir -p -- '+shlex.quote(directory)],timeout=60)
    remote=directory.rstrip('/')+'/'+path.name
    run(['scp','-P',port,*options,str(path),target+':'+remote+'.partial'],timeout=1800)
    response=run(['ssh','-p',port,*options,target,'sha256sum -- '+shlex.quote(remote+'.partial')],timeout=120).decode().split()
    if not response or response[0]!=sha256(path):raise BackupError('Remote SSH checksum differs')
    run(['ssh','-p',port,*options,target,'mv -- '+shlex.quote(remote+'.partial')+' '+shlex.quote(remote)],timeout=60)
    return {'method':'ssh','verified':True,'sha256':response[0]}


def upload_gdrive(path,cfg):
    headers=drive_token(cfg);folder=cfg['REV_BACKUP_DRIVE_FOLDER_ID']
    metadata=requests.get('https://www.googleapis.com/drive/v3/files/'+folder,
        headers=headers,params={'fields':'id,mimeType,trashed,capabilities(canAddChildren)'},timeout=(10,30))
    check_http(metadata,'Drive destination metadata')
    f=metadata.json()
    if f.get('mimeType')!='application/vnd.google-apps.folder' or f.get('trashed') or not f.get('capabilities',{}).get('canAddChildren'):
        raise BackupError('Drive destination is not a writable folder')
    permissions=requests.get('https://www.googleapis.com/drive/v3/files/'+folder+'/permissions',
        headers=headers,params={'fields':'permissions(type,role),nextPageToken'},timeout=(10,30))
    check_http(permissions,'Drive destination permissions')
    if (not permissions.ok or not permissions.json().get('permissions') or permissions.json().get('nextPageToken') or
            any(p.get('type')!='user' or p.get('role')!='owner' for p in permissions.json().get('permissions',[]))):
        raise BackupError('Drive destination privacy cannot be verified')
    start=requests.post('https://www.googleapis.com/upload/drive/v3/files',
        headers=dict(headers,**{'X-Upload-Content-Type':'application/octet-stream','X-Upload-Content-Length':str(path.stat().st_size)}),
        params={'uploadType':'resumable','fields':'id,size,md5Checksum'},
        json={'name':path.name,'parents':[folder],'appProperties':{'project':'mp-analytics-rev','encrypted':'true'}},timeout=(10,30))
    check_http(start,'Drive upload initialization')
    if not start.headers.get('Location'):raise BackupError('Drive upload response has no resumable location')
    with path.open('rb') as stream:
        response=requests.put(start.headers['Location'],headers={'Content-Type':'application/octet-stream'},data=stream,timeout=(10,1800))
    check_http(response,'Drive upload content')
    result=response.json()
    if int(result.get('size',-1))!=path.stat().st_size or result.get('md5Checksum')!=md5(path):
        raise BackupError('Drive upload checksum or size differs')
    readback=requests.get('https://www.googleapis.com/drive/v3/files/'+result['id'],headers=headers,
        params={'alt':'media'},stream=True,timeout=(10,90));check_http(readback,'Drive uploaded content readback')
    digest=hashlib.sha256();count=0
    for block in readback.iter_content(1024*1024):digest.update(block);count+=len(block)
    if count!=path.stat().st_size or digest.hexdigest()!=sha256(path):raise BackupError('Drive readback SHA-256 differs')
    return {'method':'gdrive','verified':True,'readback_verified':True,'sha256':digest.hexdigest(),
            'file_id':result['id'],'bytes':count,'md5':result['md5Checksum']}


def upload(path,cfg):
    method=cfg.get('REV_BACKUP_OFFSITE_METHOD','')
    if method=='ssh':return upload_ssh(path,cfg)
    if method=='gdrive':return upload_gdrive(path,cfg)
    raise BackupError('No autonomous offsite destination configured')


def successful(row):
    return bool(row.get('offsite',{}).get('verified') and
        (row.get('offsite',{}).get('method')!='gdrive' or row['offsite'].get('readback_verified')))


def safe_error(error):
    return str(error) if isinstance(error,BackupError) else type(error).__name__


def record_attempt(directory,row):
    directory=Path(directory);atomic_json(directory/'latest_attempt.json',row)
    if not successful(row):return False
    bundle=Path(row['bundle'])
    if not bundle.is_file() or sha256(bundle)!=row['bundle_sha256']:raise BackupError('Successful local bundle integrity differs')
    previous=directory/'latest_success.json'
    if previous.is_file():
        old=json.loads(previous.read_text())
        if old.get('bundle')!=row['bundle']:atomic_json(directory/'previous_success.json',old)
    atomic_json(previous,row)
    # Compatibility readers see SUCCESS only. A failure never replaces this marker.
    atomic_json(directory/'latest.json',row)
    return True


def retention_selection(records,daily=7,weekly=4,recent=2):
    ordered=sorted(records,key=lambda x:x['source_snapshot_utc'],reverse=True)
    keep={x['bundle'] for x in ordered[:recent]};days=set();weeks=set()
    for row in ordered:
        date=dt.datetime.strptime(row['source_snapshot_utc'][:8],'%Y%m%d').date()
        week=date.isocalendar()[:2]
        if date not in days and len(days)<daily:days.add(date);keep.add(row['bundle'])
        if week not in weeks and len(weeks)<weekly:weeks.add(week);keep.add(row['bundle'])
    return keep


def drive_retention_plan(items,protected_ids,folder):
    """Read-only plan. No trash/permanent deletion method is provided."""
    if not protected_ids:raise BackupError('Verified external anchor ids required before planning deletion')
    records=[]
    for item in items:
        if (folder not in item.get('parents',[]) or item.get('appProperties',{}).get('project')!='mp-analytics-rev'
                or not re.fullmatch(r'rev-backup-[0-9]{8}T[0-9]{6}Z\.tar\.gz\.gpg',item.get('name',''))):continue
        records.append({'bundle':item['id'],'source_snapshot_utc':item['name'][11:27],'item':item})
    keep=retention_selection(records,daily=7,weekly=4,recent=0)|set(protected_ids)
    return {'keep_file_ids':sorted(keep),'candidates':[r['item'] for r in records if r['bundle'] not in keep],
        'trash_authorized':False,'permanent_deletion_authorized':False,'policy':{'daily':7,'weekly':4}}


def retention(directory,keep_recent=2,keep_days=7,keep_weeks=4,new_success=None):
    # Local files only. Drive deletion is NEVER performed here.
    directory=Path(directory)
    if not new_success or not successful(new_success):return {'deleted':0,'deferred':'new external success is required'}
    protected=set();records=[]
    for name in ['latest_success.json','previous_success.json','last_restored_success.json']:
        p=directory/name
        if p.is_file():protected.add(json.loads(p.read_text()).get('bundle'))
    prefix='rev-db-backup-' if new_success.get('kind')=='database' else 'rev-backup-'
    for file in directory.glob(prefix+'*.tar.gz.gpg'):
        receipt=file.with_suffix(file.suffix+'.json')
        if not receipt.is_file() or file.is_symlink():continue
        row=json.loads(receipt.read_text())
        if (row.get('bundle')!=str(file) or not successful(row) or row.get('bundle_sha256')!=sha256(file)):continue
        row.setdefault('source_snapshot_utc',file.name[len(prefix):][:16]);records.append(row)
    keep=retention_selection(records,keep_days,keep_weeks,keep_recent)|protected;deleted=0
    for row in records:
        file=Path(row['bundle'])
        if row['bundle'] not in keep and file.parent.resolve()==directory.resolve():
            file.unlink();file.with_suffix(file.suffix+'.json').unlink();deleted+=1
    return {'deleted':deleted,'protected':len(keep),'remote_deletion_enabled':False}


def ensure_backup_space(directory,database_bytes):
    # No speculative cleanup before a new external success; fail closed on space.
    directory=Path(directory);required=database_bytes*2+MIN_FREE_BYTES
    for marker in ['latest_success.json','latest.json']:
        try:
            previous=json.loads((directory/marker).read_text());p=Path(previous['bundle'])
            if p.parent.resolve()==directory.resolve() and not p.is_symlink() and p.is_file() and previous.get('bundle_sha256')==sha256(p):
                required=min(required,max(1024*1024*1024,p.stat().st_size*4+MIN_FREE_BYTES));break
        except (OSError,ValueError,KeyError,TypeError):pass
    if shutil.disk_usage(directory).free<required:raise BackupError('Insufficient free space for a safe backup/drill; verified copies preserved')


def drive_space_preflight(cfg,directory):
    if cfg.get('REV_BACKUP_OFFSITE_METHOD')!='gdrive':return
    response=requests.get('https://www.googleapis.com/drive/v3/about',headers=drive_token(cfg),params={'fields':'storageQuota'},timeout=(10,30))
    check_http(response,'Drive quota lookup');quota=response.json()['storageQuota']
    expected=0
    for marker in ['latest_success.json','latest.json']:
        p=Path(directory)/marker
        if p.is_file():expected=max(expected,int(json.loads(p.read_text()).get('bytes',0)))
    free=int(quota.get('limit',0))-int(quota.get('usage',0))
    if expected and free<expected:raise BackupError('Drive storage quota insufficient; free_bytes='+str(free)+'; previous_bundle_bytes='+str(expected))


class SpaceCheckedWriter:
    def __init__(self,stream,directory):self.stream=stream;self.directory=directory
    def write(self,data):
        if shutil.disk_usage(self.directory).free-len(data)<MIN_FREE_BYTES:
            raise BackupError('Backup packaging stopped to preserve free disk space')
        return self.stream.write(data)
    def __getattr__(self,name):return getattr(self.stream,name)


def backup(root=ROOT,cfg=None,local_only=False,include_runtime=True):
    os.umask(0o077);root=Path(root).resolve();cfg=cfg or config();started=time.monotonic()
    directory=Path(cfg.get('REV_BACKUP_DIR','/root/rev-recovery/backups'));directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (directory/'.backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        key=key_path(cfg,create=True)
        if not local_only:drive_space_preflight(cfg,directory)
        dsn,params=connection(root)
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        kind=cfg.get('REV_BACKUP_KIND','full')
        if kind not in ('full','database') or (kind=='database' and include_runtime):raise BackupError('Invalid backup kind/runtime combination')
        target=directory/(('rev-db-backup-' if kind=='database' else 'rev-backup-')+stamp+'.tar.gz.gpg')
        if target.exists():raise BackupError('Backup name already exists')
        with tempfile.TemporaryDirectory(prefix='.rev-backup-',dir=directory) as tmp:
            tmp=Path(tmp);dump=tmp/'database.dump'
            with psycopg2.connect(dsn) as conn:
                conn.set_session(isolation_level='REPEATABLE READ',readonly=True)
                with conn.cursor() as cur:
                    cur.execute('SELECT pg_export_snapshot(),current_database(),pg_database_size(current_database())')
                    snapshot,database,size=cur.fetchone()
                    ensure_backup_space(directory,size)
                    tables=fingerprint(cur)
                    run(['pg_dump','--format=custom','--compress=6','--no-owner','--no-acl','--lock-wait-timeout=30000',
                         '--snapshot='+snapshot,'--file='+str(dump)],env=pg_environment(params),space_guard=directory)
            run(['pg_restore','--list',str(dump)],timeout=120)
            code=tmp/'code.tar';code_snapshot(root,code)
            manifest={'version':1,'kind':kind,'created_at_utc':stamp,'hostname':socket.gethostname(),'source_root':str(root),
                      'source_database':database,'database_bytes':size,'postgres_major':16,
                      'git_commit':run(['git','-C',str(root),'rev-parse','HEAD']).decode().strip(),
                      'tables':tables,'dump_sha256':sha256(dump),'code_sha256':sha256(code),
                      'runtime_included':include_runtime,'python_abi':'cp'+str(os.sys.version_info.major)+str(os.sys.version_info.minor),
                      'operating_system':Path('/etc/os-release').read_text(),'secrets_encrypted':True}
            manifest_file=tmp/'manifest.json';manifest_file.write_text(json.dumps(manifest,indent=2))
            requirements=tmp/'requirements-installed.txt'
            requirements.write_text('\n'.join(sorted(d.metadata['Name']+'=='+d.version for d in importlib.metadata.distributions() if d.metadata.get('Name')))+'\n')
            package=tmp/'recovery.tar.gz'
            with package.open('wb') as raw,tarfile.open(fileobj=SpaceCheckedWriter(raw,directory),mode='w:gz',compresslevel=6) as tar:
                for file in [dump,code,manifest_file,requirements]:tar.add(file,arcname=file.name,recursive=False)
                tar.add(private_file(root/'.env'),arcname='secrets/runtime.env',recursive=False)
                backup_config=Path('/root/.config/rev-recovery/backup.env')
                if backup_config.is_file():tar.add(private_file(backup_config),arcname='secrets/backup/backup.env',recursive=False)
                for setting,name in (('REV_BACKUP_DRIVE_AUTH_FILE','drive-oauth.json'),('REV_BACKUP_SSH_KEY','offsite_ed25519')):
                    if cfg.get(setting):
                        credential=private_file(cfg[setting])
                        if credential.resolve()==key.resolve():raise BackupError('Encryption key cannot be bundled as offsite credential')
                        tar.add(credential,arcname='secrets/backup/'+name,recursive=False)
                if cfg.get('REV_BACKUP_SSH_KNOWN_HOSTS'):
                    tar.add(cfg['REV_BACKUP_SSH_KNOWN_HOSTS'],arcname='secrets/backup/known_hosts',recursive=False)
                if include_runtime:tar.add((root/'venv').resolve(),arcname='runtime/venv',recursive=True)
                for name in UNITS:
                    p=Path('/etc/systemd/system')/name
                    if p.is_file():tar.add(p,arcname='systemd/'+name,recursive=False)
                for p in (root/'ops').glob('rev_backup*'):
                    if p.is_file():tar.add(p,arcname='recovery-tools/ops/'+p.name,recursive=False)
                for p in (root/'ops').glob('rev_restore*'):
                    if p.is_file():tar.add(p,arcname='recovery-tools/ops/'+p.name,recursive=False)
            partial=target.with_suffix(target.suffix+'.partial')
            try:encrypt(package,partial,key,space_guard=directory);partial.replace(target)
            finally:
                if partial.exists():partial.unlink()
        result={'kind':kind,'bundle':str(target),'bytes':target.stat().st_size,'bundle_sha256':sha256(target),
                'source_snapshot_utc':stamp,'git_commit':manifest['git_commit'],'local_only':local_only,
                'offsite':{'verified':False},'backup_seconds':round(time.monotonic()-started,3)}
        error=None
        if not local_only:
            try:result['offsite']=upload(target,cfg)
            except Exception as exc:error=safe_error(exc);result['offsite'].update(error_type=type(exc).__name__,error=error)
        result['total_seconds']=round(time.monotonic()-started,3)
        atomic_json(target.with_suffix(target.suffix+'.json'),result)
        if record_attempt(directory,result):
            retention(directory,12 if kind=='database' else 2,0 if kind=='database' else 7,0 if kind=='database' else 4,new_success=result)
        if error:raise BackupError('Encrypted local copy complete, but verified offsite transfer failed: '+error)
        return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(ROOT));parser.add_argument('--config')
    parser.add_argument('--local-only',action='store_true');parser.add_argument('--no-runtime',action='store_true')
    parser.add_argument('--database-only',action='store_true',help='Same consistent DB+code/config format, without reproducible venv; separate journal required')
    args=parser.parse_args()
    cfg=config(args.config);attempt_started=time.time()
    if args.database_only:
        cfg['REV_BACKUP_KIND']='database';args.no_runtime=True
        if cfg.get('REV_BACKUP_DIR','/root/rev-recovery/backups')=='/root/rev-recovery/backups':parser.error('Database-only snapshots require their own directory/journals')
    try:print(json.dumps(backup(args.root,cfg,args.local_only,not args.no_runtime)))
    except Exception as exc:
        directory=Path(cfg.get('REV_BACKUP_DIR','/root/rev-recovery/backups'))
        marker=directory/'latest_attempt.json';row={}
        if marker.is_file() and marker.stat().st_mtime>=attempt_started:
            try:row=json.loads(marker.read_text())
            except ValueError:pass
        row.update(ok=False,checked_at=dt.datetime.now(dt.timezone.utc).isoformat(),error=safe_error(exc))
        row.setdefault('offsite',{'verified':False})
        if directory.is_dir():atomic_json(directory/'latest_attempt.json',row)
        print(json.dumps(row));raise SystemExit(1)
if __name__=='__main__':main()
