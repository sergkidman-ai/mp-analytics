"""reports/feedback_drafts.py — генератор ЧЕРНОВИКОВ ответов на отзывы/вопросы.

Режим «только черновики»: ничего не постит. Берёт неотвеченные из raw_feedback, для каждого
собирает грунтовку (данные карточки), пишет draft в тон бренда и проставляет маршрут
(auto — безопасно к авто-постингу позже; review — на человека) и уверенность. Вопросы про
совместимость СВЕРЯЮТСЯ со списком моделей карточки: чего нет в карточке — не утверждаем.

Вывод: строки в raw_feedback (draft_*) + HTML-файл на вычитку (docs/feedback_drafts.html,
в git НЕ коммитим — там имена покупателей).

Запуск:  ./venv/bin/python reports/feedback_drafts.py
"""
import re
import sys
import html
import pathlib
from datetime import datetime, timezone

import requests
from psycopg2.extras import Json

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402
from collectors.ozon import _headers         # noqa: E402

OUT = BASE_DIR / "docs" / "feedback_drafts.html"
ATTR_URL = "https://api-seller.ozon.ru/v4/product/info/attributes"


# ─────────────────────────── грунтовка (данные карточки) ───────────────────────────
def _ozon_compat(account, skus):
    """sku → текст совместимости из карточки (имя + значения атрибутов + виджет-описание)."""
    if not skus:
        return {}
    rows = db.query("SELECT sku, product_id FROM ozon_dims WHERE account=%s AND sku = ANY(%s)",
                    (account, list(skus)))
    pid_by_sku = {r["sku"]: r["product_id"] for r in rows if r["product_id"]}
    if not pid_by_sku:
        return {}
    H = _headers(account)
    out = {}
    pids = [str(p) for p in pid_by_sku.values()]
    for i in range(0, len(pids), 100):
        r = requests.post(ATTR_URL, headers=H,
                          json={"filter": {"product_id": pids[i:i + 100], "visibility": "ALL"},
                                "limit": 100, "last_id": ""}, timeout=120)
        r.raise_for_status()
        for it in r.json().get("result", []):
            parts = [it.get("name") or ""]
            for a in it.get("attributes", []):
                parts += [v.get("value", "") for v in a.get("values", [])]
            out[str(it.get("sku") or it.get("id"))] = " ".join(parts).lower()
    # ключ вернём по sku
    res = {}
    for sku, pid in pid_by_sku.items():
        res[sku] = out.get(str(pid)) or out.get(sku) or ""
    # некоторые карточки индексируются по sku в ответе — добьём прямым совпадением
    return {s: (res.get(s) or "") for s in skus}


def _norm(s):
    return re.sub(r"[\s\-_]", "", str(s).lower())


MODEL_RX = re.compile(r"[A-Za-zА-Яа-я]{1,6}[- ]?\d{2,4}[A-Za-z0-9\-]*")


# ─────────────────────────── шаблоны отзывов ───────────────────────────
POS_WB = [
    "{name}, благодарим, что выбрали {product} в нашем магазине «Цифровой квадрат». "
    "Хорошего настроения 🌟 и приятных покупок 🛍",
    "{name}, спасибо за высокую оценку {product}! Рады, что всё подошло. "
    "Удачной печати и лёгких покупок 🌟🛍",
]
POS_OZ = [
    "Благодарим за заказ и высокую оценку! Рады, что товар подошёл. Хорошего настроения 🌟",
    "Спасибо за 5 звёзд! Приятно, что оправдали ожидания — будем рады видеть вас снова 🌟🛍",
    "Благодарим за отзыв! Лёгкой печати и хорошего настроения 🖨️🌟",
]
NEG_WB = ("Здравствуйте, {name}! Напишите нам, пожалуйста, в чат по QR-коду на упаковке "
          "о проблеме с {product} — обязательно разберёмся и поможем.")
