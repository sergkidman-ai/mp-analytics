# поток: rev
import datetime,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import rev_backup as backup


class BackupPolicyTests(unittest.TestCase):
    def row(self,root,name):
        file=root/name;file.write_bytes(b'synthetic encrypted fixture')
        return {'bundle':str(file),'bundle_sha256':backup.sha256(file),'source_snapshot_utc':name[11:27],
            'offsite':{'method':'gdrive','verified':True,'readback_verified':True}}

    def test_failure_never_replaces_last_success_or_compatibility_marker(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);row=self.row(root,'rev-backup-20261108T110149Z.tar.gz.gpg');backup.record_attempt(root,row)
            saved=(root/'latest_success.json').read_bytes();old=(root/'latest.json').read_bytes()
            backup.record_attempt(root,{'error':'HTTP 403 storageQuotaExceeded','offsite':{'verified':False}})
            self.assertEqual((root/'latest_success.json').read_bytes(),saved);self.assertEqual((root/'latest.json').read_bytes(),old)
            self.assertIn('storageQuotaExceeded',json.loads((root/'latest_attempt.json').read_text())['error'])

    def test_metadata_only_upload_is_not_success(self):
        self.assertFalse(backup.successful({'offsite':{'method':'gdrive','verified':True}}))

    def test_daily_weekly_share_objects_and_retain_four_weeks(self):
        today=datetime.date(2026,11,8);records=[]
        for n in range(40):
            stamp=(today-datetime.timedelta(days=n)).strftime('%Y%m%d')+'T013000Z'
            records.append({'bundle':stamp,'source_snapshot_utc':stamp})
        keep=backup.retention_selection(records)
        self.assertLessEqual(len(keep),11)
        self.assertTrue(all(r['bundle'] in keep for r in records[:7]))
        self.assertEqual(len({datetime.datetime.strptime(s[:8],'%Y%m%d').date().isocalendar()[:2] for s in keep}),4)

    def test_local_rotation_preserves_last_restored_and_previous_success(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);rows=[]
            for day in ['01','02','03','04']:
                row=self.row(root,'rev-backup-202601'+day+'T013000Z.tar.gz.gpg');rows.append(row)
                backup.atomic_json(Path(row['bundle']+'.json'),row)
            backup.atomic_json(root/'last_restored_success.json',rows[0]);backup.atomic_json(root/'previous_success.json',rows[1]);backup.atomic_json(root/'latest_success.json',rows[-1])
            result=backup.retention(root,1,0,0,new_success=rows[-1])
            self.assertTrue(Path(rows[0]['bundle']).exists());self.assertTrue(Path(rows[1]['bundle']).exists());self.assertTrue(Path(rows[-1]['bundle']).exists())
            self.assertFalse(Path(rows[2]['bundle']).exists());self.assertEqual(result['deleted'],1)

    def test_http_reason_is_preserved_without_sensitive_server_message(self):
        response=Mock(ok=False,status_code=403);response.json.return_value={'error':{'message':'synthetic-secret-in-message','errors':[{'reason':'storageQuotaExceeded'}]}}
        with self.assertRaises(backup.BackupError) as error:backup.check_http(response,'Drive upload')
        self.assertIn('HTTP 403',str(error.exception));self.assertIn('storageQuotaExceeded',str(error.exception));self.assertNotIn('synthetic-secret',str(error.exception))

    def test_no_rotation_before_new_external_success(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(backup.retention(Path(d),new_success={'offsite':{'verified':False}})['deleted'],0)

    def test_uploaded_metadata_is_insufficient_when_readback_differs(self):
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'fixture.gpg';file.write_bytes(b'fixture')
            metadata=Mock(ok=True);metadata.json.return_value={'mimeType':'application/vnd.google-apps.folder','capabilities':{'canAddChildren':True}}
            permissions=Mock(ok=True);permissions.json.return_value={'permissions':[{'type':'user','role':'owner'}]}
            readback=Mock(ok=True);readback.iter_content.return_value=[b'different']
            start=Mock(ok=True,headers={'Location':'https://www.googleapis.com/synthetic-upload'})
            uploaded=Mock(ok=True);uploaded.json.return_value={'id':'synthetic-id','size':file.stat().st_size,'md5Checksum':backup.md5(file)}
            with patch.object(backup,'drive_token',return_value={}),patch.object(backup.requests,'get',side_effect=[metadata,permissions,readback]),patch.object(backup.requests,'post',return_value=start),patch.object(backup.requests,'put',return_value=uploaded),self.assertRaises(backup.BackupError):
                backup.upload_gdrive(file,{'REV_BACKUP_DRIVE_FOLDER_ID':'synthetic-folder'})

    def test_database_only_cannot_use_full_backup_journal(self):
        with patch.object(sys,'argv',['rev_backup.py','--database-only','--local-only']),patch.object(backup,'config',return_value={}),patch.object(backup,'backup') as send,patch.object(sys,'stderr',Mock()),self.assertRaises(SystemExit):backup.main()
        send.assert_not_called()

    def test_remote_plan_excludes_unrelated_files_and_never_authorizes_deletion(self):
        def item(day,identifier,project='mp-analytics-rev'):
            return {'id':identifier,'name':'rev-backup-202611'+day+'T013000Z.tar.gz.gpg','parents':['own-folder'],'appProperties':{'project':project}}
        rows=[item('07','anchor'),item('08','latest'),item('06','foreign','other-project')]
        rows.append({'id':'document','name':'notes.pdf','parents':['own-folder'],'appProperties':{'project':'mp-analytics-rev'}})
        plan=backup.drive_retention_plan(rows,{'anchor'},'own-folder')
        self.assertEqual(set(plan['keep_file_ids']),{'anchor','latest'});self.assertEqual(plan['candidates'],[])
        self.assertFalse(plan['trash_authorized']);self.assertFalse(plan['permanent_deletion_authorized'])
        with self.assertRaises(backup.BackupError):backup.drive_retention_plan(rows,set(),'own-folder')


if __name__=='__main__':unittest.main()
