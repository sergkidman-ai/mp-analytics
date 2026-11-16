#!/usr/bin/env python3
# поток: infra
"""Encrypted, verified offsite backup of the main Pult database and deployment.

Never restores to the production cluster. Restore verification starts a separate
PostgreSQL cluster with no network, workers or production sockets configured.
Drive rotation deletes only this application's verified files older than 72h.
"""
import argparse
import configparser
import contextlib
import csv
import datetime as dt
import fcntl
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
import requests

ROOT=Path('/opt/mp-analytics')
STATE=Path('/root/.config/pult-backup')
LOCAL=Path('/var/backups/mp-analytics/pult-drive')
CONFIG=STATE/'config.json'
PROJECT='mp-analytics-pult-backup'
PG=Path('/usr/lib/postgresql/16/bin')


def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.chmod(0o600);tmp.replace(path)
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb')as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def private(path):
    p=Path(path);assert p.is_file()and not p.is_symlink()and p.stat().st_uid==0 and not p.stat().st_mode&0o077,'unsafe_private_file'
    return p
def config():
    c=json.loads(private(CONFIG).read_text());assert c['database']=='mp_analytics'
    if c.get('offline_key_confirmed')and c.get('key_sha256'):
        assert digest(private(c['key_file']))==c['key_sha256'],'backup_key_changed'
    else:
        policy=json.loads(private('/srv/sokol/config/backup-oauth-policy.json').read_text())
        assert policy['key_saved_offline_owner_confirmed']and digest(private(c['key_file']))==policy['key_sha256'],'offline_key_not_confirmed'
    return c
