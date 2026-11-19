"""Read the seller's visible Ozon review list into an account-scoped local history."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import UUID

ACCOUNTS = ("oz_acc1", "oz_acc2")
REVIEWS = "https://seller.ozon.ru/app/reviews"
HOME = Path.home() / ".rev-ozon"
ROW = 'tr:visible, [role="row"]:visible, [data-review-id]:visible'
NEXT = re.compile(r"^(Следующая(?: страница)?|Впер[её]д|Next(?: page)?)$", re.I)


class Unavailable(Exception):
    pass


def seller_page(url):
    u = urlsplit(url)
    return u.scheme == "https" and u.hostname == "seller.ozon.ru" and u.path.rstrip("/") == "/app/reviews"


def read_page(page):
    # Work only with fields displayed by the seller UI. Keep no HTML or session data.
    return page.locator(ROW).evaluate_all("""rows => rows.map(row => {
        const table = row.closest('table,[role=table],[role=grid]');
        const headers = table ? Array.from(table.querySelectorAll('thead th,[role=columnheader]'),
            x => x.innerText.trim().toLowerCase()) : [];
        const cells = Array.from(row.querySelectorAll('td,[role=cell],[role=gridcell]'));
        const link = row.querySelector('a[href*="review_id="]');
        let id = row.getAttribute('data-review-id');
        if (!id && link) {
            const u = new URL(link.href);
            if (u.origin === location.origin && u.pathname.startsWith('/app/reviews'))
                id = u.searchParams.get('review_id');
        }
        const fields = {};
        cells.forEach((c,i) => fields[(c.getAttribute('data-label') || headers[i] || '').toLowerCase()] = c.innerText.trim());
        return {id, fields, dataRow: cells.length > 0};
    })""")


def project(rows):
    records = {}
    for row in rows:
        if not row["id"]:
            if row["dataRow"]:
                raise Unavailable("unrecognized_review_id")
            continue
        try:
            key = str(UUID(row["id"]))
        except ValueError:
            raise Unavailable("unrecognized_review_id") from None
        fields = row["fields"]
        body_key = next((k for k in ("текст отзыва", "отзыв", "комментарий покупателя") if k in fields), None)
        if body_key is None:
            raise Unavailable("unrecognized_review_columns")
        status = (fields.get("статус ответа") or fields.get("статус") or "").strip().casefold()
        reply = fields.get("ответ продавца", "").strip()
        if reply  .casefold() in ("—", "-", "нет ответа", "без ответа", "ответить"):
            reply = ""
        answered = ("answered" if reply or status in ("есть ответ продавца", "с ответом продавца")
                    else "unanswered" if status in ("без ответа", "нет ответа продавца") else "unknown")
        records[key] = dict(id=key, text=fields[body_key], rating=fields.get("оценка", ""),
                            product=fields.get("товар", ""), answer=reply, state=answered)
    return records


def scan(page, label, max_pages):
    page.goto(REVIEWS, wait_until="domcontentloaded", timeout=45000)
    try:
        page.get_by_text(label, exact=True).first.wait_for(state="visible", timeout=15000)
    except Exception:
        raise Unavailable("check_login_and_active_shop") from None
    records = {}
    previous = set()
    for index in range(max_pages):
        page.wait_for_selector("td:visible, [role=cell]:visible, [role=gridcell]:visible, [data-review-id]:visible", timeout=15000)
        if not seller_page(page.url) or not page.get_by_text(label, exact=True).first.is_visible():
            raise Unavailable("check_login_and_active_shop")
        batch = project(read_page(page))
        if not batch or set(batch) == previous:
            raise Unavailable("list_not_recognized")
        records.update(batch)
        previous = set(batch)
        nxt = page.get_by_role("button", name=NEXT).or_(page.get_by_role("link", name=NEXT))
        if not nxt.count() or not nxt.first.is_visible() or not nxt.first.is_enabled():
            break
        if index + 1 < max_pages:
            nxt.first.click(timeout=5000)
            for attempt in range(30):
                page.wait_for_timeout(300)
                ids = {r["id"] for r in read_page(page) if r["id"]}
                if ids and ids != previous:
                    break
    return records


def save(db, account, records):
    now = datetime.now(timezone.utc).isoformat()
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS reviews(account TEXT, id TEXT, data TEXT NOT NULL,
                   first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(account,id))""")
        new = 0
        for key, value in records.items():
            data = json.dumps(value, ensure_ascii=False)
            cur = db.execute("INSERT OR IGNORE INTO reviews VALUES(?,?,?,?,?)",(account,key,data,now,now))
            new += cur.rowcount
            db.execute("UPDATE reviews SET data=?, last_seen=? WHERE account=? AND id=?", (data,now,account,key))
    return new


