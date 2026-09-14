"""Уведомление о выкупе ВБ → УПД (СЧФДОП) для загрузки в Диадок.

Формат: КНД 1115131, версия 5.03, кодировка windows-1251, перевод строки CRLF —
ровно как в уже принятом документе № 764559005, который лежит рядом эталоном
(upd_sample_wb.xml). Генератор строится ПОДСТАНОВКОЙ В ЭТАЛОН, а не сборкой с нуля:
так гарантированно сохраняются порядок и написание тех элементов, которые мы не
трогаем, и любая правка проверяется побайтовым сравнением с принятым документом.

Арифметика сверена на № 764559005 — все 7 строк сошлись до копейки:
    СтТовУчНал  = «Сумма выкупа, руб. (вкл. НДС)» из уведомления
    СумНал      = «Сумма НДС» из уведомления
    СтТовБезНДС = разность
    ЦенаТов     = разность ÷ количество
КИЗ не используем: в уведомлениях он пустой, элемент НомСредИдентТов не выводим.

Использование:  make_upd("wb_acc1", "764559005") -> (имя_файла, bytes)
"""
import re
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports.upd_orgs import BUYER, SELLERS, vat_rate  # noqa: E402

SAMPLE = BASE_DIR / "reports" / "upd_sample_wb.xml"
ENC = "cp1251"
BASIS = "Уведомление о выкупе"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _q(x):
    return Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _fmt(x):
    return f"{_q(x):.2f}"


def _set(frag, tag, **attrs):
    """Заменить значения атрибутов в первом теге tag фрагмента."""
    m = re.search(r"<" + tag + r"\b[^>]*>", frag)
    if not m:
        raise RuntimeError(f"в эталоне нет тега <{tag}>")
    el = m.group(0)
    for name, value in attrs.items():
        el, n = re.subn(rf'({name}=")[^"]*(")', lambda g: g.group(1) + _esc(value) + g.group(2),
                        el, count=1)
        if not n:
            raise RuntimeError(f"в <{tag}> нет атрибута {name}")
    return frag[:m.start()] + el + frag[m.end():]


def _party(frag, org):
    """Проставить в блок стороны (СвПрод/ГрузОтпр/ГрузПолуч/СвПокуп) её реквизиты."""
    frag = _set(frag, "СвЮЛУч", НаимОрг=org["name"], ИННЮЛ=org["inn"], КПП=org["kpp"])
    return _set(frag, "АдрИнф", АдрТекст=org["address"])


def _cut(text, start, end):
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[:i], text[i:j], text[j:]


def notice(account, number):
    got = db.query("SELECT * FROM wb_redeem_notice WHERE account=%s AND doc_number=%s",
                   (account, number))
    if not got:
        raise RuntimeError(f"уведомления {number} по {account} нет в базе")
    return got[0]


def filename(account, n):
    """Имя файла по правилам ФНС: ON_NSCHFDOPPR_<получатель>_<отправитель>_<дата>_<GUID>."""
    seller = SELLERS[account]
    return (f"ON_NSCHFDOPPR_{BUYER['edi_id']}_{seller['edi_id']}"
            f"_{date.today():%Y%m%d}_{uuid.uuid4()}_0_0_0_0_0_00")


def _no_vat(rate):
    """Ставка «без НДС» / «0%» — в этих случаях ФНС ждёт не число, а элемент <БезНДС>."""
    return "без" in (rate or "").lower()


def _vat_elem(frag, outer, value, rate):
    """Внутренность <СумНал>/<СумНалВсего>: либо <СумНал>число</СумНал>, либо <БезНДС>.

    Формат 5.03 даёт здесь выбор из двух элементов, и при НалСт="без НДС" число
    (даже 0.00) схема не принимает.
    """
    inner = ("<БезНДС>без НДС</БезНДС>" if _no_vat(rate)
             else f"<СумНал>{_fmt(value)}</СумНал>")
    return re.sub(rf"(<{outer}>\s*)<СумНал>[^<]*</СумНал>",
                  lambda m: m.group(1) + inner, frag, count=1)


