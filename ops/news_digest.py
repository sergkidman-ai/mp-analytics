# поток: ev — выжимка по денежным новостям площадок: что меняется и что делать
"""Новость площадки -> короткая выжимка: что меняется, на сколько, с какого числа,
чем это бьёт по нашей марже и что сделать в первую очередь.

Зачем отдельный слой, а не «читать новость целиком». Новость Маркета о тарифах — это три
экрана текста, где цифра «×1,5 за возврат» спрятана в середине абзаца, а половина письма
касается FBO, которым мы не пользуемся. В дневнике нужна не новость, а её денежный смысл.

Границы:
- Считаем ТОЛЬКО по денежным правилам (`MONEY_RULES`): тарифы, комиссии, договор/правила,
  подписки, автоподключения. Пожары на складах в выжимке не нуждаются — там всё в заголовке.
- Сырьё не трогаем (правило 2 проекта): выжимка ложится в отдельную колонку `digest`,
  её можно пересчитать другой моделью, не ходя повторно в API площадки.
- **Расход живых денег.** Каждый вызов — платный запрос к модели, поэтому по умолчанию
  модуль считает и показывает смету (`--dry`), а тратит только по явному `--run`.
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

from core import db
from reports.llm_client import client_for, create_with_retry, LlmUnavailable

MODEL = os.environ.get("EV_DIGEST_MODEL", "deepseek-v4-pro")
# Прайс DeepSeek, $/млн токенов — только для сметы в чат, на биллинг не влияет.
USD_IN = float(os.environ.get("EV_DIGEST_USD_IN", "0.28"))
USD_OUT = float(os.environ.get("EV_DIGEST_USD_OUT", "0.42"))
USD_RUB = float(os.environ.get("USD_RUB", "92"))
MAX_OUT = 700

MONEY_RULES = ("тарифы", "комиссии", "договор", "подписки",
               "автоподключение услуги", "цены на услуги", "блокировки")

SYSTEM = """Ты аналитик продавца совместимых картриджей на маркетплейсах.
Контекст, который надо учитывать всегда:
- Площадки: Wildberries, Ozon, Яндекс.Маркет. Схема работы на ВСЕХ — ФБС (товар на нашем
  складе, площадка забирает под заказ). Услуги FBO/хранения на складе площадки нас не
  касаются — так и пиши, если новость только про них.
- Цель — не потерять маржу: целевая маржа 25% от нашей промо-цены.
Твоя задача: из новости площадки сделать выжимку для дневника управленческих событий.
Отвечай ТОЛЬКО валидным JSON без markdown, поля:
{"changes": "что меняется, 1-2 предложения, конкретно",
 "amount": "на сколько: было->стало, проценты, рубли. Если цифр в новости нет — 'цифр нет'",
 "from_date": "YYYY-MM-DD или null, если дата вступления не названа",
 "impact": "чем это бьёт по нам при ФБС: какая статья расходов растёт и на каких товарах",
 "action": "что сделать в первую очередь, чтобы не потерять в марже: 1-3 коротких пункта
            через '; '. Только выполнимое нами, без общих слов",
 "severity": "high | mid | low — high, если задевает деньги по всему ассортименту",
 "relevant": true/false — false, если новость нас не касается (только FBO, только другие
             категории, только реклама)}
