# поток: rev
"""Collection must not hold the processing lock needed by the five-minute tick."""
import ast
from contextlib import contextmanager
import fcntl
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from feedback_bot.recovery_runtime import processing_lock
ROOT=Path(__file__).resolve().parents[1]

class CycleConcurrency(unittest.TestCase):
    def test_collection_and_short_service_have_different_outer_locks(self):
        full=(ROOT/'feedback_bot/feedback-recovery-cycle.service').read_text()
        short=(ROOT/'feedback_bot/feedback-recovery-review.service').read_text()
        self.assertIn('/root/rev-recovery/collection.lock',full)
        self.assertNotIn('/root/rev-recovery/cycle.lock',full)
        self.assertIn('/root/rev-recovery/cycle.lock',short)

    def test_actual_full_cycle_collects_outside_processing_lock(self):
        held=False;events=[]
        @contextmanager
        def lock():
            nonlocal held
            self.assertFalse(held);held=True
            try:yield 0
            finally:held=False
        def collect(*a):
            self.assertFalse(held);events.append('collect')
        def process(*a):
            self.assertTrue(held);events.append('process')
        tree=ast.parse((ROOT/'feedback_bot/recovery_cycle.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        ns={'os':SimpleNamespace(umask=lambda x:None),'runtime':SimpleNamespace(setup=lambda:None,processing_lock=lock),
            'recovery_budget':SimpleNamespace(setup=lambda:None),'db':SimpleNamespace(execute=lambda q:None),
            'sys':SimpleNamespace(argv=[]),'refresh_wb_active':lambda:[],
            'refresh_catalog_if_stale':collect,'process_collected':process,
            'collect':SimpleNamespace(stage=lambda name,fn:fn(),collect_wb=collect,collect_ozon=collect,collect_yandex=collect)}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'cycle','exec'),ns)
        ns['main']()
        self.assertGreater(events.count('collect'),0)
        self.assertEqual(events[-1],'process')
        self.assertFalse(held)

    def test_processing_lock_still_serializes_all_processors(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'processing.lock')
            with processing_lock(path):
                with open(path,'a') as other:
                    with self.assertRaises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with open(path,'a') as other:
                fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)

if __name__=='__main__':unittest.main()