def command(args,*,env=None,timeout=3600,log=None,stdout=None):
    r=subprocess.run(list(map(str,args)),env=env,stdout=stdout if stdout is not None else subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if log:Path(log).write_bytes(r.stderr)
    if r.returncode:raise RuntimeError('subcommand_failed_'+Path(str(args[0])).name)
    return r.stdout
def import_file(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


class Drive:
    def __init__(self,c):
        self.c=c
        self.token=None;self.expires=0
    def headers(self):
        if self.token and self.expires>time.time()+60:return {'Authorization':'Bearer '+self.token}
        auth=json.loads(private(self.c['auth_file']).read_text())
        r=requests.post('https://oauth2.googleapis.com/token',data={'grant_type':'refresh_token',
                        'refresh_token':auth['refresh_token'],'client_id':auth['client_id'],
                        'client_secret':auth['client_secret']},timeout=(15,60))
        if not r.ok:raise RuntimeError('drive_token_refresh_failed')
        data=r.json();self.token=data['access_token'];self.expires=time.time()+int(data.get('expires_in',3600))
        return {'Authorization':'Bearer '+self.token}
    def call(self,method,url,**kw):
        r=requests.request(method,url,headers=self.headers(),timeout=(15,90),**kw)
        if not r.ok:raise RuntimeError('drive_http_'+str(r.status_code))
        return r
    def validate(self):
        d=self.call('GET','https://www.googleapis.com/drive/v3/files/'+self.c['folder_id'],params={'fields':'id,mimeType,trashed,owners(emailAddress),permissions(type,role)'}).json()
        assert not d.get('trashed')and d['mimeType']=='application/vnd.google-apps.folder'
        assert not any(x['type']in('anyone','domain')for x in d.get('permissions',[])),'unsafe_drive_sharing'
        expected=self.c['expected_drive_account']
        assert any(x.get('emailAddress')==expected for x in d.get('owners',[])),'unexpected_drive_owner'
    def quota(self):
        d=self.call('GET','https://www.googleapis.com/drive/v3/about',params={'fields':'storageQuota'}).json()['storageQuota']
        return {'total':int(d['limit']),'used':int(d['usage']),'free':int(d['limit'])-int(d['usage'])}
    def list(self):
        rows=[];page=None
        while True:
            params={'q':"'"+self.c['folder_id']+"' in parents and trashed=false",'pageSize':1000,'fields':'nextPageToken,files(id,name,size,md5Checksum,sha256Checksum,parents,appProperties)'}
            if page:params['pageToken']=page
            d=self.call('GET','https://www.googleapis.com/drive/v3/files',params=params).json();rows+=d.get('files',[]);page=d.get('nextPageToken')
            if not page:return rows
    def upload(self,path,kind):
        path=Path(path);size=path.stat().st_size
        assert re.fullmatch(r'pult-(?:full|bootstrap|documents)-\d{8}T\d{6}Z\.tar\.gpg',path.name),'unexpected_upload_name'
        command([*self.transfer_args(),'copyto',path,'pult-drive:'+path.name,'--immutable'],timeout=5400,log=path.parent/'upload.log')
        items=[r for r in self.list()if r['name']==path.name];assert len(items)==1
        item=items[0];assert int(item['size'])==size
        self.call('PATCH','https://www.googleapis.com/drive/v3/files/'+item['id'],params={'fields':'id'},json={'appProperties':{'project':PROJECT,'kind':kind}})
        return item
    def transfer_args(self):
        return [self.c['rclone_binary'],'--config',str(private(self.c['rclone_config'])),
                '--drive-root-folder-id',self.c['folder_id'],'--drive-chunk-size','8M',
                '--transfers','1','--checkers','1','--retries','3','--low-level-retries','5',
                '--contimeout','15s','--timeout','5m','--tpslimit','2','--stats','0']
    def download(self,item,path):
        assert not Path(path).exists(),'download_destination_exists'
        items=[r for r in self.list()if r['id']==item['id']and r['name']==item['name']];assert len(items)==1
        command([*self.transfer_args(),'copyto','pult-drive:'+item['name'],path,'--immutable'],timeout=5400,log=Path(path).parent/'download.log')
        Path(path).chmod(0o600)
        assert Path(path).stat().st_size==int(item['size'])
    def delete(self,item):
        assert item.get('appProperties',{}).get('project')==PROJECT and self.c['folder_id']in item.get('parents',[]),'outside_backup_scope'
        live=self.call('GET','https://www.googleapis.com/drive/v3/files/'+item['id'],params={'fields':'id,name,size,parents,appProperties'}).json()
        assert live['name']==item['name']and int(live['size'])==int(item['size'])and self.c['folder_id']in live['parents']
        self.call('DELETE','https://www.googleapis.com/drive/v3/files/'+item['id'])
        check=requests.get('https://www.googleapis.com/drive/v3/files/'+item['id'],headers=self.headers(),timeout=(15,60));assert check.status_code==404,'deletion_not_verified'


def pg_environment():
    sys.path.insert(0,str(ROOT));from core import db
    p=parse_dsn(db.DATABASE_URL);assert p.get('dbname')=='mp_analytics','unexpected_database'
    e=os.environ.copy()
    for k,v in p.items():
        field={'dbname':'DATABASE','host':'HOST','port':'PORT','user':'USER','password':'PASSWORD','sslmode':'SSLMODE'}.get(k)
        if field:e['PG'+field]=v
    e['PGOPTIONS']='-c timezone=UTC -c statement_timeout=0'
    return db,e
def capture(directory):
    db,env=pg_environment()
    with db.get_conn()as conn:
        conn.set_session(isolation_level='REPEATABLE READ',readonly=True)
        with conn.cursor()as q:
            q.execute('SELECT current_database(),pg_export_snapshot(),pg_database_size(current_database())');name,snapshot,size=q.fetchone();assert name=='mp_analytics'
            q.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname NOT IN('pg_catalog','information_schema')AND schemaname NOT LIKE 'pg_toast%%' ORDER BY 1,2");tables=q.fetchall();counts={}
            for schema,table in tables:
                q.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(table)));counts[schema+'.'+table]=q.fetchone()[0]
            manifest={'database':name,'source_size':size,'snapshot_utc':now(),'row_counts':counts,'format':'custom zstd:3','dump':'database.dump'}
            atomic(directory/'manifest.json',manifest)
            command([PG/'pg_dump','--format=custom','--compress=zstd:3','--lock-wait-timeout=30000','--snapshot='+snapshot,'--file',directory/'database.dump'],env=env,timeout=5400,log=directory/'pg_dump.log')
    manifest.update(dump_bytes=(directory/'database.dump').stat().st_size,sha256=digest(directory/'database.dump'));atomic(directory/'manifest.json',manifest)
    return manifest


