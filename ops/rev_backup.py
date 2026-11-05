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


def run(command,*,env=None,timeout=1800):
    result=subprocess.run(command,env=env,capture_output=True,timeout=timeout)
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


def encrypt(source,dest,key):
    run(['gpg','--batch','--yes','--pinentry-mode','loopback','--passphrase-file',str(private_file(key)),
         '--symmetric','--cipher-algo','AES256','--compress-algo','none','--output',str(dest),str(source)])


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
                 'docs/handoff/archive/rev_2026-09-10.md','docs/reports/rev_gold_set.md'):
        p=Path(root)/name
        if p.is_file():allowed.append(p)
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
    auth=json.loads(private_file(cfg['REV_BACKUP_DRIVE_AUTH_FILE']).read_text())
    refresh=requests.post('https://oauth2.googleapis.com/token',data={
        'grant_type':'refresh_token','refresh_token':auth['refresh_token'],
        'client_id':auth['client_id'],'client_secret':auth['client_secret']},timeout=(10,30))
    if not refresh.ok:raise BackupError('Drive authorization failed; HTTP '+str(refresh.status_code))
    token=refresh.json().get('access_token')
    if not token:raise BackupError('Drive authorization returned no access token')
    headers={'Authorization':'Bearer '+token};folder=cfg['REV_BACKUP_DRIVE_FOLDER_ID']
    metadata=requests.get('https://www.googleapis.com/drive/v3/files/'+folder,
        headers=headers,params={'fields':'id,mimeType,trashed,capabilities(canAddChildren)'},timeout=(10,30))
    if not metadata.ok:raise BackupError('Drive folder unavailable; HTTP '+str(metadata.status_code))
    f=metadata.json()
    if f.get('mimeType')!='application/vnd.google-apps.folder' or f.get('trashed') or not f.get('capabilities',{}).get('canAddChildren'):
        raise BackupError('Drive destination is not a writable folder')
    permissions=requests.get('https://www.googleapis.com/drive/v3/files/'+folder+'/permissions',
        headers=headers,params={'fields':'permissions(type,role),nextPageToken'},timeout=(10,30))
    if (not permissions.ok or not permissions.json().get('permissions') or permissions.json().get('nextPageToken') or
            any(p.get('type')!='user' or p.get('role')!='owner' for p in permissions.json().get('permissions',[]))):
        raise BackupError('Drive destination privacy cannot be verified')
    start=requests.post('https://www.googleapis.com/upload/drive/v3/files',
        headers=dict(headers,**{'X-Upload-Content-Type':'application/octet-stream','X-Upload-Content-Length':str(path.stat().st_size)}),
        params={'uploadType':'resumable','fields':'id,size,md5Checksum'},
        json={'name':path.name,'parents':[folder],'appProperties':{'project':'mp-analytics-rev','encrypted':'true'}},timeout=(10,30))
    if not start.ok or not start.headers.get('Location'):raise BackupError('Drive upload could not start')
    with path.open('rb') as stream:
        response=requests.put(start.headers['Location'],headers={'Content-Type':'application/octet-stream'},data=stream,timeout=(10,1800))
    if not response.ok:raise BackupError('Drive upload result unconfirmed; HTTP '+str(response.status_code))
    result=response.json()
    if int(result.get('size',-1))!=path.stat().st_size or result.get('md5Checksum')!=md5(path):
        raise BackupError('Drive upload checksum or size differs')
    return {'method':'gdrive','verified':True,'file_id':result['id'],'bytes':path.stat().st_size,'md5':result['md5Checksum']}


def upload(path,cfg):
    method=cfg.get('REV_BACKUP_OFFSITE_METHOD','')
    if method=='ssh':return upload_ssh(path,cfg)
    if method=='gdrive':return upload_gdrive(path,cfg)
    raise BackupError('No autonomous offsite destination configured')


def retention(directory,keep_recent=12,keep_days=7):
    files=sorted(Path(directory).glob('rev-backup-*.tar.gz.gpg'),reverse=True)
    keep=set(files[:keep_recent]);dates=set()
    for path in files:
        date=path.name[len('rev-backup-'):][:8]
        if date not in dates and len(dates)<keep_days:dates.add(date);keep.add(path)
    for path in files:
        # Only files generated by this tool, with a completed private receipt, are eligible.
        receipt=path.with_suffix(path.suffix+'.json')
        if path not in keep and receipt.is_file():
            data=json.loads(receipt.read_text())
            if data.get('bundle_sha256')==sha256(path) and (data.get('offsite',{}).get('verified') or data.get('local_only')):
                path.unlink();receipt.unlink()


