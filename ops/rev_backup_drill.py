#!/usr/bin/env python3
# поток: rev
"""Download five approved anchors and verify them in a separate PostgreSQL cluster.

No Drive DELETE/PATCH, no live database connection, no application/worker startup.
The existing archive decoder and table fingerprints are reused.
"""
from pathlib import Path
import csv,datetime,json,os,pwd,subprocess,sys,tarfile
import psycopg2
from psycopg2 import sql
import rev_backup as backup
import rev_restore as restore

ROOT=Path(__file__).resolve().parents[1]
STATE=Path('/root/rev-recovery/backup-maintenance-20261108')
PGROOT=ROOT/'backups/pgv'
BIN=Path('/usr/lib/postgresql/16/bin')
ANCHORS=('20261108T110149Z','20261105T180142Z','20261105T230106Z','20261106T230148Z','20261107T230002Z')
NAMES={f'rev-backup-{stamp}.tar.gz.gpg' for stamp in ANCHORS}


def cmd(args,**kw):return backup.run(list(map(str,args)),**kw)


def cluster_start(resume=False):
    if PGROOT.is_symlink() or not str(PGROOT).startswith(str(ROOT/'backups')+'/'):raise RuntimeError('Invalid isolated cluster path')
    PGROOT.parent.mkdir(exist_ok=True,mode=0o755)
    if PGROOT.exists():
        if not resume or not (STATE/'anchors-result.json').is_file() or (PGROOT/'data/postmaster.pid').exists():raise RuntimeError('Drill cluster exists or is active; inspect durable proof before resuming')
        cmd(['runuser','-u','postgres','--',BIN/'pg_ctl','-D',PGROOT/'data','-l',PGROOT/'postgres.log','-w','start'])
        return {'host':str(PGROOT/'s'),'port':16543,'user':'postgres','dbname':'postgres'}
    PGROOT.mkdir(mode=0o700);uid=pwd.getpwnam('postgres');os.chown(PGROOT,uid.pw_uid,uid.pw_gid)
    sock=PGROOT/'s';sock.mkdir(mode=0o700);os.chown(sock,uid.pw_uid,uid.pw_gid)
    cmd(['runuser','-u','postgres','--',BIN/'initdb','-D',PGROOT/'data','-A','trust','-U','postgres','--no-locale','--encoding=UTF8'])
    conf=PGROOT/'data/postgresql.conf'
    with conf.open('a') as stream:stream.write("\nlisten_addresses=''\nport=16543\nunix_socket_directories='"+str(sock)+"'\nunix_socket_permissions=0700\nshared_buffers='32MB'\nwork_mem='4MB'\nmaintenance_work_mem='32MB'\nmax_connections=10\nautovacuum=off\n")
    cmd(['runuser','-u','postgres','--',BIN/'pg_ctl','-D',PGROOT/'data','-l',PGROOT/'postgres.log','-w','start'])
    return {'host':str(sock),'port':16543,'user':'postgres','dbname':'postgres'}


