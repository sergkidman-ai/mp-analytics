"""ops/wb_promo_plan_import.py — поток: mkt
Загрузка плановых цен акции ВБ из выгрузки личного кабинета в `wb_promo_plan_price`.

Откуда файл: Сергей/оператор скачивает в ЛК «Все товары, подходящие для акции …» за сутки
до старта (бот предупреждает) и кидает в бота-дропбокс → /opt/mp-analytics/dropbox.
Кабинет определяется по колонке «Бренд»: «Цифровой квадрат» → wb_acc1, «Dsquare» → wb_acc2.

Зачем: у автоакций ВБ не отдаёт плановую цену через API (`nomenclatures` → 422), а это потолок
участия. Без него сторож не знает, сколько можно забрать, и отдаёт скидку целиком.
Плановая цена держится всю акцию, меняется только наше участие (решение/наблюдение Сергея 19.09).

Запуск:
    ./venv/bin/python -m ops.wb_promo_plan_import                 # два свежих файла из dropbox
    ./venv/bin/python -m ops.wb_promo_plan_import --file путь.xl  # конкретный файл
    ./venv/bin/python -m ops.wb_promo_plan_import --valid-to 2026-09-27
"""
import argparse
import datetime as dt
import glob
import os
import pathlib
import re
import sys

import pandas as pd

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                              # noqa: E402

DROPBOX = "/opt/mp-analytics/dropbox"
BRAND_ACC = {"цифровой квадрат": "wb_acc1", "dsquare": "wb_acc2"}
COLS = {"nm": "Артикул WB", "vc": "Артикул поставщика", "plan": "Плановая цена для акции",
        "retail": "Текущая розничная цена", "disc": "Текущая скидка на сайте, %",
        "inp": "Товар уже участвует в акции", "brand": "Бренд"}


def read_file(path, nrows=None):
    d = pd.read_excel(path, dtype={COLS['vc']: str}, nrows=nrows)
    d.columns = [str(c).strip() for c in d.columns]
    miss = [v for v in COLS.values() if v not in d.columns]
    if miss:
        raise RuntimeError(f"{os.path.basename(path)}: нет колонок {miss}")
    brands = d[COLS["brand"]].dropna().astype(str).str.strip().str.lower().value_counts()
    acc = next((BRAND_ACC[b] for b in brands.index if b in BRAND_ACC), None)
    if not acc:
        raise RuntimeError(f"{os.path.basename(path)}: бренд не опознан ({list(brands.index)[:3]})")
    # Имя акции — из имени файла: «Все товары подходящие для акции_<НАЗВАНИЕ>_дата.xlsx»
    # (у обычных «Товары для акции_…», у выгрузки на исключение «Товары для исключения из акции_…»). Бот добавляет спереди «дата_время_Имя_логин_»
    # и заменяет пробелы на «_», поэтому ищем саму фразу, а не режем N первых токенов.
    base = re.sub(r"\.(xlsx?|xl)$", "", os.path.basename(path)).replace("_", " ")
    m = re.search(r"(?:Все товары подходящие для акции|Товары для акции|Товары для исключения из акции)\s*(.+)$", base)
    m = (m.group(1) if m else base)
    m = re.sub(r"\s*\d{2}[. ]\d{2}[. ]\d{4}.*$", "", m).strip(" _")   # хвост «20.09.2026 11.34.52» (бот меняет _ на пробел)
    m = re.sub(r"\s+\d{2}[. ]\d{2}$", "", m)  # ручной хвост даты «03_11»
    m = re.sub(r"(автоматические(?: скидки)?)\s+\d{1,2}$", r"\1", m, flags=re.I)
    return acc, (m or base)[:120], d