def document_files(root=ROOT):
    """Original labels/invoices; omit derived pairs and repeat API cache exports."""
    sources=[('print-labels',root/'.private/print_runtime/orders',{'.pdf'}),
             ('invoice-inbox',root/'invoice_bot/inbox',{'.pdf','.xls','.xlsx','.xml','.zip'}),
             ('invoice-mail',root/'invoice_bot/inbox_mail',{'.pdf','.xls','.xlsx','.xml','.zip'}),
             ('incoming',root/'incoming',{'.pdf','.xls','.xlsx','.xml','.zip'})]
    for label,base,extensions in sources:
        if not base.is_dir():continue
        for p in sorted(base.rglob('*')):
            if p.is_file()and not p.is_symlink()and p.suffix.lower()in extensions:
                assert p.resolve().is_relative_to(base.resolve()),'document_path_escape'
                yield p,'documents/'+label+'/'+p.relative_to(base).as_posix()


def deployment(directory,c):
    """Recover working code, including uncommitted files; never package the master key."""
    roots=['core','collectors','reports','ops','web','feedback_bot','invoice_bot','cf','prices','events','migrations','warehouse','returns']
    extensions={'.py','.sql','.html','.css','.js','.ts','.tsx','.jsx','.sh','.ps1','.bat','.cmd','.service','.timer','.md','.json','.toml','.yaml','.yml','.svg','.png','.jpg','.webp','.ico','.woff','.woff2','.caddy','.ini','.conf'}
    excluded={'.git','.private','.claude','__pycache__','node_modules','venv','logs','downloads','incoming','backups','pgdata','reports/private'}
    selected=set()
    for folder in roots:
        base=ROOT/folder
        if not base.is_dir():continue
        for parent,dirs,files in os.walk(base,followlinks=False):
            rel=Path(parent).relative_to(ROOT);dirs[:]=[x for x in dirs if x not in excluded and not x.startswith('.')and not (Path(parent)/x).is_symlink()]
            if 'private'in rel.parts:dirs[:]=[];continue
            for name in files:
                p=Path(parent)/name
                if not p.is_symlink()and p.suffix in extensions and not name.startswith('.')and p.stat().st_size<=10*1024*1024:selected.add(p)
    for p in ROOT.iterdir():
        if p.is_file()and not p.is_symlink()and(p.suffix in extensions or p.name.startswith('requirements'))and not p.name.startswith('.'):selected.add(p)
    head=command(['git','-C',ROOT,'rev-parse','HEAD']).decode().strip()
    bundle=directory/'repository.bundle';command(['git','-C',ROOT,'-c','pack.threads=1','-c','pack.windowMemory=64m','bundle','create',bundle,'--all'],timeout=300)
    # Role passwords/ownership are cluster globals, not part of a database dump.
    # They remain solely inside the encrypted recovery package.
    db,source_env=pg_environment();port=source_env.get('PGPORT','5432')
    assert source_env.get('PGHOST','127.0.0.1')in('127.0.0.1','localhost','/var/run/postgresql')
    role_env={k:v for k,v in os.environ.items()if not k.startswith('PG')}
    roles=command(['runuser','-u','postgres','--',PG/'pg_dumpall','--roles-only','--host=/var/run/postgresql','--port='+port],env=role_env,timeout=90)
    configs=[ROOT/'.env',STATE/'config.json',Path('/etc/caddy/Caddyfile'),private(c['rclone_config'])]
    configs+=list(Path('/etc/caddy/private').glob('*.caddy'))
    configs+=list(Path('/etc/systemd/system').glob('mp-*.service'))+list(Path('/etc/systemd/system').glob('mp-*.timer'))
    configs+=list(Path('/etc/systemd/system').glob('customer-guard*.service'))+list(Path('/etc/systemd/system').glob('customer-guard*.timer'))
    configs+=list(Path('/etc/systemd/system').glob('pult-backup.*'))
    configs+=[Path('/srv/beyindi/config/Beyindi.caddy')]
    # Preserve the existing pinned edge transport and recovery credentials inside
    # the encrypted archive; the encryption passphrase is always excluded.
    configs+=[Path('/root/sokol-recovery/project/deploy/recovery/beget_proxy.py'),
              Path('/srv/sokol/access/beget-proxy/connection.txt'),
              Path('/srv/sokol/access/beget-proxy/known_hosts')]
    files={};out=directory/'deployment.tar.gz'
    with tarfile.open(out,'w:gz',compresslevel=6)as tar:
        info=tarfile.TarInfo('configuration/postgresql-roles.sql');info.size=len(roles);info.mode=0o600;tar.addfile(info,io.BytesIO(roles))
        for p in sorted(selected):
            before=digest(p);tar.add(p,arcname='code/'+str(p.relative_to(ROOT)),recursive=False);assert digest(p)==before,'working_file_changed';files[str(p)]=before
        for p in configs:
            if not p.is_file():continue
            source=p.resolve();assert source!=Path(c['key_file']).resolve()
            before=digest(source);tar.add(source,arcname='configuration/'+str(p).lstrip('/'),recursive=False);assert digest(source)==before;files[str(source)]=before
        for p,arcname in document_files():
            before=digest(p);tar.add(p,arcname=arcname,recursive=False);assert digest(p)==before,'document_changed_during_capture';files[str(p)]=before
        auth=json.loads(private(c['auth_file']).read_text());stable={k:auth[k]for k in ['client_id','client_secret','refresh_token']}
        blob=json.dumps(stable).encode();info=tarfile.TarInfo('configuration/drive-auth.json');info.size=len(blob);info.mode=0o600;tar.addfile(info,io.BytesIO(blob))
        tar.add(bundle,arcname='repository.bundle',recursive=False)
        requirements='\n'.join(sorted(d.metadata['Name']+'=='+d.version for d in importlib.metadata.distributions()if d.metadata.get('Name'))).encode()
        info=tarfile.TarInfo('requirements-installed.txt');info.size=len(requirements);info.mode=0o600;tar.addfile(info,io.BytesIO(requirements))
        # Existing pinned SSH transport, credentials never copied to logs or command args.
        sys.path.insert(0,'/root/sokol-recovery/project/deploy/recovery');import beget_proxy as edge
        edge.WORK=directory;edge.ROOT=ROOT
        script="python3 - <<'EDGE'\nimport json\nfrom pathlib import Path\nfiles=['/etc/nginx/nginx.conf','/srv/sokol/proxy/stream.conf','/etc/nginx/conf.d/mp-beyindi-edge-http.conf']\nprint(json.dumps({p:Path(p).read_text()for p in files}))\nEDGE\n"
        data=edge.ssh(script,'edge-config',timeout=45).encode();info=tarfile.TarInfo('configuration/russian-ingress.json');info.size=len(data);info.mode=0o600;tar.addfile(info,io.BytesIO(data))
    assert command(['git','-C',ROOT,'rev-parse','HEAD']).decode().strip()==head,'git_head_changed'
    assert all(digest(Path(p))==h for p,h in files.items()),'deployment_changed_during_capture'
    atomic(directory/'deployment-manifest.json',{'captured_at':now(),'git_head':head,'files':files,'bytes':out.stat().st_size,'sha256':digest(out),'master_key_included':False})
    return out


