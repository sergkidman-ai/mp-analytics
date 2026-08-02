# поток: ret
"""Текст сводки по возвратам: стадия → площадка/аккаунт → ПВЗ → позиции."""
import html
from collections import defaultdict
from datetime import datetime, timezone

from core import db
from returns_bot import pending
from returns_bot.sources.ozon import ACCOUNT_TITLE as OZON_TITLE

PLATFORM_ICON = {"ozon": "🔵", "yandex": "🟡", "wb": "🟣"}
PLATFORM_TITLE = {"ozon": "OZON", "yandex": "ЯНДЕКС", "wb": "WB"}
STAGE_ICON = {"pickup": "🟢", "attention": "🔴", "transit": "🔷"}

HEADS_SQL = """
SELECT platform, account, campaign, return_id, order_number, status_name, stage,
       pvz_name, pvz_address, pvz_instruction, where_now, barcode,
       deadline_at, storage_days, storage_sum, amount, first_seen
  FROM mp_returns
 WHERE gone_at IS NULL AND stage = ANY(%s)
 ORDER BY platform, account, pvz_address NULLS LAST, first_seen
"""

# Яндекс в возврате отдаёт только shopSku — человеческое имя добираем из МойСклада
# (правило 1: МС — источник правды по товарам). Ключ — `external_code` (совпадает у 1224 из
# 1371 shopSku, article даёт 25): он НЕ уникален, поэтому LATERAL LIMIT 1, чтобы не размножить
# позиции. Не нашлось — в сводке остаётся артикул площадки.
ITEMS_SQL = """
SELECT i.platform, i.account, i.return_id, i.offer_id, i.qty,
       CASE WHEN i.platform = 'yandex' THEN COALESCE(NULLIF(p.name, ''), i.name)
            ELSE i.name END AS name
  FROM mp_return_items i
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


def _esc(v):
    return html.escape(str(v)) if v is not None else ""


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


def fetch(stages):
    heads = db.query(HEADS_SQL, (list(stages),))
    items = defaultdict(list)
    for it in db.query(ITEMS_SQL, (list(stages),)):
        items[(it["platform"], it["account"], it["return_id"])].append(it)
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


def _return_block(h, items, with_pvz_line):
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
    if with_pvz_line and h.get("where_now"):
        line += f"\n       сейчас: {_esc(h['where_now'])}"
    if not with_pvz_line and h.get("barcode"):
        line += f"\n       штрихкод: <code>{_esc(h['barcode'])}</code>"
    return line


def stage_section(stage, heads, items):
    rows = [h for h in heads if h["stage"] == stage]
    if not rows:
        return ""
    out = [f"{STAGE_ICON.get(stage, '•')} <b>{pending.STAGE_TITLE[stage]}</b> ({len(rows)})"]
    by_acc = defaultdict(list)
    for h in rows:
        by_acc[(h["platform"], h["account"], h.get("campaign"))].append(h)

    for (platform, account, campaign), acc_rows in sorted(by_acc.items()):
        title = account_title(platform, account, campaign)
        out.append(f"\n{PLATFORM_ICON.get(platform, '⚪')} {PLATFORM_TITLE.get(platform, platform)}"
                   f" · {_esc(title)} ({len(acc_rows)})")
        by_pvz = defaultdict(list)
        for h in acc_rows:
            by_pvz[(h.get("pvz_name"), h.get("pvz_address"))].append(h)
        for (name, address), pvz_rows in sorted(by_pvz.items(), key=lambda kv: str(kv[0])):
            label = " · ".join(x for x in (name, address) if x) or "точка не указана"
            out.append(f"  📍 {_esc(label)}")
            instr = next((r["pvz_instruction"] for r in pvz_rows if r.get("pvz_instruction")), None)
            if instr:
                out.append(f"     ℹ️ {_esc(instr[:300])}")
            for h in pvz_rows:
                key = (h["platform"], h["account"], h["return_id"])
                out.append(_return_block(h, items.get(key, []), with_pvz_line=(stage == "transit")))
    return "\n".join(out)


def summary(stages=("pickup", "attention", "transit")):
    """Готовый HTML-текст сводки. Пустой список стадий → короткое «всё чисто»."""
    heads, items = fetch(stages)
    counts = {s: sum(1 for h in heads if h["stage"] == s) for s in stages}
    today = datetime.now().strftime("%d.%m")

    if not heads:
        return f"📦 <b>Возвраты на {today}</b>\n\nЗабирать нечего — висящих возвратов нет."

    pvz = {(h["platform"], h.get("pvz_address")) for h in heads
           if h["stage"] in ("pickup", "attention") and h.get("pvz_address")}
    head_line = (f"📦 <b>Возвраты FBS на {today}</b> — забрать {counts.get('pickup', 0)}"
                 f" на {len(pvz)} точках, разобраться {counts.get('attention', 0)},"
                 f" в пути {counts.get('transit', 0)}")

    blocks = [head_line]
    for stage in pending.STAGE_ORDER:
        if stage not in stages:
            continue
        section = stage_section(stage, heads, items)
        if section:
            blocks.append(section)
    return "\n\n".join(blocks)


def pvz_digest():
    """Короткий список точек: куда ехать и сколько там коробок."""
    heads, _ = fetch(("pickup", "attention"))
    by = defaultdict(int)
    for h in heads:
        by[(h["platform"], h.get("pvz_name"), h.get("pvz_address"))] += 1
    if not by:
        return "Забирать нечего."
    out = ["📍 <b>Точки, где лежат возвраты</b>"]
    for (platform, name, address), n in sorted(by.items(), key=lambda kv: -kv[1]):
        label = " · ".join(x for x in (name, address) if x) or "точка не указана"
        out.append(f"{PLATFORM_ICON.get(platform, '⚪')} {_esc(label)} — <b>{n}</b>")
    return "\n".join(out)
