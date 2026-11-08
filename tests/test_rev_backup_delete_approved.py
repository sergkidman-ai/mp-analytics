# поток: rev
import sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import rev_backup_delete_approved as d


class PermanentDeleteTests(unittest.TestCase):
    def drive(self):
        with patch.object(d.backup,'drive_token',return_value={}):drive=d.ExactDeleteDrive({}, {'approved'},{'protected'})
        drive.session=Mock();return drive

    def test_protected_or_unlisted_cannot_be_deleted(self):
        drive=self.drive()
        for file_id in ['protected','other','emptyTrash']:
            with self.assertRaises(d.backup.BackupError):drive.delete_approved(file_id)
        drive.session.delete.assert_not_called()

    def test_delete_is_exact_id_with_empty_body_and_requires_absence(self):
        drive=self.drive();drive.session.delete.return_value=Mock(ok=True,status_code=204);drive.session.get.return_value=Mock(status_code=404)
        self.assertEqual(drive.delete_approved('approved','etag'),204)
        args=drive.session.delete.call_args
        self.assertEqual(args.args[0],'https://www.googleapis.com/drive/v3/files/approved')
        self.assertNotIn('json',args.kwargs);self.assertNotIn('data',args.kwargs)
        self.assertEqual(args.kwargs['headers'],{'If-Match':'etag'})

    def test_successful_delete_response_alone_is_insufficient(self):
        drive=self.drive();drive.session.delete.return_value=Mock(ok=True,status_code=204);drive.session.get.return_value=Mock(status_code=200)
        with self.assertRaises(d.backup.BackupError):drive.delete_approved('approved')

    def test_network_ambiguity_is_not_retried(self):
        drive=self.drive();drive.session.delete.side_effect=d.requests.Timeout('synthetic timeout') if hasattr(d,'requests') else TimeoutError('synthetic timeout')
        with self.assertRaises(Exception):drive.delete_approved('approved')
        self.assertEqual(drive.session.delete.call_count,1)

    def test_folders_are_never_eligible(self):
        with patch.object(d.trash,'validate'):
            with self.assertRaises(d.backup.BackupError):d.validate({'mimeType':'application/vnd.google-apps.folder'},{},'folder',True)


if __name__=='__main__':unittest.main()
