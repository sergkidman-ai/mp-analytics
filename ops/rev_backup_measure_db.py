#!/usr/bin/env python3
# поток: rev
"""Measure one current consistent DB snapshot; restore locally, never upload it."""
import datetime,json,os
from pathlib import Path
import rev_backup as backup
import rev_backup_drill as drill

def main():
    os.umask(0o077)
    if Path.cwd()!=drill.ROOT:raise RuntimeError('Run from the Rev checkout')
    anchors=json.loads((drill.STATE/'anchors-result.json').read_text())
    if anchors['status']!='PASS' or len(anchors['anchors'])!=5:raise RuntimeError('Verified full anchor is required')
    cfg=backup.config();folder=drill.STATE/'db-only';folder.mkdir(mode=0o700,exist_ok=True)
    cfg.update(REV_BACKUP_DIR=str(folder),REV_BACKUP_KIND='database')
    os.environ['GNUPGHOME']=str(drill.STATE/'gnupg')
    row=backup.backup(drill.ROOT,cfg,local_only=True,include_runtime=False)
    file=Path(row['bundle']);item={'name':file.name,'size':str(file.stat().st_size),'sha256Checksum':row['bundle_sha256']}
    params=drill.cluster_start(resume=True)
    try:proof=drill.verify_one(item,cfg,params,local_file=file)
    finally:drill.cmd(['runuser','-u','postgres','--',drill.BIN/'pg_ctl','-D',drill.PGROOT/'data','-m','fast','-w','stop'])
    full=anchors['anchors'][0]
    proof.update(base_full_sha256=full['sha256'],same_exported_snapshot_method=True,
        same_postgres_major=16,no_venv=True,no_drive_upload=True,timer_enabled=False,
        reduction_vs_full_percent=round((1-file.stat().st_size/full['bytes'])*100,2),
        retained_12_db_snapshots_bytes=12*file.stat().st_size)
    backup.atomic_json(drill.STATE/'database-only-result.json',proof)
    print(json.dumps(proof),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'status':'FAIL','error':backup.safe_error(error)}),flush=True)
        raise SystemExit(1)