Ничего не выдумывай: чего в новости нет — того нет."""


MONTHS_RU = ("январ", "феврал", "март", "апрел", "ма[йя]", "июн",
             "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
DATE_WORD = re.compile(r"\bс\s+(\d{1,2})\s+(" + "|".join(MONTHS_RU) +
                       r")\w*(?:\s+(\d{4}))?", re.I)
DATE_NUM = re.compile(r"\bс\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")
# Фраза с цифрой и единицей — то, ради чего новость вообще читают: «вырастет на 15%»,
# «×1,5 за возврат», «120 ₽ за литр». Берём предложение целиком, чтобы не потерять «на что».
AMOUNT = re.compile(r"[^.!?\n]*?\d[\d\s.,]*\s*(?:%|процентн\w*|₽|руб\w*|\bраза?\b|"
                    r"копе\w*)[^.!?\n]*[.!?]?", re.I)
# «п. 1.4 раздела „Карточка товара"» — это ссылка на пункт договора, а не цифра изменения.
CLAUSE = re.compile(r"\bп\.|\bпп\.|раздел|пункт|приложени", re.I)

# Что из новости бьёт по НАШИМ деньгам. Схема на всех площадках ФБС, поэтому хранение и
# приёмка на складе площадки нас не касаются — это в NOT_OURS, а не в статьях расходов.
COST_LINES = (
    (r"логистик|доставк|достав\w*\s+до\s+покупател|последн\w*\s+мил", "логистика",
     "пересчитать юнит-экономику там, где логистика — большая доля цены; проверить габариты "
     "карточек (за раздутый короб платим сами)"),
    (r"комисси", "комиссия площадки",
     "пересчитать маржу по категориям; товары с маржой ниже 25% — поднять цену или убрать из акций"),
    (r"возврат|невыкуп|отказ\w*\s+покупател", "возвраты и невыкуп",
     "смотреть долю невыкупа по SKU; на позициях с высоким невыкупом снять рекламу и промо"),
    (r"эквайринг|расчётно-кассов|рко", "эквайринг",
     "заложить новый процент в расчёт цены — он снимается с каждого заказа"),
    (r"реклам|продвижен|ставк\w*\s+за\s+показ|трафарет", "реклама",
     "сверить ДРР по кампаниям после даты изменения; убыточные кампании отключить"),
    (r"подписк|premium|премиум", "подписка площадки",
     "до даты изменения сравнить платёж за подписку с её выгодой и решить, продлевать ли"),
    (r"штраф|санкц|блокир|скрыл\w*\s+товар", "штрафы и блокировки",
     "проверить свои карточки на названное нарушение до даты вступления"),
    (r"утрат|кражи|подмен|недостач|претенз", "претензии по утраченному товару",
     "уложиться в новый срок подачи претензий — иначе потерянный товар не компенсируют"),
    (r"упаковк|доупаковк|маркиров", "упаковка и маркировка",
     "проверить, не подключена ли услуга автоматом, и отключить, если не нужна"),
)
NOT_OURS = re.compile(r"хранени|fbo|фбо|витрин\w*\s+склад|транзитн\w*\s+поставк", re.I)


def _from_date(text, today=None):
    """Дата вступления из текста новости. -> 'YYYY-MM-DD' или None."""
    today = today or datetime.now(timezone.utc).date()
    m = DATE_NUM.search(text)
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        except ValueError:
            return None
    m = DATE_WORD.search(text)
    if not m:
        return None
    month = next(i for i, p in enumerate(MONTHS_RU, 1) if re.match(p, m[2], re.I))
    year = int(m[3]) if m[3] else today.year
    try:
        d = date(year, month, int(m[1]))
    except ValueError:
        return None
    # Год не назван, а месяц уже прошёл — значит речь о следующем годе.
    if not m[3] and (d - today).days < -180:
        d = date(year + 1, month, int(m[1]))
    return d.isoformat()


def extract(row):
    """Выжимка ПРАВИЛАМИ, без модели и без денег. -> dict того же вида, что у LLM.

    Цифры и дату берём из текста как есть (ничего не считаем и не додумываем — новость
    источник), а «что сделать» — из таблицы статей расходов: какая наша статья растёт,
    то и проверяем. Слабое место честное: если площадка написала цифру словами или спрятала
    её в таблицу-картинку, в выжимке будет «цифр нет» — тогда открывать новость целиком.
    """
    text = f"{row['title']}. {row['body'] or ''}"
    # Статью расходов засчитываем, только если она названа в заголовке или в предложении
    # с цифрой. Иначе длинная новость об изменениях договора «задевает» сразу всё: слова
    # «логистика», «реклама», «возврат» там встречаются просто в перечне разделов.
    money_sent = [c for c in AMOUNT.findall(text) if not CLAUSE.search(c)]
    where = row["title"] + " " + " ".join(money_sent)
    lines, actions = [], []
    for pat, name, action in COST_LINES:
        if re.search(pat, where, re.I):
            lines.append(name)
            actions.append(action)
    ours = bool(lines) and not (NOT_OURS.search(text) and len(lines) < 2)

    # Из всех фраз с цифрами берём те, что про НАШИ статьи расходов: в одной новости площадки
    # мешаются скидки на хранение (нам не нужно) и рост логистики (нужно очень).
    cost_pat = "|".join(p for p, _, _ in COST_LINES)
    ranked = []
    for a in money_sent:
        a = re.sub(r"\s+", " ", a).strip()
        if not 12 < len(a) < 200 or a in [x[1] for x in ranked]:
            continue
        score = (2 if re.search(cost_pat, a, re.I) else 0) + \
                (0 if NOT_OURS.search(a) else 1) + \
                (1 if re.search(r"%|₽|руб|процентн", a, re.I) else 0)
        ranked.append((score, a))
    amounts = [a for _, a in sorted(ranked, key=lambda x: -x[0])[:3]]

    # Заголовок говорит «что», но не говорит «как»: добираем предложения с глаголами изменения.
    changed = [re.sub(r"\s+", " ", c).strip() for c in re.findall(
        r"[^.!?\n]*\b(?:обнов\w+|повыс\w+|подним\w+|снизи\w+|измен\w+|вырас\w+|"
        r"увелич\w+|уменьш\w+|унифицир\w+|введ\w+|отмен\w+)\b[^.!?\n]*[.!?]", text)]
    ttl = row["title"].lower().rstrip(".")
    changed = [c for c in changed if 20 < len(c) < 220 and ttl not in c.lower()
               and not CLAUSE.search(c)][:2]

    return {
        "engine": "rules",
        "changes": " ".join([row["title"] + "."] + changed),
        "amount": " ".join(amounts) if amounts else "цифр нет — смотреть новость целиком",
        "from_date": _from_date(text),
        "impact": ("задевает: " + ", ".join(lines[:3])) if lines
                  else "цифр по нашим статьям расходов в тексте нет — прочитать новость целиком",
        "action": "; ".join(actions[:2]) if actions
                  else "прочитать новость и решить, задевает ли она нас",
        "severity": "high" if len(lines) > 1 else ("mid" if lines else "low"),
        "relevant": ours,
    }


def render(d):
    """JSON выжимки -> текст для дневника."""
    if not d:
        return None
    when = d.get("from_date")
    if when:
        try:
            when = datetime.strptime(when, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            pass
    lines = [f"Что меняется: {d.get('changes', '—')}",
             f"На сколько: {d.get('amount', '—')}",
             f"С какого числа: {when or 'не названо'}",
             f"Как бьёт по нам (ФБС): {d.get('impact', '—')}"]
    lines.append(f"Что сделать: {d.get('action', '—')}")
    if not d.get("relevant", True):
        lines.append("Похоже, нас не касается (у нас везде ФБС) — оставлено для истории.")
    if d.get("engine") == "rules":
        lines.append("— выжимка собрана правилами по тексту новости, без модели.")
    return "\n".join(lines)


def _parse(text):
    """Ответ модели -> dict. Пустой ответ и мусор в выжимку не пускаем."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("модель вернула не JSON")
    d = json.loads(m.group(0))
    if not d.get("changes"):
        raise ValueError("в выжимке нет главного — что меняется")
    if d.get("from_date") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d["from_date"])):
        d["from_date"] = None
    return d


