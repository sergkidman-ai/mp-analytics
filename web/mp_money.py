# поток: fin
"""Read-only composition of official marketplace balances and overdue WB payouts.

Overdue classification and amounts come from the existing cashflow view. We do not
change payout matching or treat its forecast as a bank-account balance.
"""
from decimal import Decimal
import datetime as dt
from zoneinfo import ZoneInfo


def compose_mp_money(balances, overdue, account_orgs, org="", today=None):
    today = today or dt.datetime.now(ZoneInfo("Europe/Moscow")).date()
    balances = [r for r in balances if not org or account_orgs.get(r["account"]) == org]
    # A payout reference is counted once, including if the view contains repeated rows.
    payouts = {}
    for r in overdue:
        account = r["ref"].split(":", 1)[0]
        if account not in account_orgs or not account.startswith("wb_"):
            continue
        if account_orgs[account] != r["org_inn"] or (org and r["org_inn"] != org):
            continue
        if Decimal(str(r["amount"])) <= 0:
            continue
        key = (r["org_inn"], r["ref"])
        if key not in payouts or r["day"] > payouts[key]["day"]:
            payouts[key] = r
    overdue = list(payouts.values())
    if not balances and not overdue:
        return None
    by = {}
    for r in balances:
        by[r["platform"]] = by.get(r["platform"], Decimal(0)) + Decimal(str(r["balance"]))
    wb_balance = by.get("wb")
    wb_overdue = sum((Decimal(str(r["amount"])) for r in overdue), Decimal(0))
    balance_total = sum(by.values(), Decimal(0))
    if wb_overdue:
        by["wb"] = (wb_balance or Decimal(0)) + wb_overdue
    details = []
    for r in overdue:
        # Same contractual expected date as cf.mp_forecast.wb_forecast:
        # reporting week Monday + 5 weeks. cf_line.day is rescheduled to today,
        # so it cannot be used as the original payout due date.
        account, week = r["ref"].split(":", 1)
        try:
            due = dt.date.fromisoformat(week) + dt.timedelta(weeks=5)
        except ValueError:
            due = None
        details.append({"account": account, "org_inn": r["org_inn"],
                        "ref": r["ref"], "amount": float(round(Decimal(str(r["amount"])), 2)),
                        "due_date": due.isoformat() if due else None,
                        "days_overdue": max(0, (today-due).days) if due else None})
    details.sort(key=lambda r: (r["due_date"] or "9999", r["account"]))
    have = {r["account"] for r in balances}
    dates = [r["day"] for r in balances + overdue]
    return {"total": float(round(balance_total + wb_overdue, 2)),
            **{p: float(round(v, 2)) for p, v in by.items()},
            "balance_total": float(round(balance_total, 2)),
            "wb_account_balance": float(round(wb_balance, 2)) if wb_balance is not None else None,
            "wb_overdue": float(round(wb_overdue, 2)),
            "wb_overdue_count": len(overdue),
            "wb_overdue_payments": details,
            "wb_overdue_max_days": max((r["days_overdue"] for r in details
                                        if r["days_overdue"] is not None), default=None),
            "wb_overdue_source": "cashflow",
            "wb_overdue_as_of": max((r["day"] for r in overdue), default=None).isoformat() if overdue else None,
            "as_of": max(dates).isoformat(),
            "missing": [a for a, inn in account_orgs.items() if a not in have and (not org or inn == org)]}
