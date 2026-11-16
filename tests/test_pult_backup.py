# поток: infra
import unittest
from unittest.mock import Mock,patch
from ops.pult_backup import retention_selection,Drive,PROJECT


class BackupRetentionTest(unittest.TestCase):
    def test_latest_copy_per_successful_day_and_two_day_limit(self):
        records=[{'file_id':'morning','snapshot_utc':'2026-11-16T01:00:00+00:00'},
                 {'file_id':'evening','snapshot_utc':'2026-11-16T20:00:00+00:00'},
                 {'file_id':'previous','snapshot_utc':'2026-11-14T01:00:00+00:00'},
                 {'file_id':'older','snapshot_utc':'2026-11-13T01:00:00+00:00'}]
        self.assertEqual(retention_selection(records,daily=2),{'evening','previous'})

    def test_weeks_and_months_preserve_older_restore_points(self):
        records=[{'file_id':day,'snapshot_utc':day+'T01:00:00+00:00'}
                 for day in ['2026-11-16','2026-11-15','2026-11-08','2026-10-31','2026-09-30']]
        self.assertEqual(retention_selection(records,daily=1,weekly=2,monthly=3),
                         {'2026-11-16','2026-11-15','2026-10-31','2026-09-30'})

    def test_no_success_receipts_means_no_expired_record(self):
        self.assertEqual(retention_selection([]),set())

    def test_rotation_cannot_touch_other_projects_or_folders(self):
        drive=Drive({'folder_id':'owned'});drive.call=Mock()
        with self.assertRaises(AssertionError):
            drive.delete({'id':'foreign','parents':['owned'],'appProperties':{'project':'another-app'}})
        with self.assertRaises(AssertionError):
            drive.delete({'id':'foreign','parents':['other-folder'],'appProperties':{'project':PROJECT}})
        drive.call.assert_not_called()

    def test_permanent_deletion_rechecks_metadata_and_confirms_absence(self):
        drive=Drive({'folder_id':'owned'});drive.call=Mock();drive.headers=Mock(return_value={})
        item={'id':'file','name':'old','size':'10','parents':['owned'],'appProperties':{'project':PROJECT}}
        drive.call.return_value.json.return_value=item
        with patch('ops.pult_backup.requests.get',return_value=Mock(status_code=404)):
            drive.delete(item)
        self.assertEqual([x.args[0]for x in drive.call.call_args_list],['GET','DELETE'])

if __name__=='__main__':unittest.main()
