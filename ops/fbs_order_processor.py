# поток: inv
"""Dispatch confirmed orders and catch up existing shipments in independent stages."""
import subprocess
import sys


def run(runner=subprocess.run):
    results = [runner([sys.executable, '-m', module, '--apply'], check=False)
               for module in ('ops.ozon_manual_packages', 'ops.fbs_confirmed_daily_dispatch', 'ops.fbs_ready_market_catchup')]
    return int(any(result.returncode for result in results))


if __name__ == '__main__':
    raise SystemExit(run())
