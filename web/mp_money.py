# поток: fin
"""Read-only composition of official marketplace balances and overdue WB payouts.

Overdue classification and amounts come from the existing cashflow view. We do not
change payout matching or treat its forecast as a bank-account balance.
"""
from decimal import Decimal


def compose_mp_money(balances, overdue, account_orgs, org=""):
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
    have = {r["account"] for r in balances}
    dates = [r["day"] for r in balances + overdue]
    return {"total": float(round(balance_total + wb_overdue, 2)),
            **{p: float(round(v, 2)) for p, v in by.items()},
            "balance_total": float(round(balance_total, 2)),
            "wb_account_balance": float(round(wb_balance, 2)) if wb_balance is not None else None,
            "wb_overdue": float(round(wb_overdue, 2)),
            "wb_overdue_count": len(overdue),
            "wb_overdue_source": "cashflow",
            "wb_overdue_as_of": max((r["day"] for r in overdue), default=None).isoformat() if overdue else None,
            "as_of": max(dates).isoformat(),
            "missing": [a for a, inn in account_orgs.items() if a not in have and (not org or inn == org)]}
