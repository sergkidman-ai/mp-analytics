# поток: inv
import unittest
from unittest.mock import Mock

from ops.fbs_ms_import_pipeline import run


class ImportPipelineTests(unittest.TestCase):
    def test_status_runs_after_partial_import_failure_and_service_reports_failure(self):
        runner = Mock(side_effect=[Mock(returncode=1), Mock(returncode=0), Mock(returncode=0)])
        self.assertEqual(run(runner), 1)
        self.assertEqual(runner.call_count, 3)
        self.assertIn('ops.fbs_ms_post_import_status', runner.call_args_list[1].args[0])
        self.assertIn('ops.fbs_ms_post_import_shipments', runner.call_args_list[2].args[0])

    def test_status_failure_is_reported(self):
        runner = Mock(side_effect=[Mock(returncode=0), Mock(returncode=1), Mock(returncode=0)])
        self.assertEqual(run(runner), 1)

    def test_shipment_failure_is_reported(self):
        runner = Mock(side_effect=[Mock(returncode=0), Mock(returncode=0), Mock(returncode=1)])
        self.assertEqual(run(runner), 1)


if __name__ == '__main__':
    unittest.main()
