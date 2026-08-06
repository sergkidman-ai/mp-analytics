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
from returns_bot.sources.wb import ACCOUNT_TITLE as WB_TITLE

PLATFORM_ICON = {"ozon": "🔵", "yandex": "🟡", "wb": "🟣"}
PLATFORM_TITLE = {"ozon": "OZON", "yandex": "ЯНДЕКС", "wb": "WB"}
STAGE_ICON = {"pickup": "🟢", "attention": "🔴", "transit": "🔷"}

HEADS_SQL = """
SELECT platform, COALESCE(source, platform) AS source, account, campaign, return_id,
       order_number, return_type, scheme, status_name, stage,
       pvz_name, pvz_address, where_now, track_number,
       deadline_at, storage_days, storage_sum, amount, first_seen
  FROM mp_returns
 WHERE gone_at IS NULL AND stage = ANY(%s)
 ORDER BY platform, account, pvz_address NULLS LAST, first_seen
"""

# Название товара. Ozon отдаёт его сам. Остальные — добираем из наших же каталогов:
#   Яндекс (в возврате только shopSku):
#     1) каталог Яндекса `raw_yandex_offer` (262 из 290 артикулов возвратов) — имя ровно то,
#        что видит покупатель в карточке;
#     2) МойСклад по `external_code`/`article` — добивка;
#     3) не нашлось нигде (снятые с продажи артикулы вроде «3902del») — остаётся сам артикул.
#   WB (в отчёте только nmId и предмет «Картриджи для принтеров»): `wb_cards` по nm_id,
#     покрытие 38 из 38 живых возвратов на 02.08.2026.
# ms_product.external_code НЕ уникален → LATERAL LIMIT 1, иначе позиция размножится.
ITEMS_SQL = """
SELECT i.platform, i.account, i.return_id, i.offer_id, i.qty,
       CASE WHEN i.platform = 'yandex'
            THEN COALESCE(NULLIF(y.name, ''), NULLIF(p.name, ''), i.name)
            WHEN i.platform = 'wb'
            THEN COALESCE(NULLIF(w.title, ''), i.name)
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
  LEFT JOIN LATERAL (
       SELECT title FROM wb_cards
        WHERE i.platform = 'wb' AND nm_id::text = i.offer_id LIMIT 1) w ON true
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
# Город, приклеенный к улице без запятой: «г Москва», «МСК», «СПБ».
CITY_RE = re.compile(r"^\s*(?:г\.?|гор\.?|город|[А-ЯЁ]{2,4})\s+")


def _esc(v):
    return html.escape(str(v)) if v is not None else ""


def _strip_city(seg):
    """«МСК Улица Годовикова 11к5» → «Улица Годовикова 11к5».

    Режем только приклеенный слева город и только если без него сегмент всё ещё адрес:
    у постфиксных типов («Ленинградское шоссе») слева стоит НАЗВАНИЕ, его терять нельзя.
    """
    m = CITY_RE.match(seg)
    return seg[m.end():] if m and STREET_RE.search(seg[m.end():]) else seg


def short_address(address):
    """«129075, Москва, улица Годовикова, 11 к.2» → «улица Годовикова, 11 к.2».

    WB пишет адрес одной строкой без запятых («МСК Улица Годовикова 11к5») — отсюда _strip_city.
    """
    if not address:
        return None
    parts = [p.strip() for p in str(address).split(",") if p.strip()]
    for i, p in enumerate(parts):
        if STREET_RE.search(p):
            return ", ".join([_strip_city(p)] + parts[i + 1:])
    return ", ".join(parts[-2:]) if len(parts) > 2 else ", ".join(parts)


def pvz_label(name, address):
    """Подпись точки: короткий адрес плюс код склада, если он что-то значит."""
    addr = short_address(address)
    keep_name = name and not GENERIC_PVZ_RE.match(name)
    return " · ".join(x for x in (addr, keep_name and name or None) if x) or "точка не указана"


def account_title(platform, account, campaign):
    if platform == "ozon":
        return OZON_TITLE.get(account, account)
    if platform == "wb":
        return WB_TITLE.get(account, account)
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


def _item_lines(items):
    """Состав возврата: по товару на строку (решение Сергея 06.08.2026 — так читается)."""
    if not items:
        return ["состав не указан"]
    lines = []
    for it in items[:4]:
        title = it.get("name") or it.get("offer_id") or "?"
        if len(title) > 48:
            title = title[:47] + "…"
        qty = it.get("qty") or 1
        lines.append(f"{title} ×{qty}")
    if len(items) > 4:
        lines.append(f"+ещё {len(items) - 4}")
    return lines


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


def _number_label(h):
    """Номер позиции для склада (готовый HTML).

    У WB номер заказа (`orderId` → отгрузка в МойСкладе) есть только у возвратов «приехал по МП»,
    то есть отгруженных с НАШЕГО склада. Возвраты со склада ВБ (брак, неверное вложение, по
    инициативе продавца) заказа у нас не имеют — там ищем не номер, а помечаем ФБО, чтобы склад
    не искал несуществующий заказ (решение Сергея 04.08.2026).

    У вывоза со склада FBO номер заказа не бывает в принципе: приезжает наш же товар со стока.
    Печатаем номер заявки на вывоз — по нему строка ищется в ЛК Ozon (FBO → Вывоз и утилизация).
    """
    num = h.get("order_number")
    if h.get("source") == "ozon_removal":
        what = "вывоз со склада FBO" if h.get("scheme") == "FboRemoval" else "вывоз с поставки"
        return f"{what}, заявка <code>{_esc(num)}</code>" if num else what
    if num:
        return f"<code>{_esc(num)}</code>"
    if h["platform"] == "wb":
        rt = h.get("return_type")
        return "ФБО (со склада ВБ), номера заказа нет" + (f" · {_esc(rt)}" if rt else "")
    return f"<code>{_esc(h.get('return_id'))}</code>"


def _return_block(h, items, show_where_now):
    # Номер — тот, по которому позицию находят у нас (у WB это orderId = имя отгрузки в МС).
    # Штрихкод коробки не печатаем: для получения он не нужен (решение Сергея 04.08.2026).
    line = f"     • {_number_label(h)}"
    # Название товара — с новой строки: в одну строку с номером всё сливается (06.08.2026).
    for name in _item_lines(items):
        line += f"\n       {_esc(name)}"
    # Трек Почты России — единственный способ получить возврат Real-FBS: в отделении спрашивают
    # его, а не номер возврата. Отдельной строкой, чтобы не терялся в хвосте.
    if h.get("track_number"):
        line += f"\n       трек Почты: <code>{_esc(h['track_number'])}</code>"
    extra = []
    # Тип содержимого коробки вывоза («Доступно к продаже») не печатаем: складу он ничего
    # не даёт, а строку удлиняет (решение Сергея 06.08.2026).
    # В стадии «забрать» статус площадки («Готов к выдаче», «Принят в пункте выдачи»,
    # «В пункте выдачи») ничего не добавляет: раз позиция в списке — она лежит и ждёт.
    if h.get("status_name") and h["stage"] != "pickup":
        extra.append(_esc(h["status_name"]))
    tail = _tail(h)
    if tail:
        extra.append(_esc(tail))
    if extra:
        line += "\n       " + " · ".join(extra)
    if show_where_now and h.get("where_now"):
        line += f"\n       сейчас: {_esc(h['where_now'])}"
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
        # пустая строка между точками и между возвратами: сплошной блок глазом не разбирается
        for n_pvz, ((name, address), pvz_rows) in enumerate(
                sorted(by_pvz.items(), key=lambda kv: str(kv[0]))):
            if n_pvz:
                out.append("")
            out.append(f"  📍 {_esc(pvz_label(name, address))}")
            for n_ret, h in enumerate(pvz_rows):
                if n_ret:
                    out.append("")
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

    # у возвратов Почтой России адреса нет вовсе — точку считаем по имени способа получения
    pvz = {(h["platform"], h.get("pvz_address") or h.get("pvz_name"))
           for h in heads if h.get("pvz_address") or h.get("pvz_name")}
    n_pvz = len(pvz)
    word = "точке" if n_pvz % 10 == 1 and n_pvz % 100 != 11 else "точках"
    bits = [f"забрать {counts['pickup']} на {n_pvz} {word}"] if counts.get("pickup") else []
    if counts.get("attention"):
        bits.append(f"разобраться {counts['attention']}")
    if counts.get("transit"):
        bits.append(f"в пути {counts['transit']}")

    # не только FBS: с 06.08.2026 сюда же идут Real-FBS (Почтой России) и вывоз со склада FBO
    blocks = [f"📦 <b>Возвраты{who} на {today}</b> — " + ", ".join(bits)]
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