NEG_OZ = ("Здравствуйте! Сожалеем, что товар вызвал нарекания. Напишите нам в чат — "
          "разберёмся и поможем с решением (возврат или замена). Спасибо, что сообщили.")
NEG_RETURN = ("Здравствуйте! Приносим извинения за сложности с возвратом — так быть не должно, "
              "мы всегда идём навстречу. Если вопрос ещё открыт, напишите нам в чат: оформим "
              "возврат или замену. Спасибо, что дали знать.")
NEG_ORIG = ("Здравствуйте! Уточним: это совместимый картридж (не оригинальный), информация есть "
            "в характеристиках карточки. Если качество печати не устроило — напишите нам, "
            "поможем с возвратом или заменой. Обязательно разберёмся.")


def _first_name(payload):
    n = (payload or {}).get("userName") or ""
    return n.strip().split()[0] if n.strip() else "Здравствуйте"


def _short_product(name):
    name = re.sub(r"^Картридж(и)?\s+", "", name or "").strip()
    return (name[:60] + "…") if len(name) > 61 else (name or "ваш товар")


def _pick(variants, ext_id):
    return variants[sum(map(ord, ext_id)) % len(variants)]


# ─────────────────────────── логика вопросов (проверка данных) ───────────────────────────
def _draft_question(body, product_name, compat):
    b = body.lower()
    g = {"compat_used": bool(compat)}
    # 1) заправлен / тонер
    if re.search(r"заправл|тонер|краск|чернил", b) and "?" in body or re.search(r"заправл|с тонером", b):
        if re.search(r"фотобарабан|барабан|драм|drum", (product_name or "").lower() + " " + compat):
            return ("Здравствуйте! Это фотобарабан — он не содержит тонера, тонер приобретается отдельно.",
                    0.7, {**g, "rule": "drum"})
        return ("Здравствуйте! Да, картридж поставляется заправленным, с тонером — готов к печати.",
                0.75, {**g, "rule": "filled"})
    # 2) оригинал?
    if re.search(r"оригинал", b):
        if "совместим" in compat:
            return ("Здравствуйте! Это совместимый картридж (не оригинальный) — он аналог, "
                    "обеспечивает качественную печать. Все детали в характеристиках карточки.",
                    0.65, {**g, "rule": "compatible"})
        return ("", 0.2, {**g, "rule": "orig_unknown"})  # на человека
    # 3) возврат
    if re.search(r"верн|возврат|обмен", b):
        return ("Здравствуйте! Да, если товар не подойдёт, его можно вернуть. Напишите нам — "
                "подскажем по возврату и поможем подобрать нужный вариант.", 0.6, {**g, "rule": "return"})
    # 4) совместимость с моделью принтера
    models = [m.group(0) for m in MODEL_RX.finditer(body) if re.search(r"\d", m.group(0))]
    models = [m for m in models if len(_norm(m)) >= 3]
    if models and compat:
        cn = _norm(compat)
        found = [m for m in models if _norm(m) in cn]
        missing = [m for m in models if _norm(m) not in cn]
        if found and not missing:
            return (f"Здравствуйте! Да, подходит для {', '.join(found)} — эта модель есть в списке "
                    f"совместимости карточки.", 0.8, {**g, "rule": "model_ok", "models": found})
        if missing:
            return (f"Здравствуйте! Уточните, пожалуйста, точную модель аппарата: в списке "
                    f"совместимости этого товара модели {', '.join(missing)} нет, есть похожие. "
                    f"Поможем подобрать верный вариант.", 0.4,
                    {**g, "rule": "model_mismatch", "missing": missing})
    # 5) прочее — на человека
    return ("", 0.15, {**g, "rule": "other"})


# ─────────────────────────── генерация ───────────────────────────
def _classify(r):
    if r["kind"] == "question":
        return "question"
    if (r["rating"] or 0) <= 3:
        return "negative"
    empty = not (r["body"] or "").strip() and not (r["pros"] or "").strip() and not (r["cons"] or "").strip()
    return "empty5" if empty else "positive"


