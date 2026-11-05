#!/usr/bin/env python3
# поток: rev
"""Restore an encrypted Rev snapshot into a NEW database; never starts services."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import time
import uuid

import psycopg2
from psycopg2 import sql
from dotenv import dotenv_values, set_key
from psycopg2.extensions import make_dsn
from rev_backup import (BackupError, ROOT, config, connection, decrypt, fingerprint,
                        key_path, pg_environment, run, sha256)


def safe_member(member):
    p=PurePosixPath(member.name)
    if p.is_absolute() or '..' in p.parts or member.isdev() or member.isfifo():
        raise BackupError('Unsafe archive member rejected')
    if member.islnk():raise BackupError('Archive hard links are not accepted')
    if member.issym():
        target=PurePosixPath(member.linkname)
        if '..' in target.parts or (target.is_absolute() and str(target) not in ('/usr/bin/python3','/usr/bin/python3.10')):
            raise BackupError('Unsafe archive symbolic link rejected')
        if not str(p).startswith('runtime/venv/'):
            raise BackupError('Only runtime symlinks are accepted')
    return True


def extract(archive,dest):
    """Validate the whole archive before extraction, including symlink descendants."""
    members=archive.getmembers();links=set()
    for member in members:
        safe_member(member)
        if member.issym():links.add(PurePosixPath(member.name))
    for member in members:
        if any(parent in links for parent in PurePosixPath(member.name).parents):
            raise BackupError('Archive contains a file below a symbolic link')
    archive.extractall(dest,members=members)


def validate_target(target,source):
    import re
    if not re.fullmatch(r'rev_restore_(?:drill|recovery)_[A-Za-z0-9_]+',target) or target==source:
        raise BackupError('Target must be a new rev_restore_drill_* or rev_restore_recovery_* database')


def create_database(params,target):
    admin=dict(params,dbname='postgres')
    conn=psycopg2.connect(**admin);conn.autocommit=True
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM pg_database WHERE datname=%s',(target,))
            if cur.fetchone():raise BackupError('Target database already exists; no overwrite allowed')
            cur.execute('SELECT rolcreatedb OR rolsuper,current_user FROM pg_roles WHERE rolname=current_user')
            can_create,owner=cur.fetchone()
            if can_create:
                cur.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0 ENCODING \'UTF8\'').format(sql.Identifier(target)))
                return
        # Standard local postgres administration; no grants, socket or pg_hba changes.
        host=params.get('host','')
        if os.geteuid()!=0 or host not in ('','localhost','127.0.0.1','::1','/var/run/postgresql'):
            raise BackupError('Database creation requires CREATEDB or local root postgres administration')
        env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
        run(['runuser','-u','postgres','--','createdb','--maintenance-db=postgres',
             '--owner='+owner,'--template=template0','--encoding=UTF8',target],env=env,timeout=60)
    finally:conn.close()


def install_files(stage,root,params,target,manifest):
    root=Path(root)
    if root.exists() and any(root.iterdir()):raise BackupError('Recovery directory must be absent or empty')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tarfile.open(stage/'code.tar') as archive:extract(archive,root)
    if (stage/'runtime/venv').exists():
        import shutil
        shutil.move(str(stage/'runtime/venv'),root/'venv')
    import shutil
    shutil.copyfile(stage/'secrets/runtime.env',root/'.env');os.chmod(root/'.env',0o600)
    set_key(str(root/'.env'),'DATABASE_URL',make_dsn(**dict(params,dbname=target)))
    set_key(str(root/'.env'),'FEEDBACK_LIVE_SEND','0')
    set_key(str(root/'.env'),'FEEDBACK_RESTORE_HOLD','1')
    set_key(str(root/'.env'),'FEEDBACK_CALLBACK_EPOCH',uuid.uuid4().hex[:16])
    if (stage/'secrets/backup').exists():shutil.copytree(stage/'secrets/backup',root/'recovery-backup-config')
    source_root=manifest['source_root']
    runtime_path='/opt/mp-analytics/venv'
    unitdir=root/'recovery-systemd';unitdir.mkdir(mode=0o700)
    for path in (stage/'systemd').glob('feedback-*'):
        text=path.read_text().replace(source_root,str(root)).replace(runtime_path,str(root/'venv'))
        (unitdir/path.name).write_text(text)
    if (root/'venv/bin').exists():
        for path in (root/'venv/bin').iterdir():
            if path.is_file() and not path.is_symlink():
                data=path.read_bytes()
                old=b'#!'+runtime_path.encode()+b'/bin/'
                if data.startswith(old):path.write_bytes(data.replace(old,b'#!'+str(root/'venv').encode()+b'/bin/',1))
    return {'directory':str(root),'publications_enabled':False,'callback_epoch_rotated':True,'units_installed':False}


def restore(bundle,target,root=ROOT,cfg=None,install_root=None):
    os.umask(0o077);started=time.monotonic();cfg=cfg or config()
    _dsn,params=connection(root);validate_target(target,params['dbname'])
    if install_root and Path(install_root).exists() and any(Path(install_root).iterdir()):
        raise BackupError('Recovery directory must be absent or empty')
    bundle=Path(bundle)
    with tempfile.TemporaryDirectory(prefix='rev-restore-',dir='/root/rev-recovery') as directory:
        stage=Path(directory);plain=stage/'recovery.tar.gz'
        decrypt(bundle,plain,key_path(cfg))
        with tarfile.open(plain,'r:gz') as archive:extract(archive,stage)
        manifest=json.loads((stage/'manifest.json').read_text())
        if sha256(stage/'database.dump')!=manifest['dump_sha256'] or sha256(stage/'code.tar')!=manifest['code_sha256']:
            raise BackupError('Snapshot integrity mismatch')
        create_database(params,target)
        run(['pg_restore','--exit-on-error','--no-owner','--no-acl','--jobs=2','--dbname='+target,str(stage/'database.dump')],
            env=pg_environment(params,target),timeout=3600)
        with psycopg2.connect(**dict(params,dbname=target)) as conn:
            conn.set_session(isolation_level='REPEATABLE READ',readonly=True)
            with conn.cursor() as cur:
                actual=fingerprint(cur,tuple(name for name,record in manifest['tables'].items() if 'digest' in record))
                if actual!=manifest['tables']:raise BackupError('Restored row counts or critical content digests differ')
                cur.execute('SELECT pg_database_size(current_database())');size=cur.fetchone()[0]
        result={'ok':True,'bundle_sha256':sha256(bundle),'target_database':target,
                'source_snapshot_utc':manifest['created_at_utc'],'verified_tables':len(actual),
                'verified_critical_digests':sum('digest' in x for x in actual.values()),
                'restored_bytes':size,'services_started':False,'publications_enabled':False}
        if install_root:result['files']=install_files(stage,install_root,params,target,manifest)
        result['restore_seconds']=round(time.monotonic()-started,3)
        return result


def main():
    p=argparse.ArgumentParser();p.add_argument('bundle');p.add_argument('--database',required=True)
    p.add_argument('--root',default=str(ROOT));p.add_argument('--config');p.add_argument('--install-root')
    args=p.parse_args()
    try:print(json.dumps(restore(args.bundle,args.database,args.root,config(args.config),args.install_root)))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc) if isinstance(exc,BackupError) else type(exc).__name__}));raise SystemExit(1)
if __name__=='__main__':main()