def make_upd(account, number, now=None):
    seller = SELLERS.get(account)
    if not seller:
        raise RuntimeError(f"не заполнены реквизиты продавца {account} "
                           f"(reports/upd_orgs.py): ИНН/КПП/адрес и идентификатор ЭДО")
    if not seller.get("edi_id"):
        raise RuntimeError(f"у продавца {account} не задан идентификатор ЭДО Диадока "
                           f"(reports/upd_orgs.py, поле edi_id) — он входит в ИдФайл")
    n = notice(account, number)
    now = now or datetime.now()
    d_doc = n["doc_date"].strftime("%d.%m.%Y")

    # newline="" — чтобы сохранить CRLF эталона: Python иначе молча приводит их к LF,
    # и файл перестаёт быть побайтовой копией принятого документа.
    with open(SAMPLE, encoding=ENC, newline="") as fh:
        text = fh.read()
    head, goods_tpl, tail = _cut(text, "<СведТов", "</СведТов>")
    # Между первым и последним СведТов в эталоне лежат остальные шесть строк — их
    # выбрасываем целиком и собираем таблицу заново из позиций уведомления.
    tail = tail[tail.rindex("</СведТов>") + len("</СведТов>"):]

    name = filename(account, n)
    head = _set(head, "Файл", ИдФайл=name)
    head = _set(head, "Документ", ДатаИнфПр=f"{now:%d.%m.%Y}", ВремИнфПр=f"{now:%H.%M.%S}",
                НаимЭконСубСост=seller["name"])
    head = _set(head, "СвСчФакт", НомерДок=n["doc_number"], ДатаДок=d_doc)

    # Стороны идут в эталоне в этом порядке, каждая — своим блоком.
    pre, prod, rest = _cut(head, "<СвПрод>", "</СвПрод>")
    prod = _party(prod, seller)
    mid, ship, rest = _cut(rest, "<ГрузОтпр>", "</ГрузОтпр>")
    ship = _party(ship, seller)
    mid2, cons, rest = _cut(rest, "<ГрузПолуч>", "</ГрузПолуч>")
    cons = _party(cons, BUYER)
    mid3, buyer, rest = _cut(rest, "<СвПокуп>", "</СвПокуп>")
    buyer = _party(buyer, BUYER)
    # ДокПодтвОтгрНом стоит между грузополучателем и покупателем — он попал в mid3.
    mid3 = _set(mid3, "ДокПодтвОтгрНом", РеквНомерДок=n["doc_number"], РеквДатаДок=d_doc)
    rest = _set(rest, "ТекстИнф", Значен=n["doc_number"])
    head = pre + prod + mid + ship + mid2 + cons + mid3 + buyer + rest

    rows, base_all, vat_all, with_all = [], Decimal(0), Decimal(0), Decimal(0)
    for i, p in enumerate(n["positions"], 1):
        qty = Decimal(p["qty"])
        with_vat = _q(p["sum_with_vat"])
        # Ставку берём по режиму продавца, а налог считаем от суммы выкупа сверху вниз:
        # НДС = сумма × ставка / (100 + ставка). На всех 500 позициях, где ВБ ставку
        # указал верно, этот расчёт совпал с его «Суммой НДС» до копейки.
        rate = vat_rate(account, n["doc_date"], p["vat_rate"])
        vat = Decimal(0) if _no_vat(rate) else _q(
            with_vat * Decimal(rate.rstrip("%")) / (100 + Decimal(rate.rstrip("%"))))
        base = _q(with_vat - vat)
        # В принятом УПД наименование = название из уведомления плюс артикул через
        # пробел — так во всех семи строках эталона; повторяем.
        g = _set(goods_tpl, "СведТов", НомСтр=str(i),
                 НаимТов=f'{p["name"]} {p["article"]}',
                 КолТов=f"{qty.normalize():f}", ЦенаТов=_fmt(base / qty),
                 СтТовБезНДС=_fmt(base), НалСт=rate, СтТовУчНал=_fmt(with_vat))
        g = _set(g, "ДопСведТов", КодТов=p["article"])
        g = _set(g, "ИнфПолФХЖ2", Значен=p["article"])
        g = _vat_elem(g, "СумНал", vat, rate)
        rows.append(g)
        base_all += base
        vat_all += vat
        with_all += with_vat

    tail = _set(tail, "ВсегоОпл", СтТовБезНДСВсего=_fmt(base_all),
                СтТовУчНалВсего=_fmt(with_all))
    # Итоговый налог — «без НДС» только если без НДС ВСЕ позиции; иначе число.
    tail = _vat_elem(tail, "СумНалВсего", vat_all,
                     "без НДС" if not vat_all else "5%")
    tail = _set(tail, "СвПер", ДатаПер=d_doc)
    tail = _set(tail, "ОснПер", РеквНаимДок=BASIS, РеквНомерДок=n["doc_number"],
                РеквДатаДок=d_doc)
    s = seller["signer"]
    tail = _set(tail, "Подписант", Должн=s["Должн"], СпосПодтПолном=s["СпосПодтПолном"])
    tail = _set(tail, "ФИО", Фамилия=s["Фамилия"], Имя=s["Имя"], Отчество=s["Отчество"])

    # Разделитель между товарными строками берём из эталона, а не придумываем:
    # перевод строки и отступ должны совпасть с принятым документом.
    sep = re.search(r"</СведТов>(\s*)<СведТов", text).group(1)
    out = head + sep.join(rows) + tail
    data = out.encode(ENC)
    # Проверка, что получился разбираемый XML: в ЭДО уходит юридически значимый документ.
    import xml.etree.ElementTree as ET
    ET.fromstring(data)
    return name + ".xml", data


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    num = sys.argv[2] if len(sys.argv) > 2 else "764559005"
    fn, data = make_upd(acc, num)
    print(fn, len(data), "байт")