def backup(root=ROOT,cfg=None,local_only=False,include_runtime=True):
    os.umask(0o077);root=Path(root).resolve();cfg=cfg or config();started=time.monotonic()
    directory=Path(cfg.get('REV_BACKUP_DIR','/root/rev-recovery/backups'));directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (directory/'.backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        key=key_path(cfg,create=True)
        dsn,params=connection(root)
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        target=directory/('rev-backup-'+stamp+'.tar.gz.gpg')
        if target.exists():raise BackupError('Backup name already exists')
        with tempfile.TemporaryDirectory(prefix='.rev-backup-',dir=directory) as tmp:
            tmp=Path(tmp);dump=tmp/'database.dump'
            with psycopg2.connect(dsn) as conn:
                conn.set_session(isolation_level='REPEATABLE READ',readonly=True)
                with conn.cursor() as cur:
                    cur.execute('SELECT pg_export_snapshot(),current_database(),pg_database_size(current_database())')
                    snapshot,database,size=cur.fetchone()
                    if shutil.disk_usage(directory).free<size*2+512*1024*1024:raise BackupError('Insufficient free space for a safe backup/drill')
                    tables=fingerprint(cur)
                    run(['pg_dump','--format=custom','--compress=6','--no-owner','--no-acl','--lock-wait-timeout=30000',
                         '--snapshot='+snapshot,'--file='+str(dump)],env=pg_environment(params))
            run(['pg_restore','--list',str(dump)],timeout=120)
            code=tmp/'code.tar';code_snapshot(root,code)
            manifest={'version':1,'created_at_utc':stamp,'hostname':socket.gethostname(),'source_root':str(root),
                      'source_database':database,'database_bytes':size,'postgres_major':16,
                      'git_commit':run(['git','-C',str(root),'rev-parse','HEAD']).decode().strip(),
                      'tables':tables,'dump_sha256':sha256(dump),'code_sha256':sha256(code),
                      'runtime_included':include_runtime,'python_abi':'cp'+str(os.sys.version_info.major)+str(os.sys.version_info.minor),
                      'operating_system':Path('/etc/os-release').read_text(),'secrets_encrypted':True}
            manifest_file=tmp/'manifest.json';manifest_file.write_text(json.dumps(manifest,indent=2))
            requirements=tmp/'requirements-installed.txt'
            requirements.write_text('\n'.join(sorted(d.metadata['Name']+'=='+d.version for d in importlib.metadata.distributions() if d.metadata.get('Name')))+'\n')
            package=tmp/'recovery.tar.gz'
            with tarfile.open(package,'w:gz',compresslevel=6) as tar:
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
            partial=target.with_suffix(target.suffix+'.partial');encrypt(package,partial,key);partial.replace(target)
        result={'bundle':str(target),'bytes':target.stat().st_size,'bundle_sha256':sha256(target),
                'source_snapshot_utc':stamp,'git_commit':manifest['git_commit'],'local_only':local_only,
                'offsite':{'verified':False},'backup_seconds':round(time.monotonic()-started,3)}
        error=None
        if not local_only:
            try:result['offsite']=upload(target,cfg)
            except Exception as exc:error=type(exc).__name__;result['offsite']['error_type']=error
        result['total_seconds']=round(time.monotonic()-started,3)
        target.with_suffix(target.suffix+'.json').write_text(json.dumps(result,indent=2))
        (directory/'latest.json').write_text(json.dumps(result,indent=2))
        retention(directory,int(cfg.get('REV_BACKUP_KEEP_RECENT','12')),int(cfg.get('REV_BACKUP_KEEP_DAYS','7')))
        if error:raise BackupError('Encrypted local copy complete, but verified offsite transfer failed: '+error)
        return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(ROOT));parser.add_argument('--config')
    parser.add_argument('--local-only',action='store_true');parser.add_argument('--no-runtime',action='store_true')
    args=parser.parse_args()
    try:print(json.dumps(backup(args.root,config(args.config),args.local_only,not args.no_runtime)))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc) if isinstance(exc,BackupError) else type(exc).__name__}));raise SystemExit(1)
if __name__=='__main__':main()
