# поток: ret
"""Текст сводки по возвратам: юрлицо → площадка → ПВЗ → позиции.

На каждое юрлицо (Цифровой / Дисквэр) — своё сообщение: ездят за возвратами разные люди.
"""
import html
import re
from collections import defaultdict
from datetime import datetime, timezone

from core import db
from returns_bot import orgs, pending
from returns_bot.sources.ozon import ACCOUNT_TITLE as OZON_TITLE

PLATFORM_ICON = {"ozon": "🔵", "yandex": "🟡", "wb": "🟣"}
PLATFORM_TITLE = {"ozon": "OZON", "yandex": "ЯНДЕКС", "wb": "WB"}
STAGE_ICON = {"pickup": "🟢", "attention": "🔴", "transit": "🔷"}

HEADS_SQL = """
SELECT platform, account, campaign, return_id, order_number, status_name, stage,
       pvz_name, pvz_address, where_now, barcode,
       deadline_at, storage_days, storage_sum, amount, first_seen
  FROM mp_returns
 WHERE gone_at IS NULL AND stage = ANY(%s)
 ORDER BY platform, account, pvz_address NULLS LAST, first_seen
"""

# Название товара. Ozon отдаёт его сам. Яндекс в возврате даёт только shopSku, поэтому:
#   1) наш же каталог Яндекса `raw_yandex_offer` (262 из 290 артикулов возвратов) — там имя
#      ровно то, что видит покупатель в карточке;
#   2) МойСклад по `external_code`/`article` — добивка для того, чего в каталоге нет;
#   3) не нашлось нигде (снятые с продажи артикулы вроде «3902del») — остаётся сам артикул.
# ms_product.external_code НЕ уникален → LATERAL LIMIT 1, иначе позиция размножится.
ITEMS_SQL = """
SELECT i.platform, i.account, i.return_id, i.offer_id, i.qty,
       CASE WHEN i.platform = 'yandex'
            THEN COALESCE(NULLIF(y.name, ''), NULLIF(p.name, ''), i.name)
            ELSE i.name END AS name
  FROM mp_return_items i
  LEFT JOIN LATERAL (
       SELECT payload->'offer'->>'name' AS name FROM raw_yandex_offer
        WHERE i.platform = 'yandex' AND offer_id = i.offer_id LIMIT 1) y ON true
  LEFT JOIN LATERAL (
       SELECT name FROM ms_product
        WHERE i.platform = 'yandex'
          AND (external_code = i.offer_id OR article = i.offer_id)
        ORDER BY archived, (external_code = i.offer_id) DESC LIMIT 1) p ON true
 WHERE (i.platform, i.account, i.return_id) IN (
       SELECT platform, account, return_id FROM mp_returns
        WHERE gone_at IS NULL AND stage = ANY(%s))
 ORDER BY i.platform, i.account, i.return_id, i.seq
"""

# Сегмент адреса, с которого начинается «улица и дом» — всё до него (страна, индекс, город,
# область, район) в сводке лишнее: ездим по городу, а не по стране.
STREET_RE = re.compile(
    r"(?:^|\s|\.)(?:ул|улица|просп|проспект|пр-?кт|пр-?д|проезд|ш|шоссе|пер|переулок|"
    r"б-?р|бульвар|наб|набережная|пл|площадь|туп|тупик|аллея|линия|тракт|мкр|микрорайон)"
    r"(?:\s|\.|$)", re.I)
# Мусорные имена точек: «Пункт выдачи заказов Яндекс Маркета» одинаково у всех — шума больше,
# чем пользы. Осмысленные коды складов Ozon («МОСКВА_4048») оставляем.
GENERIC_PVZ_RE = re.compile(r"^\s*(пункт выдачи|пвз\b|постамат)", re.I)


def _esc(v):
    return html.escape(str(v)) if v is not None else ""


def short_address(address):
    """«129075, Москва, улица Годовикова, 11 к.2» → «улица Годовикова, 11 к.2»."""
    if not address:
        return None
    parts = [p.strip() for p in str(address).split(",") if p.strip()]
    for i, p in enumerate(parts):
        if STREET_RE.search(p):
            return ", ".join(parts[i:])
    return ", ".join(parts[-2:]) if len(parts) > 2 else ", ".join(parts)


def pvz_label(name, address):
    """Подпись точки: короткий адрес плюс код склада, если он что-то значит."""
    addr = short_address(address)
    keep_name = name and not GENERIC_PVZ_RE.match(name)
    return " · ".join(x for x in (addr, keep_name and name or None) if x) or "точка не указана"


def account_title(platform, account, campaign):
    if platform == "ozon":
        return OZON_TITLE.get(account, account)
    return campaign or account


def _days(ts):
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).days


def _date(ts):
    return ts.strftime("%d.%m") if ts else None


def _money(v):
    return f"{float(v):,.0f}".replace(",", " ") if v is not None else None


def fetch(stages, org=None):
    heads = db.query(HEADS_SQL, (list(stages),))
    if org:
        heads = [h for h in heads if orgs.of(h["platform"], h["account"]) == org]
    keep = {(h["platform"], h["account"], h["return_id"]) for h in heads}
    items = defaultdict(list)
    for it in db.query(ITEMS_SQL, (list(stages),)):
        key = (it["platform"], it["account"], it["return_id"])
        if key in keep:
            items[key].append(it)
    return heads, items


def _item_line(items):
    """Состав возврата одной строкой."""
    if not items:
        return "состав не указан"
    parts = []
    for it in items[:4]:
        title = it.get("name") or it.get("offer_id") or "?"
        if len(title) > 48:
            title = title[:47] + "…"
        qty = it.get("qty") or 1
        parts.append(f"{title} ×{qty}")
    if len(items) > 4:
        parts.append(f"+ещё {len(items) - 4}")
    return "; ".join(parts)


