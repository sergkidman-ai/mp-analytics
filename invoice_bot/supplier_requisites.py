# -*- coding: utf-8 -*-
# поток: inv
"""
Реквизиты поставщиков: счёт = источник истины → перезапись карточки МС.

Модель (согласовано):
  • единственно верный источник банковских реквизитов поставщика — его СЧЁТ (блок «Получатель»);
  • по 9 рабочим поставщикам парсим последний счёт → перезаписываем реквизиты карточки МС;
  • черновики платёжных поручений (Альфа H2H) берут реквизиты получателя ИЗ МС;
  • смена реквизитов у поставщика (он сообщает отдельно) → по команде репарсим его счёт и перезаписываем.
  • трогаем ТОЛЬКО эти 9 карточек, в чужие не смотрим.

Банк-блок счёта на оплату принадлежит ПОЛУЧАТЕЛЮ (=поставщик, кому мы платим); плательщик (мы)
свой банк в счёте обычно не печатает. р/с = 407…/408…, корсчёт = 301…, БИК = 04… — различаем по префиксу.

CLI (из корня проекта):
  ./venv/bin/python invoice_bot/supplier_requisites.py --sync            # dry-run диф «счёт → МС» по 9
  ./venv/bin/python invoice_bot/supplier_requisites.py --sync --apply    # + записать в МС
  ./venv/bin/python invoice_bot/supplier_requisites.py --supplier <ИНН>  # dry-run по одному (смена рекв.)
  ./venv/bin/python invoice_bot/supplier_requisites.py --supplier <ИНН> --apply
"""
import os, sys, re, json, glob, argparse

ROOT = "/opt/mp-analytics"                      # проект живёт только здесь (как ms.py)
sys.path.insert(0, os.path.join(ROOT, "invoice_bot"))   # плоские импорты, как в invoice_to_po
from invoice_to_po import read_grid, grid_text, parse_header, SUPPLIERS, AGENT_OVERRIDE   # noqa
from ms import get, post, put   # noqa

INBOX_DIRS = [f"{ROOT}/invoice_bot/inbox_mail", f"{ROOT}/invoice_bot/inbox"]
INV_EXT = (".xls", ".xlsx", ".pdf", ".XLS")

# 9 рабочих поставщиков (по которым накоплены счета в MC_invoicebot). Трогаем ТОЛЬКО их карточки.
NINE = {
    "9731107362": "Феррет", "7730244274": "Одиссей", "9718075418": "Картридж Трейд (Блоссом)",
    "7725744338": "Тонеропттторг", "7722341813": "КВК Трейд", "7806486149": "Солюшнс принт МСК",
    "9717092410": "Тонерстор", "7840480595": "Колортек", "7736123276": "Позитив",
}


def is_upd(name, text):
    """УПД/передаточный документ — не счёт-на-оплату, банк-блок брать нельзя."""
    n = name.lower().replace("_", " ")
    return "упд" in n or "передаточ" in n or "передаточный документ" in text.lower()


# ─────────────────────────── извлечение реквизитов из текста счёта ───────────────────────────
def _digits(s):
    return re.sub(r"\D", "", s)


def _accounts(text):
    """Все 20-значные счета из текста (учёт пробелов-разделителей)."""
    out = []
    for m in re.finditer(r"(?<!\d)(\d[\d ]{18,26}\d)(?!\d)", text):
        d = _digits(m.group(1))
        if len(d) == 20:
            out.append(d)
    return out


def acc_control_ok(bic, acc):
    """Контрольный ключ расчётного счёта РФ (по последним 3 цифрам БИК). None если размеры не те."""
    if not (bic and acc and len(bic) == 9 and len(acc) == 20 and acc.isdigit()):
        return None
    base = bic[-3:] + acc            # 23 цифры
    w = [7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1]
    return sum(int(base[i]) * w[i] for i in range(23)) % 10 == 0


