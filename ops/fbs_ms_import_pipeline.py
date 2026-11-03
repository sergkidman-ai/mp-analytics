# поток: inv
"""Run the MS status and shipment steps even when one import item fails.

The importer keeps a failed item for retry, but may already have created other
orders in the same pass. Those orders still need the independent status step.
"""
from __future__ import annotations

import subprocess
import sys


def run(runner=subprocess.run) -> int:
    importer = runner([sys.executable, '-m', 'ops.fbs_ms_import_scheduler', '--apply'],
                      check=False)
    status = runner([sys.executable, '-m', 'ops.fbs_ms_post_import_status', '--apply'],
                    check=False)
    shipments = runner([sys.executable, '-m', 'ops.fbs_ms_post_import_shipments', '--apply'],
                       check=False)
    return 1 if importer.returncode or status.returncode or shipments.returncode else 0


if __name__ == '__main__':
    sys.exit(run())