def encrypt(source,destination,key,directory):
    command(['gpg','--homedir',directory/'gnupg','--batch','--yes','--pinentry-mode','loopback','--passphrase-file',private(key),'--cipher-algo','AES256','--compress-algo','none','--output',destination,'--symmetric',source],log=directory/'gpg-encrypt.log')
def unpack_verified(encrypted,directory,key):
    plain=directory/'restored.tar';command(['gpg','--homedir',directory/'gnupg','--batch','--yes','--pinentry-mode','loopback','--passphrase-file',private(key),'--output',plain,'--decrypt',encrypted],log=directory/'gpg-decrypt.log')
    target=directory/'download-restored';target.mkdir(mode=0o700)
    with tarfile.open(plain,'r:')as tar:
        members=tar.getmembers();assert {x.name for x in members}=={'database.dump','manifest.json','deployment.tar.gz','deployment-manifest.json','RESTORE.txt'}
        for member in members:
            assert member.isfile()and not member.issym()and not member.islnk()
            with tar.extractfile(member)as inp,(target/member.name).open('xb')as out:shutil.copyfileobj(inp,out,1024*1024)
    plain.unlink();return target
def restore_test(directory):
    manifest=json.loads((directory/'manifest.json').read_text());assert manifest['database']=='mp_analytics'and digest(directory/'database.dump')==manifest['sha256']
    person=pwd.getpwnam('postgres');cluster=Path(tempfile.mkdtemp(prefix='pult-backup-verify-',dir='/var/tmp'));os.chown(cluster,person.pw_uid,person.pw_gid);cluster.chmod(0o700)
    data=cluster/'data';sock=cluster/'socket';sock.mkdir(mode=0o700);os.chown(sock,person.pw_uid,person.pw_gid)
    start=time.monotonic();running=False
    try:
        command(['runuser','-u','postgres','--',PG/'initdb','-D',data,'--auth-local=trust','--auth-host=reject','--encoding=UTF8','--no-locale'],timeout=90,log=directory/'initdb.log')
        opts=f"-k {sock} -p 65432 -c listen_addresses='' -c max_worker_processes=0 -c shared_preload_libraries='' -c autovacuum=off -c shared_buffers=64MB -c max_connections=10 -c unix_socket_permissions=0700"
        command(['unshare','--net','runuser','-u','postgres','--',PG/'pg_ctl','-D',data,'-l',cluster/'server.log','-o',opts,'-w','start'],timeout=90,log=directory/'pg-start.log');running=True
        e=os.environ.copy()
        for k in list(e):
            if k.startswith('PG'):e.pop(k)
        e.update(PGHOST=str(sock),PGPORT='65432',PGUSER='postgres',PGDATABASE='verify')
        locale=manifest.get('database_locale')or config().get('database_locale')
        args=[PG/'createdb','--template=template0']
        if locale:args+=['--encoding='+locale['encoding'],'--lc-collate='+locale['collate'],'--lc-ctype='+locale['ctype']]
        command([*args,'verify'],env=e,timeout=60)
        command([PG/'pg_restore','--exit-on-error','--no-owner','--no-acl','--dbname=verify',directory/'database.dump'],env=e,timeout=5400,log=directory/'pg_restore.log')
        with psycopg2.connect(host=str(sock),port=65432,user='postgres',dbname='verify')as conn,conn.cursor()as q:
            q.execute("SELECT current_database(),current_setting('data_directory'),current_setting('listen_addresses'),current_setting('max_worker_processes')");actual=q.fetchone();assert actual==('verify',str(data),'','0')
            q.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname NOT IN('pg_catalog','information_schema')AND schemaname NOT LIKE 'pg_toast%%'");tables=q.fetchall();counts={}
            for schema,table in tables:
                q.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(table)));counts[schema+'.'+table]=q.fetchone()[0]
            assert counts==manifest['row_counts'],'restored_row_counts_differ'
        dep=json.loads((directory/'deployment-manifest.json').read_text());assert digest(directory/'deployment.tar.gz')==dep['sha256']
        return {'passed':True,'tables':len(counts),'all_table_row_counts_match':True,'database':'isolated verify','network':'none','workers_started':False,'deployment_hash_matches':True,'seconds':round(time.monotonic()-start,1),'verified_at':now()}
    finally:
        if running:command(['runuser','-u','postgres','--',PG/'pg_ctl','-D',data,'-m','immediate','-w','stop'],timeout=60)
        assert cluster.parent==Path('/var/tmp')and cluster.name.startswith('pult-backup-verify-');shutil.rmtree(cluster)


