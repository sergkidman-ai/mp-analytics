# поток: infra
"""Deletion boundaries and cross-project scope checks for owner-approved retention."""
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from ops.pult_backup import retention_selection,Drive


class ThreeDayRetention(unittest.TestCase):
    def test_72_hour_boundary_and_same_day_duplicates(self):
        reference=dt.datetime(2026,11,16,13,tzinfo=dt.timezone.utc)
        rows=[{'file_id':'expired','snapshot_utc':'2026-11-13T12:59:59+00:00'},
              {'file_id':'boundary','snapshot_utc':'2026-11-13T13:00:00+00:00'},
              {'file_id':'recent','snapshot_utc':'2026-11-16T03:10:00+00:00'},
              {'file_id':'newest','snapshot_utc':'2026-11-16T04:00:00+00:00'}]
        self.assertEqual(retention_selection(rows,max_age_hours=72,reference=reference),{'boundary','newest'})

    def test_pult_permanent_delete_cannot_touch_another_project(self):
        drive=Drive({'folder_id':'owned'});drive.call=Mock()
        with self.assertRaises(AssertionError):
            drive.delete({'id':'other','parents':['owned'],'appProperties':{'project':'foreign'}})
        drive.call.assert_not_called()

    def test_pult_changed_remote_size_stops_before_deletion(self):
        from ops.pult_backup import PROJECT
        drive=Drive({'folder_id':'owned'});drive.call=Mock()
        drive.call.return_value.json.return_value={'name':'old','size':'20','parents':['owned']}
        with self.assertRaises(AssertionError):
            drive.delete({'id':'old','name':'old','size':'10','parents':['owned'],'appProperties':{'project':PROJECT}})
        self.assertEqual([x.args[0]for x in drive.call.call_args_list],['GET'])

    def test_rev_expired_protected_anchors_do_not_override_owner_policy(self):
        spec=importlib.util.spec_from_file_location('rev_retention',Path(__file__).resolve().parents[1]/'ops/rev_backup.py')
        rev=importlib.util.module_from_spec(spec);spec.loader.exec_module(rev)
        reference=dt.datetime.now(dt.timezone.utc)
        recent=reference.strftime('%Y%m%dT%H%M%SZ');old=(reference-dt.timedelta(days=4)).strftime('%Y%m%dT%H%M%SZ')
        def item(ident,stamp,folder='owned',project='mp-analytics-rev'):
            return {'id':ident,'name':'rev-backup-'+stamp+'.tar.gz.gpg','parents':[folder],'appProperties':{'project':project}}
        plan=rev.drive_retention_plan([item('new',recent),item('old',old),item('foreign',old,project='other')],{'new','old'},'owned')
        self.assertEqual(plan['keep_file_ids'],['new'])
        self.assertEqual([x['id']for x in plan['candidates']],['old'])

    def test_rev_no_verified_external_success_cannot_delete(self):
        spec=importlib.util.spec_from_file_location('rev_failure',Path(__file__).resolve().parents[1]/'ops/rev_backup.py')
        rev=importlib.util.module_from_spec(spec);spec.loader.exec_module(rev)
        with patch.object(rev,'drive_inventory')as inventory:
            with self.assertRaises(rev.BackupError):rev.apply_drive_retention({'REV_BACKUP_RETENTION_HOURS':'72'},{'offsite':{'verified':False}})
            inventory.assert_not_called()

if __name__=='__main__':unittest.main()
