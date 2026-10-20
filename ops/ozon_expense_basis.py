# поток: fin
"""Refresh/status of common expense basis. Existing raw only, no marketplace/MS calls."""
import argparse
import json
from datetime import date
from core.ozon_expense_basis import refresh,basis_status


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['refresh','status'])
    p.add_argument('--accounts',nargs='+',choices=['oz_acc1','oz_acc2'],default=['oz_acc1','oz_acc2'])
    p.add_argument('--as-of',type=date.fromisoformat)
    p.add_argument('--log-path',default='/opt/mp-analytics/run_daily.log')
    p.add_argument('--apply',action='store_true',help='Publish immutable version and preserve/update old Ozon rules')
    a=p.parse_args()
    for account in a.accounts:
        value=refresh(account,as_of=a.as_of,log_path=a.log_path,apply=a.apply) if a.command=='refresh' else basis_status(account,as_of=a.as_of)
        print(json.dumps(value,ensure_ascii=False,default=str))


if __name__=='__main__':main()