def parse_requisites(text, supplier_inn):
    """Текст счёта → {inn, kpp, account, bic, corr, bank, checks}. Банк-блок = сторона Получателя."""
    r = {"inn": supplier_inn, "kpp": None, "account": None, "bic": None, "corr": None, "bank": None}

    # БИК — по метке; в XLS иногда хранится числом → теряется ведущий 0 (44030920 → 044030920)
    m = re.search(r"БИК[^\d]{0,12}(\d{8,9})", text)
    bic = m.group(1) if m else None
    if bic and len(bic) == 8:
        bic = "0" + bic
    if bic and re.match(r"04\d{7}$", bic):
        r["bic"] = bic

    # счета: р/с (407/408), корсчёт (301). В счёте на оплату банк-блок ТОЛЬКО у получателя
    # (плательщик свой р/с не печатает) → берём первые из текста безопасно.
    accs = _accounts(text)
    r["account"] = next((a for a in accs if a[:3] in ("407", "408")), None)
    r["corr"] = next((a for a in accs if a[:3] == "301"), None)

    # КПП получателя — СТРОГО якорим к поставщику (в счёте два блока ИНН/КПП: получатель и плательщик).
    # Если уверенно не извлекли — оставляем None (карточку МС не трогаем), чтобы не записать чужой КПП.
    m = re.search(r"КПП\s*поставщика\s*[:№]?\s*(\d{9})", text)          # формат Солюшнс
    if not m:
        m = re.search(rf"(?<!\d){supplier_inn}\D{{0,8}}(\d{{9}})(?!\d)", text)  # «ИНН <inn> КПП <kpp>»
    if m:
        r["kpp"] = m.group(1)

    # имя банка — best-effort (в МС оно уже есть; для платёжки не обязательно)
    for line in text.splitlines():
        mm = re.search(r"Банк\s*получателя[:\s]+(.+?)(?:\s+БИК|\s+Сч|$)", line)
        if mm and mm.group(1).strip():
            r["bank"] = mm.group(1).strip()[:60]
            break

    r["checks"] = {
        "account_len": bool(r["account"]) and len(r["account"]) == 20,
        "corr_len": bool(r["corr"]) and len(r["corr"]) == 20,
        "bic_len": bool(r["bic"]) and len(r["bic"]) == 9,
        "corr_matches_bic": bool(r["corr"] and r["bic"]) and r["corr"][-3:] == r["bic"][-3:],
        "account_control": acc_control_ok(r["bic"], r["account"]),
    }
    return r


def requisites_complete(r):
    c = r.get("checks", {})
    return bool(c.get("account_len") and c.get("bic_len") and c.get("corr_len")
               and c.get("corr_matches_bic") and c.get("account_control") is not False)


# ─────────────────────────── поиск последнего счёта поставщика ───────────────────────────
def iter_invoice_files():
    for d in INBOX_DIRS:
        for p in glob.glob(os.path.join(d, "*")):
            if p.endswith(INV_EXT):
                yield p


def latest_invoice_by_supplier():
    """Скан inbox → для каждого supplier_inn ПОСЛЕДНИЙ СЧЁТ по ДАТЕ ДОКУМЕНТА → {inn: (path, header)}.
    Ранжируем по (дата счёта, mtime): реквизиты берём из самого свежего ВЫСТАВЛЕННОГО счёта, а не
    из последнего по времени файла (у старого счёта .fixed.xlsx может иметь более поздний mtime)."""
    best = {}
    for p in iter_invoice_files():
        try:
            kind, payload = read_grid(p)
            text = grid_text(kind, payload)
            h = parse_header(text)              # бросит, если это не «Счёт № … от …» (напр. УПД) → пропуск
        except SystemExit:
            continue
        except Exception:
            continue
        inn = h.get("supplier_inn")
        if not inn or inn not in NINE:
            continue
        if is_upd(os.path.basename(p), text):
            continue
        rank = (h.get("inv_date"), os.path.getmtime(p))   # дата документа — главный ключ
        if inn not in best or rank > best[inn][2]:
            best[inn] = (p, text, rank)
    return {inn: (p, text) for inn, (p, text, _) in best.items()}


# ─────────────────────────── сторона МС ───────────────────────────
def resolve_counterparty(inn):
    """(counterparty_id, name) карточки, из которой платим. Учитывает override Солюшнс МСК."""
    if inn in AGENT_OVERRIDE:
        cid = AGENT_OVERRIDE[inn]
        cp = get(f"/entity/counterparty/{cid}")
        return cid, cp.get("name")
    rows = get(f"/entity/counterparty?filter=inn={inn}").get("rows", [])
    if not rows:
        return None, None
    return rows[0]["id"], rows[0].get("name")


def ms_accounts(cid):
    return get(f"/entity/counterparty/{cid}/accounts").get("rows", [])


def ms_default_account(accs):
    for a in accs:
        if a.get("isDefault"):
            return a
    return accs[0] if accs else None


# ─────────────────────────── диф и вывод ───────────────────────────
def _mask(s):
    if not s:
        return "—"
    s = str(s)
    return s[:4] + "…" + s[-4:] if len(s) > 8 else s


def diff_supplier(inn, inv_path, inv_text):
    prof = SUPPLIERS.get(inn, {})
    r = parse_requisites(inv_text, inn)
    cid, cname = resolve_counterparty(inn)
    ms_kpp = None
    ms = None
    if cid:
        cp = get(f"/entity/counterparty/{cid}")
        ms_kpp = cp.get("kpp")
        ms = ms_default_account(ms_accounts(cid))
    ms_acc = ms.get("accountNumber") if ms else None
    ms_bic = ms.get("bic") if ms else None
    ms_corr = ms.get("correspondentAccount") if ms else None

    changes = []
    if r["account"] and _digits(str(ms_acc or "")) != r["account"]:
        changes.append(f"р/с {_mask(ms_acc)} → {_mask(r['account'])}")
    if r["bic"] and (ms_bic or "") != r["bic"]:
        changes.append(f"БИК {ms_bic or '—'} → {r['bic']}")
    if r["corr"] and _digits(str(ms_corr or "")) != r["corr"]:
        changes.append(f"корсчёт {_mask(ms_corr)} → {_mask(r['corr'])}")
    if r["kpp"] and (ms_kpp or "") != r["kpp"]:
        changes.append(f"КПП {ms_kpp or '—'} → {r['kpp']}")

    return {
        "inn": inn, "name": prof.get("name", cname), "cid": cid,
        "invoice": os.path.basename(inv_path),
        "req": r, "complete": requisites_complete(r),
        "ms_present": bool(cid), "changes": changes,
    }