def pending(days=90, rules=MONEY_RULES, redo=False):
    """Новости, которым нужна выжимка (тревоги по денежным правилам без digest)."""
    cond = "" if redo else "AND digest IS NULL"
    return db.query(f"""
        SELECT platform, account, message_id, event_id, title, body, matched,
               created_at::date AS d
        FROM mp_notices
        WHERE importance = 'alert' AND matched = ANY(%s)
          AND created_at >= current_date - %s::int {cond}
        ORDER BY created_at DESC""", (list(rules), days))


def estimate(rows):
    """Смета прогона: токены и деньги. Считаем ДО траты, а не по факту."""
    chars = sum(len(r["title"] or "") + len(r["body"] or "") for r in rows)
    tok_in = int(chars / 3) + len(rows) * 450        # ~3 символа на токен + системный промпт
    tok_out = len(rows) * MAX_OUT // 2               # ответы короткие, до лимита не доходят
    usd = tok_in / 1e6 * USD_IN + tok_out / 1e6 * USD_OUT
    return {"новостей": len(rows), "вход_токенов": tok_in, "выход_токенов": tok_out,
            "usd": round(usd, 3), "руб": round(usd * USD_RUB, 2)}


def digest_one(row, client=None):
    """Одна новость -> dict выжимки. Бросает LlmUnavailable/ValueError, не глотает."""
    client = client or client_for(MODEL)
    news = f"Площадка: {row['platform']}\nДата: {row['d']}\nЗаголовок: {row['title']}\n\n{row['body']}"
    resp = create_with_retry(client, model=MODEL, max_tokens=MAX_OUT, system=SYSTEM,
                             messages=[{"role": "user", "content": news[:8000]}])
    return _parse("".join(b.text for b in resp.content if getattr(b, "text", None)))


