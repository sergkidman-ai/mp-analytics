#!/usr/bin/env python3
# поток: rev
"""Promote only downloaded/restored evidence to the last-success marker.

No upload or deletion. Preserves the last failed attempt and every protected anchor.
"""
import fcntl,json,os,shutil
from pathlib import Path
from dotenv import set_key
import rev_backup as backup
from rev_backup_drill import ROOT,STATE,ANCHORS


def main():
    os.umask(0o077)
    if Path.cwd()!=ROOT:raise RuntimeError('Use the Rev checkout')
    evidence=json.loads((STATE/'anchors-result.json').read_text())
    if evidence.get('status')!='PASS' or {x['snapshot_utc'] for x in evidence['anchors']}!=set(ANCHORS):raise RuntimeError('Five complete restore proofs required')
    for row in evidence['anchors']:
        file=STATE/'downloads'/row['name']
        if row['status']!='PASS' or not row['downloaded_from_drive'] or backup.sha256(file)!=row['sha256']:raise RuntimeError('Restored anchor integrity mismatch')
    cfg=backup.config();directory=Path(cfg.get('REV_BACKUP_DIR','/root/rev-recovery/backups'))
    if directory!=Path('/root/rev-recovery/backups'):raise RuntimeError('Unexpected active backup directory')
    with (directory/'.backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        old=directory/'latest.json';attempt=directory/'latest_attempt.json'
        if not attempt.exists() and old.is_file():backup.atomic_json(attempt,json.loads(old.read_text()))
        anchor=next(x for x in evidence['anchors'] if x['snapshot_utc']=='20261108T110149Z')
        target=directory/anchor['name']
        if target.exists():
            if backup.sha256(target)!=anchor['sha256']:raise RuntimeError('Existing local anchor differs; not overwritten')
        else:shutil.copyfile(STATE/'downloads'/anchor['name'],target);target.chmod(0o600)
        row={'kind':'full','bundle':str(target),'bytes':anchor['bytes'],'bundle_sha256':anchor['sha256'],
            'source_snapshot_utc':anchor['snapshot_utc'],'git_commit':anchor['git_commit'],'local_only':False,
            'offsite':{'method':'gdrive','verified':True,'readback_verified':True,'file_id':anchor['file_id'],'sha256':anchor['sha256']},
            'restore_verified':True,'restore_proof':str(STATE/'20261108T110149Z-restore.json')}
        success=directory/'latest_success.json'
        if success.exists() and json.loads(success.read_text()).get('source_snapshot_utc','')>row['source_snapshot_utc']:raise RuntimeError('Newer success retained; do not regress its marker')
        for file in [success,directory/'latest.json',directory/'last_restored_success.json',target.with_suffix('.gpg.json')]:backup.atomic_json(file,row)
        backup.atomic_json(directory/'protected-anchors.json',evidence['anchors'])
    config=Path('/root/.config/rev-recovery/backup.env');rollback=STATE/'backup.env.before'
    if not rollback.exists():shutil.copy2(config,rollback)
    for name,value in [('REV_BACKUP_KEEP_RECENT','2'),('REV_BACKUP_KEEP_DAYS','7'),('REV_BACKUP_KEEP_WEEKS','4')]:set_key(str(config),name,value)
    policy={'full_daily':7,'full_weekly':4,'weekly_reuses_daily_object':True,'database_recent':12,
        'rotate_only_after_new_external_readback':True,'protect_last_restored':True,
        'drive_deletion_authorized':False,'drive_permanent_deletion_authorized':False,'database_timer_enabled':False}
    backup.atomic_json(directory/'retention-policy.json',policy)
    dbconfig=Path('/root/.config/rev-recovery/db-backup.env')
    if not dbconfig.exists():
        dbconfig.touch(mode=0o600)
        values={k:cfg[k] for k in ['REV_BACKUP_OFFSITE_METHOD','REV_BACKUP_DRIVE_FOLDER_ID','REV_BACKUP_DRIVE_AUTH_FILE']}
        values.update(REV_BACKUP_DIR='/root/rev-recovery/backups/database',
            REV_BACKUP_PASSPHRASE_FILE=str(backup.key_path(cfg)),REV_BACKUP_KEEP_RECENT='12',REV_BACKUP_KEEP_DAYS='0',REV_BACKUP_KEEP_WEEKS='0')
        for name,value in values.items():set_key(str(dbconfig),name,value)
        dbconfig.chmod(0o600)
    print(json.dumps({'latest_success':anchor['snapshot_utc'],'latest_attempt_preserved':True,'five_anchors_protected':True,'drive_deletions':0}))


if __name__=='__main__':main()