def print_report(rows):
    print("=" * 100)
    print(f"{'Поставщик':26s} {'счёт распознан':14s} {'МС карточка':12s} изменения")
    print("-" * 100)
    for d in rows:
        ok = "полн ✅" if d["complete"] else "НЕПОЛН ⚠"
        card = "есть" if d["ms_present"] else "НЕТ ❌"
        ch = "нет изменений" if not d["changes"] else "; ".join(d["changes"])
        print(f"{(d['name'] or d['inn'])[:26]:26s} {ok:14s} {card:12s} {ch}")
        c = d["req"]["checks"]
        bad = [k for k, v in c.items() if v is False]
        if bad:
            print(f"{'':26s}   ⚠ провал проверок: {', '.join(bad)}  (файл: {d['invoice']})")
    print("-" * 100)
    print(f"поставщиков: {len(rows)} | реквизиты полны: {sum(r['complete'] for r in rows)} | "
          f"с изменениями к записи: {sum(1 for r in rows if r['changes'])}")
    print("=" * 100)


# ─────────────────────────── запись в МС (только с --apply) ───────────────────────────
def apply_to_ms(d):
    """Синхронизировать default-счёт карточки МС под реквизиты счёта. Идемпотентно."""
    if not d["complete"]:
        print(f"  [skip] {d['name']}: реквизиты счёта неполны — не пишу")
        return False
    if not d["cid"]:
        print(f"  [skip] {d['name']}: нет карточки в МС")
        return False
    if not d["changes"]:
        print(f"  [ok] {d['name']}: уже совпадает, правок нет")
        return False
    r = d["req"]
    accs = ms_accounts(d["cid"])
    target = next((a for a in accs if _digits(str(a.get("accountNumber") or "")) == r["account"]), None)
    new_list = []
    for a in accs:
        a2 = {k: a[k] for k in ("id", "accountNumber", "bankName", "bankLocation",
                                "correspondentAccount", "bic", "isDefault") if k in a}
        a2["isDefault"] = False
        new_list.append(a2)
    if target:
        for a2 in new_list:
            if a2.get("id") == target.get("id"):
                a2["correspondentAccount"] = r["corr"]
                a2["bic"] = r["bic"]
                if r["bank"]:
                    a2["bankName"] = r["bank"]
                a2["isDefault"] = True
    else:
        new_list.append({"accountNumber": r["account"], "bic": r["bic"],
                         "correspondentAccount": r["corr"],
                         "bankName": r["bank"] or "", "isDefault": True})
    st, resp = post(f"/entity/counterparty/{d['cid']}/accounts", new_list)
    ok = st in (200, 201)
    # КПП карточки — при расхождении
    if r["kpp"]:
        cp = get(f"/entity/counterparty/{d['cid']}")
        if (cp.get("kpp") or "") != r["kpp"]:
            put(f"/entity/counterparty/{d['cid']}", {"kpp": r["kpp"]})
    print(f"  [{'записано' if ok else 'ОШИБКА '+str(st)}] {d['name']}: {'; '.join(d['changes'])}")
    return ok


# ─────────────────────────── CLI ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true", help="по всем 9 поставщикам")
    ap.add_argument("--supplier", help="ИНН одного поставщика (репарс при смене реквизитов)")
    ap.add_argument("--apply", action="store_true", help="записать изменения в МС (иначе dry-run)")
    a = ap.parse_args()

    inv = latest_invoice_by_supplier()
    if a.supplier:
        targets = {a.supplier: inv.get(a.supplier)}
        if targets[a.supplier] is None:
            print(f"Счёт поставщика {a.supplier} не найден в inbox")
            return
    else:
        targets = inv

    rows = []
    for inn in (NINE if not a.supplier else [a.supplier]):
        if inn not in targets or targets[inn] is None:
            continue
        p, text = targets[inn]
        rows.append(diff_supplier(inn, p, text))

    print_report(rows)
    if a.apply:
        print("\n── применение в МС ──")
        for d in rows:
            apply_to_ms(d)
    else:
        print("\n(dry-run — в МС ничего не записано; для записи добавьте --apply)")


if __name__ == "__main__":
    main()