def promotion_window(account, name):
    """Resolve the LK export against the live calendar; never invent expiry dates."""
    from ops.wb_auto_entry import Client
    from ops.wb_promo_guard import CAL_API
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(dt.timezone.utc)
    def key(value):
        return re.sub(r'[^а-яa-z0-9]', '', value.lower().replace('скидки', ''))
    value = Client(account, 90).get('', url=CAL_API, params={
        'startDateTime': (now - dt.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'endDateTime': (now + dt.timedelta(days=14)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'allPromo': 'false', 'limit': 100})
    matches = [p for p in value.get('data', {}).get('promotions', [])
               if key(p['name']) == key(name)
               and dt.datetime.fromisoformat(p['endDateTime'].replace('Z', '+00:00')) >= now]
    if len(matches) != 1:
        raise RuntimeError('Не найден единственный действующий срок акции в календаре WB')
    p = matches[0]
    local = ZoneInfo('Europe/Moscow')
    start, end = [dt.datetime.fromisoformat(p[k].replace('Z', '+00:00')).astimezone(local).date()
                  for k in ('startDateTime', 'endDateTime')]
    return start.isoformat(), end.isoformat()


def load_regular(account):
    """Regular campaigns expose exact membership/caps via API; include their lower caps."""
    from ops.wb_auto_entry import Client
    from ops.wb_promo_guard import CAL_API
    from zoneinfo import ZoneInfo
    client = Client(account, 180)
    now = dt.datetime.now(dt.timezone.utc)
    local = ZoneInfo('Europe/Moscow')
    for promo in client.calendar().values():
        start, end = [dt.datetime.fromisoformat(promo[k].replace('Z', '+00:00'))
                      for k in ('startDateTime', 'endDateTime')]
        if promo['type'] != 'regular' or not start <= now <= end:
            continue
        data = []
        for offset in range(0, 100000, 1000):
            value = client.get('', url=CAL_API + '/nomenclatures', params={
                'promotionID': promo['id'], 'inAction': 'true', 'limit': 1000, 'offset': offset})
            client.raw(f"regular_{promo['id']}_{offset}", value)
            page = value.get('data', {}).get('nomenclatures')
            if not isinstance(page, list):
                raise RuntimeError('Incomplete regular campaign response')
            data.extend(page)
            if len(page) < 1000:
                break
        else:
            raise RuntimeError('Regular campaign page bound exceeded')
        if len({int(r['id']) for r in data}) != len(data):
            raise RuntimeError('Regular campaign pagination duplicate')
        rows = []
        for r in data:
            if r.get('currencyCode') != 'RUB':
                raise RuntimeError('Regular campaign cap is not in RUB')
            if not r.get('inAction') or not r.get('planPrice') or float(r['planPrice']) <= 0:
                raise RuntimeError('Regular campaign participant missing valid cap')
            rows.append(dict(account=account, promo_key=promo['name'][:120], nm_id=int(r['id']),
                vendor_code=None, plan_price=float(r['planPrice']), retail_price=r.get('price'),
                disc_pct=r.get('discount'), in_promo=True, promo_name=promo['name'][:200],
                valid_from=start.astimezone(local).date().isoformat(),
                valid_to=end.astimezone(local).date().isoformat(), source_file=f"api:calendar:{promo['id']}"))
        if rows:
            db.upsert('wb_promo_plan_price', rows,
                conflict_cols=['account', 'promo_key', 'nm_id', 'valid_from'],
                update_cols=['plan_price', 'retail_price', 'disc_pct', 'in_promo', 'valid_to', 'source_file', 'loaded_at'])
        db.execute('''update wb_promo_plan_price set in_promo=false
                      where account=%s and promo_key=%s and not (nm_id=any(%s::bigint[]))''',
                   (account, promo['name'][:120], [r['nm_id'] for r in rows]))
        print(f"  {account} API · {promo['name']}: участников {len(rows)}", flush=True)


def load(path, valid_from, valid_to):
    acc, promo, d = read_file(path)
    if not valid_to:
        valid_from, valid_to = promotion_window(acc, promo)
    if str(valid_to) < str(valid_from):
        raise ValueError('Дата конца акции раньше начала')
    rows = []
    for r in d.itertuples(index=False):
        rec = dict(zip(d.columns, r))
        nm, plan = rec.get(COLS["nm"]), rec.get(COLS["plan"])
        if pd.isna(nm) or pd.isna(plan) or float(plan) <= 0:
            continue
        rows.append({"account": acc, "promo_key": promo, "nm_id": int(nm),
                     # артикул в файле уже без «/»: 00011 — это 0001/1, ведущий ноль значащий
                     "vendor_code": None if pd.isna(rec.get(COLS["vc"])) else str(rec[COLS["vc"]]).strip(),
                     "plan_price": round(float(plan), 2),
                     "retail_price": None if pd.isna(rec.get(COLS["retail"])) else round(float(rec[COLS["retail"]]), 2),
                     "disc_pct": None if pd.isna(rec.get(COLS["disc"])) else round(float(rec[COLS["disc"]]), 2),
                     "in_promo": str(rec.get(COLS["inp"], "")).strip().lower() == "да",
                     "promo_name": promo[:200], "valid_from": valid_from, "valid_to": valid_to,
                     "source_file": os.path.basename(path)[:200]})
    if rows:
        db.upsert("wb_promo_plan_price", rows, conflict_cols=["account", "promo_key", "nm_id", "valid_from"],
                  update_cols=["vendor_code", "plan_price", "retail_price", "disc_pct", "in_promo",
                               "promo_name", "valid_to", "source_file", "loaded_at"])
    inp = sum(1 for x in rows if x["in_promo"])
    print(f"  {acc} · {promo[:42]:<42} строк {len(rows):>6}, в акции {inp:>6}", flush=True)
    return acc, len(rows), inp


def check():
    """Фактический замер: держится ли плановая цена всю акцию.

    Прямо спросить ВБ нельзя (у автоакций `nomenclatures` → 422), поэтому меряем косвенно, по
    карточкам, которые СТОЯТ РОВНО НА ПЛАНОВОЙ ЦЕНЕ: их цену держит сам ВБ, и если план поедет,
    поедут и они. Расхождение ≤1 ₽ считаем совпадением (округление скидки в процентах)."""
    for acc in ("wb_acc1", "wb_acc2"):
        r = db.query("""with p as (select distinct on (account, nm_id) * from wb_promo_plan_price
                            where account = %s order by account, nm_id, valid_from desc)
              select count(*) filter (where p.in_promo) in_promo,
                     count(*) filter (where p.in_promo and abs(pr.discounted_price - p.plan_price) <= 1) at_plan,
                     count(*) filter (where p.in_promo and pr.discounted_price > p.plan_price + 1) above_plan,
                     max(p.valid_from) plan_from, max(pr.captured_at)::timestamp(0) price_at
                from p join wb_price pr on pr.account = p.account and pr.nm_id = p.nm_id""", (acc,))[0]
        print(f"  {acc}: в акции {r['in_promo']}, стоят ровно на плане {r['at_plan']}, "
              f"выше плана {r['above_plan']} (план от {r['plan_from']}, цены на {r['price_at']})", flush=True)
    print("  Повтор этой команды в следующие дни покажет, поехал ли план: у карточек «ровно на плане»\n"
          "  цена обязана остаться той же, пока акция идёт.", flush=True)


def auto(valid_to=None, days=7):
    """Сам забирает из бота-дропбокса всё, что ещё не грузили.

    Наталья кидает выгрузки в бота за сутки до старта акции; повторно тот же файл не берём —
    отметка в `source_file`. Архивы .zip распаковываются во временный каталог.
    Плановая цена держится всю акцию (проверено 19→21.09: 22 719 карточек, ни одного изменения),
    поэтому дата выгрузки и есть valid_from, переписывать её каждый день не нужно."""
    import tempfile, zipfile
    known = {r["source_file"] for r in db.query("select distinct source_file from wb_promo_plan_price")}
    edge = dt.date.today() - dt.timedelta(days=days)
    todo = []
    for f in sorted(glob.glob(os.path.join(DROPBOX, "*"))):
        base = os.path.basename(f)
        m = re.match(r"^(\d{4})(\d{2})(\d{2})_", base)
        if not m or dt.date(*map(int, m.groups())) < edge:
            continue
        if base.lower().endswith((".xls", ".xlsx", ".xl")):
            todo.append((f, base, m.group(0)))
        elif base.lower().endswith(".zip") and base not in known:
            tmp = tempfile.mkdtemp(prefix="wbplan_")
            try:
                with zipfile.ZipFile(f) as z:
                    z.extractall(tmp)
            except Exception as e:                                    # noqa: BLE001
                print(f"  архив {base[:40]} не читается: {e}", flush=True)
                continue
            for inner in sorted(glob.glob(os.path.join(tmp, "**", "*.xls*"), recursive=True)):
                todo.append((inner, base, m.group(0)))
    # source_file on a plan is replaced by newer exports. Loading an older file
    # merely because its name disappeared from the table would roll prices back.
    latest = {}
    for path, mark, stamp in todo:
        try:
            account, promo, _ = read_file(path, nrows=5)
        except Exception:
            continue
        key = (account, promo)
        if key not in latest or (mark, path) > (latest[key][1], latest[key][0]):
            latest[key] = (path, mark, stamp)
    n = 0
    for path, mark, stamp in sorted(latest.values()):
        if os.path.basename(path)[:200] in known or mark in known:
            continue
        vf = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
        try:
            load(path, vf, valid_to)
            n += 1
        except Exception as e:                                        # noqa: BLE001
            # про старые чужие файлы (PRC, фото) молчим: они лежат в боте вечно и иначе
            # засоряют лог каждые два часа
            if dt.date(*map(int, (stamp[0:4], stamp[4:6], stamp[6:8]))) >= dt.date.today() - dt.timedelta(days=2):
                print(f"  ПРОПУСК {os.path.basename(path)[:44]}: {e}", flush=True)
    print(f"автозагрузка: новых файлов {n}", flush=True)
    for account in BRAND_ACC.values():
        load_regular(account)


def main():
    ap = argparse.ArgumentParser(description="ВБ: загрузка плановых цен акции из выгрузки ЛК")
    ap.add_argument("--file", action="append", help="файл(ы); по умолчанию свежие из dropbox")
    ap.add_argument("--dir", help="каталог с выгрузками (например распакованный zip из бота)")
    ap.add_argument("--valid-from", default=dt.date.today().isoformat(), help="дата выгрузки")
    ap.add_argument("--valid-to", help="дата конца акции (для отбора живых цен)")
    ap.add_argument("--check", action="store_true", help="замер: держится ли плановая цена")
    ap.add_argument("--auto", action="store_true",
                    help="забрать из бота-дропбокса всё новое (для крона)")
    a = ap.parse_args()
    if a.check:
        check()
        return
    if a.auto:
        auto(a.valid_to)
        return
    if a.dir:
        files = sorted(glob.glob(os.path.join(a.dir, "**", "*.xls*"), recursive=True)
                       + glob.glob(os.path.join(a.dir, "**", "*.xl"), recursive=True))
    else:
        files = a.file or sorted(glob.glob(os.path.join(DROPBOX, "*.xl*")), key=os.path.getmtime,
                             reverse=True)[:2]
    if not files:
        print("файлов не найдено", flush=True)
        return
    seen = {}
    for f in files:
        try:
            acc, n, inp = load(f, a.valid_from, a.valid_to)
        except Exception as e:                                        # noqa: BLE001
            print(f"  ПРОПУСК {os.path.basename(f)[:50]}: {e}", flush=True)
            continue
        seen.setdefault(acc, []).append(inp)
    for acc, v in seen.items():
        print(f"{acc}: акций загружено {len(v)}, суммарно участий {sum(v)}", flush=True)


if __name__ == "__main__":
    main()
