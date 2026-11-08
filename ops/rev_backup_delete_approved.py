#!/usr/bin/env python3
# поток: rev
"""Permanently delete only the exact 59 IDs explicitly approved by the owner.

No account-wide trash endpoint, folder deletion, retries of ambiguous DELETE,
OAuth/config/service changes or backup startup. All targets and anchors are
validated before the first mutation. Each intent/result is persisted separately.
"""
import argparse,csv,fcntl,json,os,time
from pathlib import Path
import rev_backup as backup
import rev_backup_trash_approved as trash
from rev_backup_drill import ROOT,STATE

MANIFEST=STATE/'permanent-deletion-candidates-59.csv'
APPROVED_SHA='18a9cf3a3939a556ba8e028fb246134c725dfec49f9bbb95ce6de697c7f499cc'
JOURNAL=STATE/'permanent-approved-59-journal.json'
FIELDS=trash.FIELDS+',mimeType'


def read_manifest():
    rows=trash.read_manifest(MANIFEST,APPROVED_SHA)
    if not all(x.get('trashed')=='true' for x in rows):raise backup.BackupError('Manifest trash state differs; stopped')
    return rows


def validate(item,row,folder,trashed):
    trash.validate(item,row,folder,trashed)
    if not item.get('mimeType') or item['mimeType']=='application/vnd.google-apps.folder':
        raise backup.BackupError('Folder/non-file target refused; stopped')


class ExactDeleteDrive(trash.ScopedDrive):
    def get(self,file_id):
        if file_id not in self.allowed|self.protected:raise backup.BackupError('Metadata outside approved/protected IDs refused')
        r=self.session.get('https://www.googleapis.com/drive/v3/files/'+file_id,params={'fields':FIELDS},timeout=(10,30))
        backup.check_http(r,'Drive approved-file metadata')
        return r.json(),r.headers.get('ETag')

    def delete_approved(self,file_id,etag=None):
        if file_id not in self.allowed or file_id in self.protected:raise backup.BackupError('Permanent deletion outside approved59 refused')
        headers={'If-Match':etag} if etag else {}
        r=self.session.delete('https://www.googleapis.com/drive/v3/files/'+file_id,headers=headers,timeout=(10,30))
        backup.check_http(r,'Drive permanently delete exact approved file')
        if r.status_code not in (200,204):raise backup.BackupError('Unexpected delete response; stopped')
        # A successful request is followed by an independent exact-ID GET.
        verify=self.session.get('https://www.googleapis.com/drive/v3/files/'+file_id,params={'fields':'id,trashed'},timeout=(10,30))
        if verify.status_code!=404:raise backup.BackupError('Deleted file is not confirmed absent; stopped')
        return r.status_code

    def verify_absent(self,file_id):
        if file_id not in self.allowed or file_id in self.protected:raise backup.BackupError('Absence check outside approved59 refused')
        r=self.session.get('https://www.googleapis.com/drive/v3/files/'+file_id,params={'fields':'id'},timeout=(10,30))
        if r.status_code!=404:raise backup.BackupError('Approved deleted file is still present or status is uncertain')

    def quota(self):
        r=self.session.get('https://www.googleapis.com/drive/v3/about',params={'fields':'user(emailAddress),storageQuota'},timeout=(10,30))
        backup.check_http(r,'Drive quota verification');data=r.json()
        if data.get('user',{}).get('emailAddress','').lower()!='beyindi.befree@gmail.com':raise backup.BackupError('Unexpected quota account')
        q=data['storageQuota'];return {'total_bytes':int(q['limit']),'used_bytes':int(q['usage']),'free_bytes':int(q['limit'])-int(q['usage'])}


def matches_previous(current,previous):return all(current.get(k)==v for k,v in previous.items())