def retention_selection(records,daily=3,weekly=0,monthly=0,max_age_hours=None,reference=None):
    chosen=set();days=set();weeks=set();months=set()
    cutoff=(reference or dt.datetime.now(dt.timezone.utc))-dt.timedelta(hours=max_age_hours)if max_age_hours is not None else None
    for rec in sorted(records,key=lambda r:r['snapshot_utc'],reverse=True):
        if cutoff and dt.datetime.fromisoformat(rec['snapshot_utc'])<cutoff:continue
        day=dt.datetime.fromisoformat(rec['snapshot_utc']).date();week=day.isocalendar()[:2];month=(day.year,day.month)
        if day not in days and len(days)<daily:days.add(day);chosen.add(rec['file_id'])
        if week not in weeks and len(weeks)<weekly:weeks.add(week);chosen.add(rec['file_id'])
        if month not in months and len(months)<monthly:months.add(month);chosen.add(rec['file_id'])
    return chosen
def rotate(drive,c,new_success):
    # Only receipts from an actual external restore count as eligible for rotation.
    records=[]
    for p in (LOCAL/'success').glob('*.json'):
        rec=json.loads(p.read_text())
        if rec.get('restore',{}).get('passed')and rec.get('download_sha256_verified'):records.append(rec)
    keep=retention_selection(records,**c['retention'])|{new_success['file_id']}
    items={x['id']:x for x in drive.list()};removed=[]
    for rec in records:
        if rec['file_id']in keep or rec['file_id']not in items:continue
        item=items[rec['file_id']];assert item['name']==rec['name']and int(item['size'])==rec['bytes']
        drive.delete(item);removed.append(item['name'])
    dependencies={ident for parent,ids in c.get('initial_dependencies',{}).items()if parent in keep for ident in ids}
    cutoff=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=c['retention']['max_age_hours'])
    for item in items.values():
        if item['id']in dependencies or item.get('appProperties',{}).get('kind')not in('bootstrap','documents'):continue
        match=re.fullmatch(r'pult-(?:bootstrap|documents)-(\d{8}T\d{6}Z)\.tar\.gpg',item['name'])
        if match and dt.datetime.strptime(match[1],'%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)<cutoff:
            drive.delete(item);removed.append(item['name'])
    return {'retained_file_ids':sorted(keep),'deleted':removed,'permanent_deletion':True,'max_age_hours':72}
def rotate_local(new_success):
    records=[]
    for p in (LOCAL/'success').glob('*.json'):
        r=json.loads(p.read_text())
        if r.get('restore',{}).get('passed')and r.get('download_sha256_verified')and r.get('local_directory'):records.append(r)
    keep_ids=retention_selection(records,**config()['retention'])
    keep={r['local_directory']for r in records if r['file_id']in keep_ids}|{new_success['local_directory']};removed=[]
    for r in records:
        directory=Path(r['local_directory'])
        if str(directory)in keep or not directory.exists():continue
        assert directory.parent==LOCAL and re.fullmatch(r'\d{8}T\d{6}Z',directory.name),'unsafe_local_retention_path'
        assert digest(directory/'manifest.json')==r['manifest_sha256'],'local_receipt_changed'
        shutil.rmtree(directory);removed.append(directory.name)
    return removed
def once(existing=None):
    c=config();LOCAL.mkdir(mode=0o700,parents=True,exist_ok=True);(LOCAL/'success').mkdir(mode=0o700,exist_ok=True)
    with (LOCAL/'backup.lock').open('a')as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ');directory=Path(existing)if existing else LOCAL/stamp
        atomic(LOCAL/'latest_attempt.json',{'phase':'capture','started_at':now(),'directory':str(directory)})
        if existing:
            assert directory.parent==LOCAL and re.fullmatch(r'\d{8}T\d{6}Z',directory.name)
            manifest=json.loads((directory/'manifest.json').read_text());assert manifest['database']=='mp_analytics'and digest(directory/'database.dump')==manifest['sha256']
        else:
            db,_=pg_environment();required=db.query('SELECT pg_database_size(current_database()) n')[0]['n']*2+2*1024**3
            assert shutil.disk_usage(LOCAL).free>=required,'insufficient_local_space_for_safe_restore'
            directory.mkdir(mode=0o700);manifest=capture(directory)
        atomic(LOCAL/'latest_attempt.json',{'phase':'deployment','started_at':now(),'directory':str(directory)})
        manifest['database_locale']=c['database_locale'];atomic(directory/'manifest.json',manifest)
        drive=Drive(c);drive.validate();deployment(directory,c)
        restore_text='Pult: restore only into a new isolated PostgreSQL16 cluster. Disable all workers/timers before changing DATABASE_URL. Never blindly overwrite shared Caddy or nginx config. This archive uses the owner-held offline Sokol backup passphrase; its master key is not included. Code includes working files as well as a Git bundle.\n'
        (directory/'RESTORE.txt').write_text(restore_text)
        plain=directory/'payload.tar'
        with tarfile.open(plain,'w')as tar:
            for name in ['database.dump','manifest.json','deployment.tar.gz','deployment-manifest.json','RESTORE.txt']:tar.add(directory/name,arcname=name,recursive=False)
        (directory/'gnupg').mkdir(mode=0o700,exist_ok=True)
        atomic(LOCAL/'latest_attempt.json',{'phase':'encryption','started_at':now(),'directory':str(directory)})
        encrypted=directory/('pult-full-'+stamp+'.tar.gpg');encrypt(plain,encrypted,c['key_file'],directory);plain.unlink()
        size=encrypted.stat().st_size;before=drive.quota();assert before['free']>=size+c['min_free_bytes'],'insufficient_drive_space'
        atomic(LOCAL/'latest_attempt.json',{'phase':'upload','started_at':now(),'bytes':size})
        item=drive.upload(encrypted,'full');atomic(LOCAL/'latest_attempt.json',{'phase':'download_verification','started_at':now(),'file_id':item['id']})
        downloaded=directory/'download.gpg';drive.download(item,downloaded);assert digest(downloaded)==digest(encrypted)
        atomic(LOCAL/'latest_attempt.json',{'phase':'isolated_restore','started_at':now(),'file_id':item['id']})
        restored=unpack_verified(downloaded,directory,c['key_file']);proof=restore_test(restored)
        receipt={'name':encrypted.name,'file_id':item['id'],'bytes':size,'sha256':digest(encrypted),'snapshot_utc':manifest['snapshot_utc'],'completed_at':now(),'source_database':'mp_analytics','tables':len(manifest['row_counts']),'download_sha256_verified':True,'restore':proof,'deployment_bytes':(directory/'deployment.tar.gz').stat().st_size,'folder_id':c['folder_id'],'local_directory':str(directory),'manifest_sha256':digest(directory/'manifest.json')}
        atomic(LOCAL/'success'/(stamp+'.json'),receipt);atomic(LOCAL/'latest_success.json',receipt)
        receipt['rotation']=rotate(drive,c,receipt);receipt['quota_after']=drive.quota();atomic(LOCAL/'latest_success.json',receipt);atomic(LOCAL/'latest_attempt.json',{'phase':'complete','ok':True,'completed_at':now(),'file_id':item['id']})
        # Downloaded mirror and plaintext verification copies are task-owned scratch only.
        downloaded.unlink();shutil.rmtree(restored)
        receipt['local_retention_removed']=rotate_local(receipt);atomic(LOCAL/'latest_success.json',receipt)
        print(json.dumps({'ok':True,'bytes':size,'tables':receipt['tables'],'restore':proof,'file_id':item['id'],'deployment_bytes':receipt['deployment_bytes'],'free_bytes':receipt['quota_after']['free']}),flush=True)


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('action',choices=['once','status']);p.add_argument('--existing');a=p.parse_args()
    if a.action=='status':
        print(json.dumps(json.loads((LOCAL/'latest_attempt.json').read_text())));raise SystemExit()
    try:once(a.existing)
    except Exception as error:
        atomic(LOCAL/'latest_attempt.json',{'phase':'failed','ok':False,'error_type':type(error).__name__,'at':now()})
        import traceback
        with (LOCAL/'failure-private.log').open('a')as diagnostic:traceback.print_exc(file=diagnostic)
        print(json.dumps({'ok':False,'error_type':type(error).__name__}),file=sys.stderr);raise SystemExit(1)