def verify_one(item,cfg,params,local_file=None):
    name=item['name'];prefix='rev-db-backup-' if local_file else 'rev-backup-';stamp=name.removeprefix(prefix).removesuffix('.tar.gz.gpg')
    encrypted=Path(local_file) if local_file else STATE/'downloads'/name
    if local_file:
        if encrypted.parent!=STATE/'db-only' or backup.sha256(encrypted)!=item['sha256Checksum']:raise RuntimeError('Invalid local measurement source')
    else:backup.download_drive(item,encrypted,cfg)
    stage=STATE/(('verified-db-' if local_file else 'verified-')+stamp);stage.mkdir(mode=0o700)
    plain=stage/'recovery.tar.gz';backup.decrypt(encrypted,plain,backup.key_path(cfg))
    with tarfile.open(plain,'r:gz') as archive:restore.extract(archive,stage)
    manifest=json.loads((stage/'manifest.json').read_text())
    if backup.sha256(stage/'database.dump')!=manifest['dump_sha256'] or backup.sha256(stage/'code.tar')!=manifest['code_sha256']:raise RuntimeError('Downloaded plaintext manifest integrity failed')
    if manifest['created_at_utc']!=stamp:raise RuntimeError('Snapshot timestamp does not match the selected anchor')
    code=stage/'restored-code';code.mkdir(mode=0o700)
    with tarfile.open(stage/'code.tar') as archive:restore.extract(archive,code)
    checked=0
    for source in code.rglob('*.py'):compile(source.read_bytes(),str(source),'exec');checked+=1
    runtime=stage/'runtime/venv/bin/python'
    if manifest.get('runtime_included') and not runtime.exists():raise RuntimeError('Restored Python runtime missing')
    database='rev_restore_drill_'+('db_' if local_file else '')+stamp.lower()
    connection=psycopg2.connect(**params);connection.autocommit=True
    with connection.cursor() as cur:cur.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(database)))
    connection.close()
    env={k:v for k,v in os.environ.items() if not k.startswith('PG')};env.update(PGHOST=params['host'],PGPORT=str(params['port']),PGUSER=params['user'],PGDATABASE=database)
    cmd([BIN/'pg_restore','--exit-on-error','--no-owner','--no-acl','--jobs=1','--dbname='+database,stage/'database.dump'],env=env,timeout=1800)
    with psycopg2.connect(**dict(params,dbname=database)) as con:
        con.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with con.cursor() as cur:
            actual=backup.fingerprint(cur,tuple(k for k,v in manifest['tables'].items() if 'digest' in v))
            if actual!=manifest['tables']:raise RuntimeError('Restored table counts or critical digests differ')
    proof={'status':'PASS','name':name,'file_id':item.get('id'),'bytes':int(item['size']),'sha256':backup.sha256(encrypted),
        'snapshot_utc':stamp,'downloaded_from_drive':local_file is None,'decryption':'PASS','code_and_dump_sha':'PASS',
        'restored_tables':len(actual),'critical_digests':sum('digest' in v for v in actual.values()),
        'compiled_python_files':checked,'runtime_present':runtime.exists(),'database':database,
        'isolated_socket':params['host'],'live_database_connected':False,'services_started':False,
        'model_calls':0,'publications_enabled':False,'verified_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'database_dump_bytes':(stage/'database.dump').stat().st_size,'git_commit':manifest['git_commit']}
    backup.atomic_json(STATE/(stamp+('-db-restore.json' if local_file else '-restore.json')),proof);print(json.dumps(proof),flush=True)
    return proof


def main():
    os.umask(0o077);STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    if Path.cwd()!=ROOT:raise RuntimeError('Run from the Rev checkout')
    cfg=backup.config();items=backup.drive_inventory(cfg);selected={x['name']:x for x in items if x['name'] in NAMES}
    if len(selected)!=5 or len([x for x in items if x['name'] in NAMES])!=5:raise RuntimeError('Five anchors are missing or ambiguous')
    backup.atomic_json(STATE/'drive-inventory.json',items)
    for directory in ['downloads','gnupg']:(STATE/directory).mkdir(mode=0o700,exist_ok=True)
    os.environ['GNUPGHOME']=str(STATE/'gnupg')
    params=cluster_start();proofs=[]
    try:
        for stamp in ANCHORS:proofs.append(verify_one(selected[f'rev-backup-{stamp}.tar.gz.gpg'],cfg,params))
        candidates=[x for x in items if x['name'] not in NAMES and x.get('appProperties',{}).get('project')=='mp-analytics-rev' and x['name'].startswith('rev-backup-')]
        if len(items)!=64 or len(candidates)!=59:raise RuntimeError('Drive set changed; deletion manifest requires fresh owner review')
        manifest=[{'file_id':x['id'],'name':x['name'],'bytes':int(x['size']),'created_at':x['createdTime'],
            'reason':'Extra intraday full snapshot; verified daily anchor retained','sha256':x.get('sha256Checksum')} for x in candidates]
        backup.atomic_json(STATE/'deletion-candidates-59.json',{'authorized_to_delete':False,'candidates':manifest,'protected':proofs,'total_bytes':sum(x['bytes'] for x in manifest)})
        with (STATE/'deletion-candidates-59.csv').open('w') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(manifest[0]));writer.writeheader();writer.writerows(manifest)
        backup.atomic_json(STATE/'anchors-result.json',{'status':'PASS','anchors':proofs,'protected_count':5,'candidate_count':59,'candidate_bytes':sum(x['bytes'] for x in manifest),'drive_files_deleted':0})
    finally:cmd(['runuser','-u','postgres','--',BIN/'pg_ctl','-D',PGROOT/'data','-m','fast','-w','stop'])


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'status':'FAIL','error':str(error) if isinstance(error,backup.BackupError) else type(error).__name__,'no_deletion_performed':True}),flush=True)
        raise SystemExit(1)