def run(execute=False):
    os.umask(0o077)
    if Path.cwd()!=ROOT or os.geteuid()!=0:raise backup.BackupError('Use the Rev checkout as its local owner')
    with (STATE/'permanent-approved-59.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if JOURNAL.exists():raise backup.BackupError('Permanent operation journal already exists; inspect before any further action')
        rows=read_manifest();anchors=json.loads((STATE/'anchors-result.json').read_text())
        protected=trash.protected_rows(anchors,json.loads((STATE/'drive-inventory.json').read_text()))
        candidates={x['file_id'] for x in rows};protected_ids={x['file_id'] for x in protected}
        cfg=backup.config();folder=cfg['REV_BACKUP_DRIVE_FOLDER_ID'];drive=ExactDeleteDrive(cfg,candidates,protected_ids)
        old_trash=json.loads((STATE/'trash-approved-59-journal.json').read_text())
        old_protected=json.loads((STATE/'protected-five-after-trash.json').read_text())
        journal={'status':'PREFLIGHT','authorized_action':'permanent_delete_exact59','manifest_sha256':APPROVED_SHA,
            'started_at':trash.now(),'before':{},'protected_before':{},'intents':{},'deleted':{}}
        try:
            for row in rows:
                item,_=drive.get(row['file_id']);validate(item,row,folder,True)
                if not matches_previous(item,old_trash['results'][row['file_id']]):raise backup.BackupError('Candidate changed since approved trash manifest; stopped')
                journal['before'][row['file_id']]=item
            for row in protected:
                item,_=drive.get(row['file_id']);validate(item,row,folder,False)
                if not matches_previous(item,old_protected[row['file_id']]):raise backup.BackupError('Protected archive changed; stopped')
                journal['protected_before'][row['file_id']]=item
            journal['quota_before']=drive.quota()
            print(json.dumps({'preflight':'PASS','trashed_unique_ids':59,'bytes':trash.EXPECTED_BYTES,'protected':5,'quota_before':journal['quota_before'],'execute':execute}),flush=True)
            if not execute:return
            journal['status']='RUNNING';backup.atomic_json(JOURNAL,journal)
            for row in rows:
                file_id=row['file_id'];item,etag=drive.get(file_id);validate(item,row,folder,True)
                if item!=journal['before'][file_id]:raise backup.BackupError('Candidate changed after preflight; stopped')
                read_manifest()
                journal['intents'][file_id]={'at':trash.now(),'action':'permanent_delete'};backup.atomic_json(JOURNAL,journal)
                status=drive.delete_approved(file_id,etag)
                journal['deleted'][file_id]={'confirmed_at':trash.now(),'delete_http':status,'get_http':404,'bytes':int(row['bytes'])};backup.atomic_json(JOURNAL,journal)
                if len(journal['deleted'])%10==0:
                    for anchor in protected:
                        live,_=drive.get(anchor['file_id']);validate(live,anchor,folder,False)
                        if live!=journal['protected_before'][anchor['file_id']]:raise backup.BackupError('Protected archive changed during deletion; stopped')
                    print(json.dumps({'confirmed_deleted':len(journal['deleted'])}),flush=True)
            for row in rows:drive.verify_absent(row['file_id'])
            protected_after={}
            for anchor in protected:
                item,_=drive.get(anchor['file_id']);validate(item,anchor,folder,False)
                if item!=journal['protected_before'][anchor['file_id']]:raise backup.BackupError('Protected archive changed at final check')
                protected_after[anchor['file_id']]=item
            read_manifest();quota_after=drive.quota()
            for _ in range(6):
                if quota_after['free_bytes']>journal['quota_before']['free_bytes']:break
                time.sleep(5);quota_after=drive.quota()
            if quota_after['free_bytes']<=journal['quota_before']['free_bytes']:raise backup.BackupError('59 files deleted but quota update is not yet confirmed; stopped')
            result={'status':'PASS','completed_at':trash.now(),'deleted_count':59,'deleted_bytes':trash.EXPECTED_BYTES,
                'five_anchors_unchanged':True,'account_trash_emptied':False,'approved_manifest_sha256':APPROVED_SHA,
                'quota_before':journal['quota_before'],'quota_after':quota_after,
                'free_bytes_increase':quota_after['free_bytes']-journal['quota_before']['free_bytes'],
                'oauth_timers_services_changed':False}
            journal.update(status='PASS',completed_at=result['completed_at'],quota_after=quota_after);backup.atomic_json(JOURNAL,journal)
            backup.atomic_json(STATE/'protected-five-after-permanent.json',protected_after)
            backup.atomic_json(STATE/'permanent-approved-59-result.json',result)
            print(json.dumps(result),flush=True)
        except Exception as error:
            if execute:
                journal.update(status='STOPPED',error=backup.safe_error(error),stopped_at=trash.now());backup.atomic_json(JOURNAL,journal)
            raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--execute',action='store_true');args=p.parse_args()
    try:run(args.execute)
    except Exception as error:
        print(json.dumps({'status':'STOPPED','error':backup.safe_error(error),'no_account_wide_trash_action':True}),flush=True)
        raise SystemExit(1)