def save(row, d):
    """Выжимка -> сырьё и, если новость уже в дневнике, в саму запись дневника."""
    db.execute("""UPDATE mp_notices SET digest = %s, digest_at = now()
                  WHERE platform=%s AND account=%s AND message_id=%s""",
               (json.dumps(d, ensure_ascii=False), row["platform"], row["account"],
                row["message_id"]))
    if row.get("event_id"):
        db.execute("UPDATE biz_events SET details = %s, expect = %s WHERE id = %s",
                   (render(d), (d.get("action") or None), row["event_id"]))


def run(days=90, limit=None, rules=MONEY_RULES, dry=True, redo=False, engine="rules"):
    """Прогон. engine='rules' — бесплатно, по тексту; 'llm' — платно, только по решению.

    dry=True у платного движка означает «показать смету и не тратить».
    """
    rows = pending(days=days, rules=rules, redo=redo)
    if limit:
        rows = rows[:limit]
    if engine == "rules":
        for r in rows:
            save(r, extract(r))
        print(f"выжимок по правилам: {len(rows)} (денег не потрачено)")
        return {"ok": len(rows), "engine": "rules"}
    est = estimate(rows)
    print("смета: " + " | ".join(f"{k} {v}" for k, v in est.items()))
    if dry or not rows:
        print("сухой прогон — к модели не ходили" if dry else "нечего считать")
        return est
    client, ok, bad = client_for(MODEL), 0, 0
    for r in rows:
        try:
            save(r, digest_one(r, client))
            ok += 1
        except (LlmUnavailable, ValueError, json.JSONDecodeError) as exc:
            bad += 1
            print(f"  пропуск [{r['platform']} {r['d']}] {r['title'][:50]}: "
                  f"{type(exc).__name__}: {str(exc)[:80]}")
    print(f"выжимок сделано: {ok}, пропущено: {bad}")
    return {"ok": ok, "bad": bad, **est}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Выжимка по денежным новостям площадок")
    ap.add_argument("--llm", action="store_true",
                    help="считать моделью вместо правил (ПЛАТНО, только по решению Сергея)")
    ap.add_argument("--run", action="store_true",
                    help="с --llm: реально потратить. Без него у модели только смета")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--limit", type=int, help="не больше N новостей за прогон")
    ap.add_argument("--redo", action="store_true", help="пересчитать и уже посчитанные")
    a = ap.parse_args(argv)
    run(days=a.days, limit=a.limit, dry=not a.run, redo=a.redo,
        engine="llm" if a.llm else "rules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