def build():
    rows = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,payload
        FROM raw_feedback WHERE is_answered=false AND account IN ('wb_acc1','oz_acc1')""")
    # грунтовка для вопросов Ozon
    q_skus = {r["item_id"] for r in rows if r["kind"] == "question" and r["platform"] == "ozon" and r["item_id"]}
    compat = _ozon_compat("oz_acc1", q_skus) if q_skus else {}

    now = datetime.now(timezone.utc)
    updates = []
    for r in rows:
        cat = _classify(r)
        pl = r["payload"] or {}
        name = _first_name(pl) if r["platform"] == "wb" else "Здравствуйте"
        prod = _short_product(r["product_name"])
        route, conf, ground, draft = "review", 0.0, {}, ""
        if cat == "empty5":
            draft = (_pick(POS_WB, r["ext_id"]).format(name=name, product=prod) if r["platform"] == "wb"
                     else _pick(POS_OZ, r["ext_id"]))
            route, conf = "auto", 0.95
        elif cat == "positive":
            draft = (_pick(POS_WB, r["ext_id"]).format(name=name, product=prod) if r["platform"] == "wb"
                     else _pick(POS_OZ, r["ext_id"]))
            route, conf = "auto", 0.8
        elif cat == "negative":
            txt = ((r["body"] or "") + " " + (r["cons"] or "")).lower()
            if re.search(r"возврат|верн|спор|отказ", txt):
                draft = NEG_RETURN
            elif re.search(r"оригинал|галапринт|бренд|фирм", txt):
                draft = NEG_ORIG
            else:
                draft = NEG_WB.format(name=name, product=prod) if r["platform"] == "wb" else NEG_OZ
            route, conf = "review", 0.5
        else:  # question
            draft, conf, ground = _draft_question(r["body"] or "", r["product_name"],
                                                  compat.get(r["item_id"], "") if r["platform"] == "ozon" else "")
            route = "review"
        updates.append({**r, "cat": cat, "draft": draft, "route": route, "conf": conf, "ground": ground})

    # запись в БД
    for u in updates:
        db.execute("""UPDATE raw_feedback SET draft_text=%s, draft_route=%s, draft_confidence=%s,
            draft_category=%s, draft_grounding=%s, draft_at=%s
            WHERE platform=%s AND account=%s AND kind=%s AND ext_id=%s""",
            (u["draft"], u["route"], u["conf"], u["cat"], Json(u["ground"]), now,
             u["platform"], u["account"], u["kind"], u["ext_id"]))
    _write_html(updates, now)
    from collections import Counter
    c = Counter(u["cat"] for u in updates)
    auto = sum(1 for u in updates if u["route"] == "auto")
    print(f"Черновиков: {len(updates)} | {dict(c)} | auto={auto} review={len(updates)-auto}", flush=True)
    print(f"Файл: {OUT}", flush=True)
    return updates


# ─────────────────────────── HTML на вычитку ───────────────────────────
def _esc(s):
    return html.escape(str(s or ""))


def _write_html(updates, now):
    order = {"question": 0, "negative": 1, "positive": 2, "empty5": 3}
    updates = sorted(updates, key=lambda u: (u["platform"], order[u["cat"]]))
    css = """body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f5f7;color:#1a1a1a}
    @media(prefers-color-scheme:dark){body{background:#161719;color:#e6e6e6}.card{background:#1f2124!important;border-color:#2c2f33!important}}
    header{padding:20px 28px;background:#5b2fb3;color:#fff}h1{margin:0;font-size:19px}
    .sub{opacity:.85;font-size:13px;margin-top:4px}.wrap{max-width:1100px;margin:0 auto;padding:20px}
    .card{background:#fff;border:1px solid #e3e5e8;border-radius:10px;padding:14px 16px;margin:10px 0}
    .row{display:flex;gap:16px}.col{flex:1;min-width:0}
    .tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;margin-right:6px}
    .q{background:#fde9c8;color:#8a5a00}.neg{background:#fbd5d5;color:#a11}.pos{background:#d6f0d8;color:#1a6b2a}.e5{background:#e5e7eb;color:#555}
    .rev{background:#ffe1a8;color:#7a4b00}.auto{background:#cdeccf;color:#155}
    .meta{font-size:12px;color:#888;margin-bottom:6px}.orig{font-size:13px;color:#444;white-space:pre-wrap}
    @media(prefers-color-scheme:dark){.orig{color:#bbb}}
    .draft{font-size:14px;background:#f0edff;border-left:3px solid #5b2fb3;padding:8px 10px;border-radius:6px;white-space:pre-wrap}
    @media(prefers-color-scheme:dark){.draft{background:#241f38}}
    .empty{color:#b00;font-style:italic}.gr{font-size:11px;color:#999;margin-top:4px}
    h2{margin:26px 0 6px;font-size:15px;border-bottom:2px solid #ddd;padding-bottom:4px}"""
    cat_ru = {"question": ("q", "Вопрос"), "negative": ("neg", "Негатив"),
              "positive": ("pos", "Позитив+текст"), "empty5": ("e5", "Пустой 5★")}
    parts = [f"<header><h1>Черновики ответов — на вычитку</h1><div class='sub'>сгенерировано {now:%Y-%m-%d %H:%M} UTC · "
             "режим только черновики, ничего не опубликовано · WB Цифровой + Ozon Премиум</div></header><div class='wrap'>"]
    # сводка
    from collections import Counter
    cc = Counter((u["platform"], u["cat"]) for u in updates)
    parts.append("<div class='card'><b>Сводка:</b> " + " · ".join(
        f"{p}/{cat_ru[c][1]}: {n}" for (p, c), n in sorted(cc.items())) + "</div>")
    shown = Counter()
    cur = None
    for u in updates:
        head = f"{u['platform']} · {cat_ru[u['cat']][1]}"
        if u["cat"] == "empty5":
            shown[(u["platform"], u["cat"])] += 1
            if shown[(u["platform"], u["cat"])] > 10:
                continue
        if head != cur:
            cur = head
            total = cc[(u["platform"], u["cat"])]
            extra = " (показаны первые 10)" if u["cat"] == "empty5" and total > 10 else ""
            parts.append(f"<h2>{_esc(head)} — {total}{extra}</h2>")
        cls, ru = cat_ru[u["cat"]]
        rr = "auto" if u["route"] == "auto" else "rev"
        orig = (u["body"] or u["pros"] or u["cons"] or "").strip() or "(без текста)"
        rating = f"{u['rating']}★ " if u["rating"] else ""
        draft = _esc(u["draft"]) if u["draft"] else "<span class='empty'>— нет черновика, на человека —</span>"
        gr = ""
        if u["ground"]:
            gr = f"<div class='gr'>грунтовка: {_esc(u['ground'])}</div>"
        parts.append(
            f"<div class='card'><div class='meta'><span class='tag {cls}'>{rating}{ru}</span>"
            f"<span class='tag {rr}'>{'AUTO' if rr=='auto' else 'REVIEW'} · conf {u['conf']:.2f}</span>"
            f"{_esc(u['product_name'])}</div>"
            f"<div class='row'><div class='col'><div class='meta'>отзыв/вопрос:</div>"
            f"<div class='orig'>{_esc(orig)}</div></div>"
            f"<div class='col'><div class='meta'>черновик ответа:</div>"
            f"<div class='draft'>{draft}</div>{gr}</div></div></div>")
    parts.append("</div>")
    OUT.write_text(css.join(["<style>", "</style>"]) + "".join(parts), encoding="utf-8")


if __name__ == "__main__":
    build()