def sync(db, account, host):
    # SSH authenticates with the operator's existing config. No browser profile is transferred.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
        raise Unavailable("invalid_ssh_alias")
    db.execute("""CREATE TABLE IF NOT EXISTS sync_progress(
        host TEXT,account TEXT,seen TEXT,id TEXT,PRIMARY KEY(host,account))""")
    checkpoint = db.execute("SELECT seen,id FROM sync_progress WHERE host=? AND account=?",
                            (host,account)).fetchone() or ("","")
    cursor = db.execute("""SELECT data,last_seen,id FROM reviews WHERE account=? AND
                        (last_seen,id)>(?,?) ORDER BY last_seen,id""",(account,*checkpoint))
    while rows := cursor.fetchmany(100):
        payload = dict(account=account, rows=[dict(json.loads(data), seen=ts) for data,ts,_ in rows])
        remote = ("/opt/mp-analytics/venv/bin/python "
                  "/opt/mp-analytics/.claude/worktrees/rev-recovery-20261103/tools/ozon_lk/import_office.py")
        child = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote],
                               input=json.dumps(payload).encode(), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=45)
        if child.returncode:
            raise Unavailable("saved_locally_ssh_sync_failed")
        with db:
            db.execute("INSERT OR REPLACE INTO sync_progress VALUES(?,?,?,?)",
                       (host, account, rows[-1][1], rows[-1][2]))


def run(account, login, root, max_pages, host):
    from playwright.sync_api import sync_playwright
    folder = root / account
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding = folder / "shop.txt"
    if not login and not binding.is_file():
        raise Unavailable("login_required")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(folder / "profile"), channel="chrome", headless=not login, locale="ru-RU")
        try:
            page = ctx.new_page()
            if login:
                page.goto(REVIEWS, wait_until="domcontentloaded")
                label = input(f"{account}: войдите, выберите магазин, откройте «Отзывы». Введите точное видимое название активного магазина: ").strip()
                if not label or not seller_page(page.url) or not page.get_by_text(label, exact=True).first.is_visible():
                    raise Unavailable("shop_label_not_visible")
                for other in ACCOUNTS:
                    path = root / other / "shop.txt"
                    if other != account and path.exists() and path.read_text(encoding="utf8").strip().casefold() == label.casefold():
                        raise Unavailable("same_shop_for_two_accounts")
                binding.write_text(label, encoding="utf8")
                return "profile_saved"
            records = scan(page, binding.read_text(encoding="utf8").strip(), max_pages)
            with closing(sqlite3.connect(root / "reviews.sqlite3", timeout=30)) as db:
                added = save(db, account, records)
                if host:
                    sync(db, account, host)
            return f"seen={len(records)} new={added}"
        finally:
            ctx.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("login","poll"))
    parser.add_argument("account", choices=(*ACCOUNTS,"all"))
    parser.add_argument("--ssh", default=os.getenv("REV_OZON_SSH"), help="Existing SSH alias for cloud-001")
    parser.add_argument("--data", type=Path, default=HOME)
    parser.add_argument("--max-pages", type=int, default=10, choices=range(1,101), metavar="1..100")
    args=parser.parse_args()
    if args.action=="login" and args.account=="all":
        parser.error("Вход выполняется отдельно по каждому кабинету")
    os.umask(0o077)
    args.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    failed=False
    for account in ACCOUNTS if args.account=="all" else (args.account,):
        try:
            result=run(account,args.action=="login",args.data,args.max_pages,args.ssh)
        except Exception as exc:
            result="unavailable: "+(str(exc) if isinstance(exc,Unavailable) else type(exc).__name__)
            failed=True
        print(account,result)
        (args.data / (account+".status")).write_text(datetime.now(timezone.utc).isoformat()+" "+result, encoding="utf8")
    return int(failed)


if __name__=="__main__":
    sys.exit(main())
