"""reports/questions_prototype.py — сбор ДОКАЗАТЕЛЬНОЙ БАЗЫ для прототипа движка вопросов.

Не отвечает сам (мозги = LLM/сессия). По набору реальных вопросов собирает:
  - класс (грубый, по интенту corpus)
  - факты карточки (источник №2, card_facts)
  - топ похожих прошлых Q&A (источник №1, feedback_corpus.retrieve; свой ответ исключён)
  - реальный наш исторический ответ (если есть) — для сверки качества
Выгружает JSON в scratchpad. Веб-поиск (источник №3) делает сессия точечно по пробелам.

Набор: весь неотвеченный backlog вопросов + стратифицированная по интентам выборка отвеченных.
"""
import sys
import json
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                    # noqa: E402
from reports.feedback_corpus import load_corpus, intent  # noqa: E402
from reports.card_facts import CardFacts               # noqa: E402

OUT = "/tmp/claude-0/-opt-mp-analytics/cbfef8e3-231d-43df-95de-edbb2cff5f9a/scratchpad/proto_evidence.json"


def pick_rows():
    rows = []
    # 1) весь неотвеченный backlog вопросов (реальная ценность)
    rows += db.query("""SELECT platform, account, kind, item_id, product_name, body, answer_text, rating
        FROM raw_feedback WHERE kind='question' AND is_answered=false
        AND length(trim(coalesce(body,'')))>8 ORDER BY platform, ext_id DESC""")
    # 2) стратифицированная выборка отвеченных: по 2 на (платформа, интент)
    ans = db.query("""SELECT platform, account, kind, item_id, product_name, body, answer_text, rating
        FROM raw_feedback WHERE kind='question' AND is_answered
        AND coalesce(trim(answer_text),'')<>'' AND length(trim(coalesce(body,'')))>12
        ORDER BY ext_id DESC""")
    seen = {}
    for r in ans:
        key = (r["platform"], intent(r["body"]))
        if seen.get(key, 0) >= 2:
            continue
        seen[key] = seen.get(key, 0) + 1
        rows.append(r)
    return rows


def main():
    corpus = load_corpus()
    cf = CardFacts()
    out = []
    for r in pick_rows():
        body = r["body"]
        facts = cf.for_ozon(r["item_id"]) if r["platform"] == "ozon" else cf.for_wb(r["item_id"])
        # источник №1: похожие прошлые Q&A, СВОЙ ответ исключаем
        sim = []
        for c in corpus.retrieve("question", body, r["product_name"], k=6):
            if c["src"].strip() == (body or "").strip():
                continue
            sim.append({"q": c["src"][:200], "a": c["answer"][:280],
                        "intent": c["intent"], "family": c["family"]})
            if len(sim) >= 4:
                break
        out.append({
            "platform": r["platform"], "sku": str(r["item_id"]),
            "product": r["product_name"], "question": body,
            "intent": intent(body),
            "is_answered": bool(r["answer_text"]),
            "real_answer": (r["answer_text"] or "").strip()[:400],
            "card_facts": facts,
            "similar_past": sim,
        })
    pathlib.Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Собрано вопросов: {len(out)} → {OUT}")
    # мини-сводка
    from collections import Counter
    print("по интентам:", dict(Counter(o["intent"] for o in out)))
    print("с фактами карточки:", sum(1 for o in out if o["card_facts"]))
    print("с похожими прошлыми Q&A:", sum(1 for o in out if o["similar_past"]))


if __name__ == "__main__":
    main()
