# поток: inv
"""collectors/bank_file_import.py — ручной импорт выписки из ФАЙЛА в `bank_txn`.

Зачем: у Озон Банка (счета есть у обеих фирм) API нет вообще, выписку можно только
выгрузить руками из личного кабинета. Контур разметки опер. расходов при этом должен
остаться единым: те же таблицы, те же правила по контрагенту, тот же экран «Выписки».
Поэтому файл разбирается здесь в те же нормализованные записи, что отдают
`alfa_statement.normalize` / `sber_statement.normalize`, и уходит в общий
`bank_txn_store.store()`.

Форматы:
  * **1С** (`1CClientBankExchange`, .txt) — основной и самый полный: есть ИНН, счета,
    БИК, номер и дата документа, назначение. Кодировка cp1251 или utf-8 — определяется.
  * **PDF** (`parse_pdf`) — печатная форма, которую Озон Банк отдаёт по расчётному счёту.
    Разбор по координатам слов (`pdftotext -bbox-layout` из poppler-utils), а не по колонкам
    из пробелов; полнота проверяется по «Итого обороты» самой выписки.
  * **CSV / XLSX** — фолбэк по заголовкам колонок (банки называют их по-разному,
    поэтому распознавание — по ключевым словам). Если обязательную колонку не нашли,
    падаем с перечнем реальных заголовков файла, а не молча импортируем мусор.

Направление (DEBIT/CREDIT) в файле явно не указано: считаем по нашему счёту —
если плательщик мы, это расход. Когда счёт в файле не указан, фолбэк — по ИНН нашей
организации.

Идемпотентность: uuid банк не даёт, поэтому натуральный ключ — хеш реквизитов
(`bank_txn_store._nk`): счёт|дата|сумма|номер|назначение. Повторная загрузка того же
файла (и файла с перекрытием периода) новых строк не создаёт.

CLI:
    ./venv/bin/python collectors/bank_file_import.py <файл> --bank ozon \
        --org 7807355364 [--account 40702810...] [--since 2026-01-01] [--dry]
"""
import argparse
import csv
import io
import hashlib
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from collectors import bank_txn_store                       # noqa: E402

SINCE_DEFAULT = "2026-01-01"        # выписку ведём с начала года (решение Сергея 03.08.2026)


# ── общее ────────────────────────────────────────────────────────────────────
def decode(data):
    """Байты файла → текст. Русские банки отдают 1С-файл в cp1251, реже в utf-8."""
    if isinstance(data, str):
        return data
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp1251", errors="replace")


