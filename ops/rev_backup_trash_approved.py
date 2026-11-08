#!/usr/bin/env python3
# поток: rev
"""Move ONLY the owner's exact SHA-pinned 59-file manifest to Drive trash.

No permanent deletion, account trash cleanup, timer/service or OAuth changes.
All 64 approved/protected file states are checked before the first mutation.
Every PATCH has a durable intent and a separately verified result. An ambiguous
response stops processing; resumption only observes/reconciles that exact file.
"""
import argparse,csv,datetime,fcntl,hashlib,json,os,re,sys
from pathlib import Path
import requests
import rev_backup as backup
from rev_backup_drill import STATE,ROOT,NAMES

MANIFEST=STATE/'deletion-candidates-59.csv'
APPROVED_SHA='1e730d407fd0d69e0bd7ce7a15655039baa27d60d9ae20cc6db87e87771f7be7'
EXPECTED_COUNT=59
EXPECTED_BYTES=10309302260
JOURNAL=STATE/'trash-approved-59-journal.json'
FIELDS='id,name,size,createdTime,modifiedTime,sha256Checksum,md5Checksum,parents,trashed,version,owners(emailAddress),appProperties'


def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read_manifest(path=MANIFEST,expected_sha=APPROVED_SHA):
    path=Path(path)
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected_sha:raise backup.BackupError('Approved manifest SHA-256 mismatch; stopped')
    with path.open(newline='') as stream:rows=list(csv.DictReader(stream))
    if len(rows)!=EXPECTED_COUNT or len({x['file_id'] for x in rows})!=EXPECTED_COUNT or sum(int(x['bytes']) for x in rows)!=EXPECTED_BYTES:
        raise backup.BackupError('Approved manifest count/IDs/total mismatch; stopped')
    for row in rows:
        if not re.fullmatch(r'[A-Za-z0-9_-]+',row['file_id']) or not re.fullmatch(r'[0-9a-f]{64}',row['sha256']):raise backup.BackupError('Invalid approved metadata')
    return rows


def validate(item,expected,folder,trashed):
    if (item.get('id')!=expected['file_id'] or item.get('name')!=expected['name']
            or int(item.get('size',-1))!=int(expected['bytes']) or item.get('sha256Checksum')!=expected['sha256']
            or item.get('createdTime')!=expected['created_at'] or item.get('trashed') is not trashed
            or item.get('parents')!=[folder] or item.get('appProperties',{}).get('project')!='mp-analytics-rev'
            or not any(x.get('emailAddress','').lower()=='beyindi.befree@gmail.com' for x in item.get('owners',[]))):
        raise backup.BackupError('Drive file state differs from approved manifest: '+expected['file_id'])


class ScopedDrive:
    def __init__(self,cfg,candidates,protected):
        self.allowed=set(candidates);self.protected=set(protected)
        if self.allowed & self.protected:raise backup.BackupError('Protected file appears in approved trash set')
        self.session=requests.Session();self.session.trust_env=False;self.session.headers.update(backup.drive_token(cfg))

    def get(self,file_id):
        if file_id not in self.allowed|self.protected:raise backup.BackupError('Read outside the exact operation scope refused')
        r=self.session.get('https://www.googleapis.com/drive/v3/files/'+file_id,params={'fields':FIELDS},timeout=(10,30))
        backup.check_http(r,'Drive exact-file metadata')
        return r.json(),r.headers.get('ETag')

    def trash(self,file_id,etag=None):
        if file_id not in self.allowed or file_id in self.protected:raise backup.BackupError('Mutation outside approved59 IDs refused')
        headers={'If-Match':etag} if etag else {}
        r=self.session.patch('https://www.googleapis.com/drive/v3/files/'+file_id,
            params={'fields':FIELDS},json={'trashed':True},headers=headers,timeout=(10,30))
        backup.check_http(r,'Drive move approved file to trash')


def protected_rows(anchors,inventory):
    if anchors.get('status')!='PASS' or len(anchors.get('anchors',[]))!=5 or {x['name'] for x in anchors['anchors']}!=NAMES:
        raise backup.BackupError('Five successful anchor restore proofs required')
    by_id={x['id']:x for x in inventory};out=[]
    for proof in anchors['anchors']:
        if proof['status']!='PASS' or not proof['downloaded_from_drive'] or proof['restored_tables']!=38 or proof['critical_digests']!=18:
            raise backup.BackupError('Protected restore proof differs')
        local=STATE/'downloads'/proof['name']
        if local.is_symlink() or not local.is_file() or local.stat().st_size!=proof['bytes'] or backup.sha256(local)!=proof['sha256']:
            raise backup.BackupError('Verified local anchor differs; stopped')
        original=by_id[proof['file_id']]
        out.append({'file_id':proof['file_id'],'name':proof['name'],'bytes':proof['bytes'],
            'created_at':original['createdTime'],'sha256':proof['sha256']})
    if len({x['file_id'] for x in out})!=5:raise backup.BackupError('Protected IDs are not unique')
    return out