def _tail(h):
    """Хвост строки: сроки, хранение, сумма."""
    bits = []
    d = _days(h.get("first_seen"))
    if d is not None and d > 0:
        bits.append(f"висит {d} дн")
    if h.get("storage_days"):
        bits.append(f"хранение {h['storage_days']} дн")
    if h.get("storage_sum"):
        bits.append(f"{_money(h['storage_sum'])} ₽ за хранение")
    if h.get("deadline_at"):
        bits.append(f"⏳ до {_date(h['deadline_at'])}")
    if h.get("amount"):
        bits.append(f"сумма {_money(h['amount'])} ₽")
    return " · ".join(bits)


def _return_block(h, items, show_where_now):
    num = h.get("order_number") or h.get("return_id")
    line = f"     • <code>{_esc(num)}</code> · {_esc(_item_line(items))}"
    extra = []
    if h.get("status_name"):
        extra.append(_esc(h["status_name"]))
    tail = _tail(h)
    if tail:
        extra.append(_esc(tail))
    if extra:
        line += "\n       " + " · ".join(extra)
    if show_where_now and h.get("where_now"):
        line += f"\n       сейчас: {_esc(h['where_now'])}"
    if not show_where_now and h.get("barcode"):
        line += f"\n       штрихкод: <code>{_esc(h['barcode'])}</code>"
    return line


def stage_section(stage, heads, items, with_title=True):
    rows = [h for h in heads if h["stage"] == stage]
    if not rows:
        return ""
    # заголовок стадии нужен, только когда стадий в сводке несколько
    out = [f"{STAGE_ICON.get(stage, '•')} <b>{pending.STAGE_TITLE[stage]}</b> ({len(rows)})"
           ] if with_title else []
    by_acc = defaultdict(list)
    for h in rows:
        by_acc[(h["platform"], h["account"], h.get("campaign"))].append(h)

    for (platform, account, campaign), acc_rows in sorted(by_acc.items()):
        # в письме по юрлицу подпись аккаунта, равная юрлицу, — повтор заголовка
        title = account_title(platform, account, campaign)
        if title == orgs.of(platform, account):
            title = None
        out.append(f"{'' if not out else chr(10)}{PLATFORM_ICON.get(platform, '⚪')} "
                   f"{PLATFORM_TITLE.get(platform, platform)}"
                   f"{' · ' + _esc(title) if title else ''} ({len(acc_rows)})")
        by_pvz = defaultdict(list)
        for h in acc_rows:
            by_pvz[(h.get("pvz_name"), h.get("pvz_address"))].append(h)
        for (name, address), pvz_rows in sorted(by_pvz.items(), key=lambda kv: str(kv[0])):
            out.append(f"  📍 {_esc(pvz_label(name, address))}")
            for h in pvz_rows:
                key = (h["platform"], h["account"], h["return_id"])
                out.append(_return_block(h, items.get(key, []), show_where_now=(stage == "transit")))
    return "\n".join(out)


def summary(stages=None, org=None):
    """HTML-текст сводки. По умолчанию — только то, что лежит и ждёт (pending.SHOW_STAGES)."""
    stages = tuple(stages or pending.SHOW_STAGES)
    heads, items = fetch(stages, org)
    counts = {s: sum(1 for h in heads if h["stage"] == s) for s in stages}
    today = datetime.now().strftime("%d.%m")
    who = f" · {org}" if org else ""

    if not heads:
        return f"📦 <b>Возвраты{who} на {today}</b>\n\nЗабирать нечего."

    pvz = {(h["platform"], h.get("pvz_address")) for h in heads if h.get("pvz_address")}
    n_pvz = len(pvz)
    word = "точке" if n_pvz % 10 == 1 and n_pvz % 100 != 11 else "точках"
    bits = [f"забрать {counts['pickup']} на {n_pvz} {word}"] if counts.get("pickup") else []
    if counts.get("attention"):
        bits.append(f"разобраться {counts['attention']}")
    if counts.get("transit"):
        bits.append(f"в пути {counts['transit']}")

    blocks = [f"📦 <b>Возвраты FBS{who} на {today}</b> — " + ", ".join(bits)]
    for stage in pending.STAGE_ORDER:
        if stage not in stages:
            continue
        section = stage_section(stage, heads, items, with_title=len(stages) > 1)
        if section:
            blocks.append(section)
    return "\n\n".join(blocks)


def summaries(stages=None):
    """[(юрлицо, текст), ...] — по сообщению на юрлицо, у которого есть что забрать."""
    stages = tuple(stages or pending.SHOW_STAGES)
    heads, _ = fetch(stages)
    present = {orgs.of(h["platform"], h["account"]) for h in heads}
    return [(o, summary(stages, org=o)) for o in orgs.order(present)]


def pvz_digest():
    """Короткий список точек: куда ехать и сколько там коробок."""
    heads, _ = fetch(pending.SHOW_STAGES)
    by = defaultdict(int)
    for h in heads:
        by[(orgs.of(h["platform"], h["account"]), h["platform"],
            h.get("pvz_name"), h.get("pvz_address"))] += 1
    if not by:
        return "Забирать нечего."
    out = ["📍 <b>Точки, где лежат возвраты</b>"]
    for (org, platform, name, address), n in sorted(by.items(), key=lambda kv: -kv[1]):
        out.append(f"{PLATFORM_ICON.get(platform, '⚪')} {_esc(org)} · "
                   f"{_esc(pvz_label(name, address))} — <b>{n}</b>")
    return "\n".join(out)
