# поток: rev
import csv,hashlib,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import rev_backup_trash_approved as t


class ApprovedTrashTests(unittest.TestCase):
    def test_manifest_change_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'manifest.csv';p.write_text('altered')
            with self.assertRaises(t.backup.BackupError):t.read_manifest(p,t.APPROVED_SHA)

    def test_duplicate_ids_are_rejected_even_if_count_and_size_match(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'manifest.csv'
            with p.open('w') as f:
                w=csv.DictWriter(f,fieldnames=['file_id','name','bytes','created_at','reason','sha256']);w.writeheader()
                for n in range(59):w.writerow({'file_id':'same-id','name':'fixture','bytes':t.EXPECTED_BYTES if n==0 else 0,'created_at':'now','reason':'test','sha256':'0'*64})
            with self.assertRaises(t.backup.BackupError):t.read_manifest(p,hashlib.sha256(p.read_bytes()).hexdigest())

    def test_protected_and_unlisted_ids_cannot_be_mutated(self):
        with patch.object(t.backup,'drive_token',return_value={}):drive=t.ScopedDrive({}, {'candidate'},{'anchor'})
        drive.session=Mock()
        for file_id in ['anchor','unlisted']:
            with self.assertRaises(t.backup.BackupError):drive.trash(file_id)
        drive.session.patch.assert_not_called()

    def test_only_trashed_true_patch_is_sent(self):
        with patch.object(t.backup,'drive_token',return_value={}):drive=t.ScopedDrive({}, {'candidate'},{'anchor'})
        drive.session=Mock();drive.session.patch.return_value=Mock(ok=True)
        drive.trash('candidate','fixture-etag')
        call=drive.session.patch.call_args
        self.assertEqual(call.kwargs['json'],{'trashed':True});self.assertEqual(call.kwargs['headers'],{'If-Match':'fixture-etag'})
        drive.session.delete.assert_not_called()

    def test_changed_state_or_content_is_rejected(self):
        row={'file_id':'candidate','name':'fixture','bytes':'7','created_at':'date','sha256':'a'*64}
        item={'id':'candidate','name':'fixture','size':'7','createdTime':'date','sha256Checksum':'a'*64,'trashed':False,
            'parents':['own-folder'],'appProperties':{'project':'mp-analytics-rev'},'owners':[{'emailAddress':'beyindi.befree@gmail.com'}]}
        t.validate(item,row,'own-folder',False)
        for key,value in [('trashed',True),('sha256Checksum','b'*64),('parents',['other-folder'])]:
            with self.assertRaises(t.backup.BackupError):t.validate({**item,key:value},row,'own-folder',False)


if __name__=='__main__':unittest.main()