def run(execute=False):
    if Path.cwd()!=ROOT or os.geteuid()!=0:raise backup.BackupError('Run only from the Rev checkout as the local owner')
    os.umask(0o077)
    with (STATE/'trash-approved-59.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        rows=read_manifest();anchors=json.loads((STATE/'anchors-result.json').read_text())
        inventory=json.loads((STATE/'drive-inventory.json').read_text())
        protected=protected_rows(anchors,inventory)
        ids={x['file_id'] for x in rows};protected_ids={x['file_id'] for x in protected}
        cfg=backup.config();folder=cfg['REV_BACKUP_DRIVE_FOLDER_ID']
        originals={x['id']:x for x in inventory}
        for row in rows+protected:
            original=originals.get(row['file_id'],{})
            # The frozen inventory records content/parent identity, not owners or
            # trash state. Those are fetched independently from Drive below.
            if (original.get('parents')!=[folder] or original.get('name')!=row['name']
                    or int(original.get('size',-1))!=int(row['bytes'])
                    or original.get('sha256Checksum')!=row['sha256'] or original.get('createdTime')!=row['created_at']):
                raise backup.BackupError('Original folder/content identity differs; stopped')
        drive=ScopedDrive(cfg,ids,protected_ids)
        journal=json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {
            'manifest_sha256':APPROVED_SHA,'authorized_action':'trash_only','permanent_deletion_authorized':False,
            'started_at':now(),'before':{},'protected_before':{},'results':{},'intents':{}}
        if journal.get('manifest_sha256')!=APPROVED_SHA or journal.get('authorized_action')!='trash_only':raise backup.BackupError('Existing operation journal differs')
        try:
            current={}
            # Complete read-only preflight, including all candidates, before ANY PATCH.
            for row in rows:
                file_id=row['file_id'];item,etag=drive.get(file_id)
                known_done=journal['results'].get(file_id,{}).get('trashed') is True
                if item.get('trashed') and not known_done:
                    if file_id not in journal['intents']:raise backup.BackupError('Unexpected pre-existing trash state; stopped')
                    validate(item,row,folder,True);journal['results'][file_id]=item;known_done=True
                validate(item,row,folder,known_done)
                current[file_id]=(item,etag)
                if file_id not in journal['before']:journal['before'][file_id]=item
            for row in protected:
                item,_=drive.get(row['file_id']);validate(item,row,folder,False)
                if row['file_id'] in journal['protected_before'] and item!=journal['protected_before'][row['file_id']]:raise backup.BackupError('Protected metadata changed; stopped')
                journal['protected_before'][row['file_id']]=item
            print(json.dumps({'preflight':'PASS','candidates':59,'bytes':EXPECTED_BYTES,'anchors':5,'execute':execute}),flush=True)
            if not execute:return
            journal['status']='RUNNING';backup.atomic_json(JOURNAL,journal)
            for row in rows:
                file_id=row['file_id']
                if journal['results'].get(file_id,{}).get('trashed') is True:continue
                # Re-read immediately before each mutation; abort on concurrent edits.
                item,etag=drive.get(file_id);validate(item,row,folder,False)
                if item!=journal['before'][file_id]:raise backup.BackupError('Candidate changed after preflight; stopped')
                read_manifest()  # Owner-pinned input must remain unchanged throughout.
                journal['intents'][file_id]={'at':now(),'action':'trashed=true'};backup.atomic_json(JOURNAL,journal)
                drive.trash(file_id,etag)
                result,_=drive.get(file_id);validate(result,row,folder,True)
                journal['results'][file_id]=result;backup.atomic_json(JOURNAL,journal)
                if len(journal['results'])%10==0:
                    for anchor in protected:
                        live,_=drive.get(anchor['file_id']);validate(live,anchor,folder,False)
                        if live!=journal['protected_before'][anchor['file_id']]:raise backup.BackupError('Protected metadata changed during operation')
                    print(json.dumps({'confirmed_in_trash':len(journal['results'])}),flush=True)
            # Final independent GET of every selected/protected ID, never a broad trash listing.
            final=[]
            for row in rows:
                item,_=drive.get(row['file_id']);validate(item,row,folder,True)
                final.append({**row,'trashed':'true','trash_verified_at':now()})
            for anchor in protected:
                item,_=drive.get(anchor['file_id']);validate(item,anchor,folder,False)
                if item!=journal['protected_before'][anchor['file_id']]:raise backup.BackupError('Protected metadata changed at final check')
            read_manifest()
            manifest=STATE/'permanent-deletion-candidates-59.csv'
            if manifest.exists():
                # Preserve an already-issued separate permanent-deletion review list.
                with manifest.open(newline='') as f:old=list(csv.DictReader(f))
                fields=['file_id','name','bytes','created_at','sha256']
                if {x['file_id']:tuple(x[k] for k in fields) for x in old}!={x['file_id']:tuple(x[k] for k in fields) for x in rows}:
                    raise backup.BackupError('Existing permanent review list differs')
            else:
                with manifest.open('x',newline='') as f:
                    writer=csv.DictWriter(f,fieldnames=list(final[0]));writer.writeheader();writer.writerows(final)
            report={'status':'PASS','completed_at':now(),'moved_count':59,'bytes':EXPECTED_BYTES,
                'five_anchors_unchanged':True,'permanent_deletions':0,'account_trash_emptied':False,
                'approved_manifest_sha256':APPROVED_SHA,'permanent_manifest':str(manifest),
                'permanent_manifest_sha256':backup.sha256(manifest),'permanent_deletion_authorized':False,
                'timers_and_services_changed':False}
            journal.update(status='PASS',completed_at=report['completed_at']);backup.atomic_json(JOURNAL,journal)
            backup.atomic_json(STATE/'trash-approved-59-result.json',report)
            backup.atomic_json(STATE/'permanent-deletion-candidates-59.json',{'authorized':False,'files':final,'protected':protected})
            print(json.dumps(report),flush=True)
        except Exception as error:
            if execute:
                journal.update(status='STOPPED',error=backup.safe_error(error),stopped_at=now());backup.atomic_json(JOURNAL,journal)
            raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--execute',action='store_true');args=p.parse_args()
    try:run(args.execute)
    except Exception as error:
        print(json.dumps({'status':'STOPPED','error':backup.safe_error(error),'permanent_deletions':0}),flush=True)
        raise SystemExit(1)