def _date(s):
    """'15.01.2026' | '2026-01-15' | '15.01.2026 12:33' → '2026-01-15'. Иначе None."""
    s = str(s or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{2})[.\-/](\d{2})[.\-/](\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else None


def _amount(s):
    """'1 000,50' | '1000.50' | '-1 000,50' → float. Пусто/мусор → None."""
    if s is None or isinstance(s, (int, float)):
        return float(s) if s not in (None, "") else None
    t = re.sub(r"[^\d,.\-]", "", str(s)).replace(",", ".")
    if t.count(".") > 1:                       # '1.000.50' — точки как разделители тысяч
        head, _, tail = t.rpartition(".")
        t = head.replace(".", "") + "." + tail
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None


def _digits(s):
    return re.sub(r"\D", "", str(s or "")) or None


def _row(bank, account, direction, day, amount, purpose, doc_no=None, doc_date=None,
         cp_name=None, cp_inn=None, cp_kpp=None, cp_acc=None, cp_bic=None, raw=None):
    """Нормализованная запись — ключи совпадают с normalize() Альфы и Сбера."""
    return {
        "bank": bank, "account": account, "uuid": None, "transaction_id": None,
        "direction": direction, "amount": amount, "currency": "RUB",
        "operation_date": day, "document_date": doc_date or day,
        "document_number": doc_no, "purpose": purpose,
        "counterparty_name": cp_name, "counterparty_inn": cp_inn, "counterparty_kpp": cp_kpp,
        "counterparty_account": cp_acc, "counterparty_bic": cp_bic,
        "_raw": raw,
    }


# ── формат 1С ────────────────────────────────────────────────────────────────
def is_1c(text):
    return text.lstrip().startswith("1CClientBankExchange")


def parse_1c(text, bank, org_inn, account=None):
    """1С-обмен → (список записей, счёт из шапки).

    Наш счёт берём из `СекцияРасчСчет`/`РасчСчет` шапки; параметр `account` его
    перебивает (нужно, когда в файле счёт не указан)."""
    lines = [ln.strip("\r\n") for ln in text.splitlines()]
    head_acc, docs, cur = None, [], None
    for ln in lines:
        key, _, val = ln.partition("=")
        key, val = key.strip(), val.strip()
        if key.startswith("СекцияДокумент"):
            cur = {"_kind": val}
            continue
        if key == "КонецДокумента":
            if cur:
                docs.append(cur)
            cur = None
            continue
        if cur is not None:
            cur[key] = val
        elif key == "РасчСчет" and val and not head_acc:
            head_acc = val

    our = account or head_acc
    our_d = _digits(our)
    ops = []
    for d in docs:
        payer_acc = d.get("ПлательщикСчет") or d.get("ПлательщикРасчСчет")
        payee_acc = d.get("ПолучательСчет") or d.get("ПолучательРасчСчет")
        if our_d and _digits(payer_acc) == our_d:
            direction = "DEBIT"
        elif our_d and _digits(payee_acc) == our_d:
            direction = "CREDIT"
        else:                       # счёт не совпал ни с чем — решаем по ИНН нашей фирмы
            direction = "DEBIT" if _digits(d.get("ПлательщикИНН")) == org_inn else "CREDIT"
        if direction == "DEBIT":
            cp_name = d.get("Получатель1") or d.get("Получатель")
            cp_inn, cp_kpp = d.get("ПолучательИНН"), d.get("ПолучательКПП")
            cp_acc, cp_bic = payee_acc, d.get("ПолучательБИК")
            day = _date(d.get("ДатаСписано")) or _date(d.get("Дата"))
        else:
            cp_name = d.get("Плательщик1") or d.get("Плательщик")
            cp_inn, cp_kpp = d.get("ПлательщикИНН"), d.get("ПлательщикКПП")
            cp_acc, cp_bic = payer_acc, d.get("ПлательщикБИК")
            day = _date(d.get("ДатаПоступило")) or _date(d.get("Дата"))
        amount = _amount(d.get("Сумма"))
        if not day or amount is None:
            continue
        # «ИНН 7807355364 ООО Ромашка» в поле имени — вычищаем префикс, ИНН уже отдельно
        cp_name = re.sub(r"^ИНН\s*\d{10,12}\s*", "", (cp_name or "").strip()) or None
        ops.append(_row(bank, our, direction, day, amount, d.get("НазначениеПлатежа"),
                        doc_no=d.get("Номер"), doc_date=_date(d.get("Дата")),
                        cp_name=cp_name, cp_inn=_digits(cp_inn), cp_kpp=_digits(cp_kpp),
                        cp_acc=cp_acc, cp_bic=_digits(cp_bic), raw=d))
    return ops, our


# ── формат таблицы (CSV / XLSX) ──────────────────────────────────────────────
# Заголовки у банков разные — ищем по ключевым словам в нижнем регистре.
COLS = {
    "date":     ("дата операции", "дата проводки", "дата документа", "дата"),
    "debit":    ("расход", "списание", "дебет", "сумма по дебету", "сумма списания"),
    "credit":   ("приход", "поступление", "кредит", "сумма по кредиту", "сумма зачисления"),
    "amount":   ("сумма операции", "сумма в валюте счета", "сумма"),
    "purpose":  ("назначение платежа", "назначение", "описание", "комментарий"),
    "cp_name":  ("контрагент", "наименование контрагента", "получатель", "плательщик",
                 "корреспондент"),
    "cp_inn":   ("инн контрагента", "инн"),
    "cp_acc":   ("счет контрагента", "счёт контрагента", "счет получателя", "счёт получателя"),
    "cp_bic":   ("бик",),
    "doc_no":   ("номер документа", "№ документа", "номер", "№"),
}


def _match_header(cells):
    """Строка заголовков → {роль: индекс}. Берём первое совпадение по ключевому слову."""
    got = {}
    low = [str(c or "").strip().lower() for c in cells]
    for role, keys in COLS.items():
        for k in keys:
            for i, c in enumerate(low):
                if c and k in c and i not in got.values():
                    got[role] = i
                    break
            if role in got:
                break
    return got


def _table_rows(data, filename):
    """Файл → список строк-списков (XLSX через openpyxl, CSV через sniffer)."""
    if str(filename).lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return [list(r) for r in wb[wb.sheetnames[0]].iter_rows(values_only=True)]
    text = decode(data)
    sample = text[:4000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return [r for r in csv.reader(io.StringIO(text), dialect)]


def parse_table(data, filename, bank, org_inn, account=None):
    """CSV/XLSX выписка → записи. Заголовок ищем в первых 15 строках."""
    rows = _table_rows(data, filename)
    head, hdr_i = None, None
    for i, r in enumerate(rows[:15]):
        got = _match_header(r)
        if "date" in got and ("amount" in got or "debit" in got or "credit" in got):
            head, hdr_i = got, i
            break
    if not head:
        seen = "; ".join(str(c) for c in (rows[0] if rows else [])[:12])
        raise ValueError("не нашёл в файле колонки даты и суммы. Заголовки первой строки: "
                         + (seen or "<пусто>"))

    def cell(r, role):
        i = head.get(role)
        return r[i] if i is not None and i < len(r) else None

    ops = []
    for r in rows[hdr_i + 1:]:
        day = _date(cell(r, "date"))
        if not day:
            continue
        deb, cre = _amount(cell(r, "debit")), _amount(cell(r, "credit"))
        if deb or cre:                        # раздельные колонки прихода и расхода
            direction = "DEBIT" if deb else "CREDIT"
            amount = abs(deb or cre)
        else:                                 # одна колонка суммы: минус = расход
            amount = _amount(cell(r, "amount"))
            if amount is None:
                continue
            direction, amount = ("DEBIT" if amount < 0 else "CREDIT"), abs(amount)
        ops.append(_row(bank, account, direction, day, amount, cell(r, "purpose"),
                        doc_no=(str(cell(r, "doc_no")).strip() if cell(r, "doc_no") else None),
                        cp_name=(str(cell(r, "cp_name")).strip() or None
                                 if cell(r, "cp_name") else None),
                        cp_inn=_digits(cell(r, "cp_inn")), cp_acc=cell(r, "cp_acc"),
                        cp_bic=_digits(cell(r, "cp_bic")),
                        raw={"row": [str(c) for c in r]}))
    return ops, account


# ── формат PDF (Озон Банк отдаёт выписку только печатной формой) ──────────────
# Разбираем по КООРДИНАТАМ слов, а не по «картинке» из пробелов (`pdftotext -layout`):
# в таблице выписки имя контрагента занимает три строки, назначение переносится, а колонки
# в двух выписках одного и того же банка стоят на разной ширине. Координаты дают колонку
# однозначно: границами служат слова шапки («Дебет», «Кредит», «Назначение»), а строкой —
# вертикальная полоса вокруг даты операции, потому что дата у записи ровно одна, а строк текста
# в ней три-четыре.
XHTML = "{http://www.w3.org/1999/xhtml}"
PDF_FOOT = ("всего", "итого", "исходящий")       # подвал таблицы: обороты и остаток, не операции


def _pdf_xml(data):
    """PDF → XHTML с координатами каждого слова (pdftotext -bbox-layout, poppler-utils)."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src, out = pathlib.Path(tmp) / "s.pdf", pathlib.Path(tmp) / "s.xml"
        src.write_bytes(data)
        try:
            subprocess.run(["pdftotext", "-bbox-layout", "-q", str(src), str(out)],
                           check=True, capture_output=True)
        except FileNotFoundError:
            raise ValueError("PDF читает pdftotext из poppler-utils, а его нет в системе "
                             "(apt-get install poppler-utils)")
        except subprocess.CalledProcessError as e:
            raise ValueError("pdftotext не смог прочитать файл: "
                             + (e.stderr or b"").decode("utf-8", "replace")[:200])
        return out.read_text(encoding="utf-8", errors="replace")


def _pdf_pages(data):
    """→ [страница], страница = [(x_начала, x_конца, y, слово)]. Пустые слова выброшены."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_pdf_xml(data))
    pages = []
    for pg in root.iter(XHTML + "page"):
        ws = [(float(w.get("xMin")), float(w.get("xMax")), float(w.get("yMin")),
               (w.text or "").strip()) for w in pg.iter(XHTML + "word")]
        pages.append([w for w in ws if w[3]])
    return [p for p in pages if p]


def _pdf_text(page):
    """Страница → текст построчно (слова одной строки склеены пробелом) — для шапки выписки."""
    rows = {}
    for x0, x1, y, t in sorted(page, key=lambda w: (w[2], w[0])):
        key = next((k for k in rows if abs(k - y) <= 2.5), y)
        rows.setdefault(key, []).append(t)
    return "\n".join(" ".join(v) for _, v in sorted(rows.items()))


def _pdf_anchors(page):
    """Шапка таблицы → x-начала колонок и y шапки; на странице без шапки → None."""
    want = {"date": "дата", "doc": "номер", "debit": "дебет", "credit": "кредит",
            "cp": "контрагент", "acct": "сч", "purpose": "назначение"}
    # Ищем ТОЛЬКО в полосе вокруг слова «Дебет»: шапка таблицы стоит под шапкой самой выписки,
    # а там есть свой «Счет:» с нашим расчётным — по нему граница колонки уезжала влево.
    head_y = min([w[2] for w in page if w[3].lower().startswith("дебет")] or [None])
    if head_y is None:
        return None
    got = {}
    for x0, x1, y, t in sorted(page, key=lambda w: (w[2], w[0])):
        if abs(y - head_y) > 20:
            continue
        low = t.lower().replace("c", "с")            # «Cчёт,» в шапке набран ЛАТИНСКОЙ C
        for role, key in want.items():
            if role not in got and low.startswith(key):
                got[role] = x0
    if len(got) < len(want):
        return None
    got["_y"] = head_y
    return got


def _pdf_rows(page, A, head_y=None):
    """Слова страницы → строки таблицы: {колонка: [(y, x, слово)]}.

    Границы строки — середины между соседними датами: у записи есть текст и выше даты
    (имя контрагента), и ниже (ИНН, БИК, перенос назначения), поэтому «от даты до даты»
    рвало бы запись пополам."""
    # `head_y` — шапка ЭТОЙ страницы; на страницах-продолжениях её нет, и отсекать там нечего:
    # таблица идёт с самого верха листа (по шапке первой страницы срезалась вся вторая).
    body = [w for w in page if head_y is None or w[2] > head_y + 4]
    stop = min([w[2] for w in body
                if w[0] < A["debit"] and w[3].lower().startswith(PDF_FOOT)] or [1e9])
    body = [w for w in body if w[2] < stop]
    ys = sorted(w[2] for w in body
                if w[0] < A["doc"] - 4 and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", w[3]))
    if not ys:
        return []
    gap = min([b - a for a, b in zip(ys, ys[1:])] or [28.0])
    pad = min(14.0, gap / 2)
    out = []
    for i, y in enumerate(ys):
        top = (y + ys[i - 1]) / 2 if i else y - pad
        bot = (y + ys[i + 1]) / 2 if i + 1 < len(ys) else min(stop, y + pad)
        cols = {"date": [], "doc": [], "amt": [], "cp": [], "acct": [], "purpose": []}
        for x0, x1, wy, t in body:
            if not top <= wy < bot:
                continue
            if x0 >= A["purpose"] - 8:
                cols["purpose"].append((wy, x0, t))
            elif x0 >= A["acct"] - 8:
                cols["acct"].append((wy, x0, t))
            elif x0 >= A["cp"] - 8:
                cols["cp"].append((wy, x0, t))
            elif x0 >= A["debit"] - 20:
                cols["amt"].append((wy, x0, x1, t))   # суммы прижаты вправо, колонку даёт x конца
            elif x0 >= A["doc"] - 8:
                cols["doc"].append((wy, x0, t))
            else:
                cols["date"].append((wy, x0, t))
        out.append(cols)
    return out


def _pdf_join(items):
    """Слова колонки → строка в порядке чтения (сверху вниз, слева направо)."""
    return re.sub(r"\s+", " ", " ".join(t for it in sorted(items) for t in (it[-1],))).strip()


def parse_pdf(data, bank, org_inn, account=None):
    """Печатная выписка PDF → (записи, наш счёт).

    Наш счёт и ИНН берём из шапки выписки; ИНН сверяем с фирмой, в которую грузим, — иначе
    выписка Дисквэра молча легла бы в Цифровой квадрат. В конце сверяем разобранные обороты
    с «Итого» самой выписки: расхождение значит, что разбор потерял операцию, и лучше упасть,
    чем залить в БД неполный месяц."""
    pages = _pdf_pages(data)
    if not pages:
        raise ValueError("в PDF нет текста — похоже, это скан; нужна выгрузка из банка файлом")
    head = _pdf_text(pages[0])
    m_inn = re.search(r"ИНН:?\s*(\d{10,12})", head)
    if m_inn and org_inn and m_inn.group(1) != str(org_inn):
        raise ValueError(f"это выписка фирмы с ИНН {m_inn.group(1)}, "
                         f"а грузим в {org_inn} — выбери в списке слева ту же фирму")
    m_acc = re.search(r"[Сc]ч[ёе]т:?\s*(\d{20})", head)
    our = account or (m_acc.group(1) if m_acc else None)

    A, ops = None, []
    for pg in pages:
        a = _pdf_anchors(pg)               # на страницах-продолжениях шапки нет — колонки те же
        A = a or A
        if A is None:
            continue
        for c in _pdf_rows(pg, A, head_y=(a["_y"] if a else None)):
            day = _date(_pdf_join(c["date"]))
            amount, x_end = None, 0.0
            for wy, x0, x1, t in sorted(c["amt"]):
                v = _amount(t)
                if v is not None:
                    amount, x_end = v, x1
                    break
            if not day or amount is None:
                continue
            cp = _pdf_join(c["cp"])
            acct = _pdf_join(c["acct"])
            m = re.search(r"ИНН:?\s*(\d{10,12})", cp)
            cp_inn = m.group(1) if m else None
            cp_name = re.sub(r"\s+", " ", re.sub(r"ИНН:?\s*\d{10,12}", "", cp)).strip() or None
            m_ca = re.search(r"[СC]:?\s*(\d{20})", acct)
            m_bic = re.search(r"БИК:?\s*(\d{9})", acct)
            ops.append(_row(
                bank, our,
                # колонка «Дебет» кончается там, где начинается «Кредит»: сумма, прижатая
                # вправо левее этой границы, — расход
                "DEBIT" if x_end <= A["credit"] + 2 else "CREDIT",
                day, amount, _pdf_join(c["purpose"]) or None,
                doc_no=_pdf_join(c["doc"]) or None,
                cp_name=cp_name, cp_inn=cp_inn,
                cp_acc=(m_ca.group(1) if m_ca else None),
                cp_bic=(m_bic.group(1) if m_bic else None),
                raw={"pdf": {"date": day, "amount": amount, "cp": cp, "acct": acct}}))

    # контроль полноты: обороты выписки против разобранного
    for label, key, direction in (("Расходы", "debit", "DEBIT"), ("Поступления", "credit", "CREDIT")):
        m = re.search(label + r":?\s*([\d  ]+,\d{2})", head)
        if not m:
            continue
        want_sum = _amount(m.group(1))
        got_sum = round(sum(o["amount"] for o in ops if o["direction"] == direction), 2)
        if want_sum is not None and abs(want_sum - got_sum) > 0.01:
            raise ValueError(f"разбор не сошёлся с выпиской: {label.lower()} по шапке "
                             f"{want_sum:.2f} ₽, разобрано {got_sum:.2f} ₽ "
                             f"({len(ops)} операций) — формат изменился, грузить нельзя")
    return ops, our


# ── импорт ───────────────────────────────────────────────────────────────────
def parse(data, filename, bank, org_inn, account=None):
    """Файл любого поддержанного формата → (записи, наш счёт)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if str(filename).lower().endswith((".xlsx", ".xlsm")):
        return parse_table(data, filename, bank, org_inn, account)
    if str(filename).lower().endswith(".pdf") or data[:5] == b"%PDF-":
        return parse_pdf(data, bank, org_inn, account)
    text = decode(data)
    if is_1c(text):
        return parse_1c(text, bank, org_inn, account)
    return parse_table(data, filename, bank, org_inn, account)


def import_bytes(data, filename, bank, org_inn, account=None, since=None, dry=False):
    """Разобрать файл и положить в `bank_txn` (+ разметка по правилам).

    → stats из `bank_txn_store.store` плюс parsed / period / account / dry.
    dry=True — только разбор, в БД ничего не пишем (проверка формата перед загрузкой)."""
    ops, acc = parse(data, filename, bank, org_inn, account)
    days = sorted(o["operation_date"] for o in ops if o.get("operation_date"))
    info = {"parsed": len(ops), "period": [days[0], days[-1]] if days else None,
            "account": acc, "file": str(filename), "dry": bool(dry),
            "debit": round(sum(o["amount"] for o in ops if o["direction"] == "DEBIT"), 2),
            "credit": round(sum(o["amount"] for o in ops if o["direction"] == "CREDIT"), 2)}
    if dry or not ops:
        info.update({"seen": len(ops), "stored": 0, "dup": 0, "before_cutoff": 0, "ruled": 0})
        return info
    raws = [o.get("_raw") for o in ops]
    # `_raw` — служебный ключ этого модуля (сырьё строки файла), в bank_txn он уезжает
    # отдельной колонкой через raws; в самой записи он не нужен.
    clean = [{k: v for k, v in o.items() if k != "_raw"} | {"account": acc} for o in ops]
    stats = bank_txn_store.store(clean, bank, org_inn, account=acc,
                                 since=since or SINCE_DEFAULT, raws=raws)
    info.update(stats)
    return info


def import_file(path, bank, org_inn, account=None, since=None, dry=False):
    p = pathlib.Path(path)
    return import_bytes(p.read_bytes(), p.name, bank, org_inn, account, since, dry)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Импорт выписки из файла в bank_txn")
    ap.add_argument("file")
    ap.add_argument("--bank", default="ozon", help="метка банка в БД (ozon, tinkoff, …)")
    ap.add_argument("--org", required=True, help="ИНН нашей организации")
    ap.add_argument("--account", default=None, help="наш счёт, если его нет в файле")
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--dry", action="store_true", help="только разбор, без записи в БД")
    a = ap.parse_args(argv)
    r = import_file(a.file, a.bank, a.org, a.account, a.since, a.dry)
    print(f"{r['file']}: разобрано {r['parsed']}"
          + (f", период {r['period'][0]}…{r['period'][1]}" if r["period"] else "")
          + f", расход {r['debit']:.2f} ₽, приход {r['credit']:.2f} ₽")
    print(f"в БД новых {r['stored']}, уже было {r['dup']}, до отсечки {r['before_cutoff']}, "
          f"размечено правилом {r['ruled']}" + (" [DRY]" if r["dry"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
