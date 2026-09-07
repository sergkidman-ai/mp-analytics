# поток: rev
"""Регрессия движка ответов на РЕАЛЬНЫХ строках БД — прогон до и после каждого блока правок.

Правило Сергея (бриф 07.09.2026): регресс на «правильно пропущенных» = откат блока. Поэтому здесь
две половины, и вторая важнее первой: мало заблокировать плохое — нельзя заблокировать хорошее,
иначе правка просто перекладывает весь поток на оператора.

Ничего не пишет и в сеть не ходит: только читает raw_feedback и прогоняет детерминированные функции.
    ./venv/bin/python tools/rev_regression.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core import db                                     # noqa: E402
from reports import feedback_draft_run as dr            # noqa: E402
from reports import request_class as rc                 # noqa: E402
from reports import publish_gate as gate                # noqa: E402

TXT = "trim(concat_ws(' ', body, pros, cons))"
GAP = (r"(не работа|ошибк|не видит|полос|мажет|не подош|не совпад|брак|подтека|"
       r"засох|не печата|бледн|смаз|разбил|выкинул|вернул|шлак)")
ok = bad = 0


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print(f"  ✗ {name} {detail}"[:160])


# ── 1. 11 кейсов авто-дыры (docs/reports/rev_auto_gap_examples_2026-09-07.md) ────────────────
gap = db.query(f"""SELECT platform, account, ext_id, rating, body, pros, cons, item_id, payload
                   FROM raw_feedback WHERE kind='review' AND draft_route='auto' AND {TXT} ~* %s""",
               (GAP,))
for r in gap:
    check(f"gap {r['platform']}/{r['ext_id']}", dr.review_flagged(r), "маркер не распознан")
print(f"1. авто-дыра: {len(gap)} кейсов, помечены на оператора {len(gap) - bad}")

# ── 2. чистый позитив обязан остаться на auto ────────────────────────────────────────────────
b0 = bad
clean = db.query(f"""SELECT platform, account, ext_id, rating, body, pros, cons, item_id, payload,
                            product_name
                     FROM raw_feedback WHERE kind='review' AND rating=5 AND length({TXT}) BETWEEN 10 AND 300
                       AND {TXT} !~* %s ORDER BY ext_id LIMIT 40""", (GAP,))
for r in clean:
    if dr.review_flagged(r):                       # «?» и сожаление — законный повод, но их считаем
        continue                                   # отдельно: это не регресс, а расширение охвата
    route = dr.draft_review(r, None, "картридж")[2]
    check(f"clean {r['ext_id']}", route == "auto", f"стал {route}: {(r['body'] or '')[:60]}")
held = sum(1 for r in clean if dr.review_flagged(r))
print(f"2. чистый позитив: {len(clean)} строк, регрессов {bad - b0}, "
      f"снято по «?»/сожалению {held}")

# ── 3. класс обращения на реальном негативе ──────────────────────────────────────────────────
b0 = bad
neg = db.query(f"""SELECT kind, rating, body, pros, cons FROM raw_feedback
                   WHERE kind='review' AND rating<=2 AND length({TXT})>20 LIMIT 40""")
claims = sum(1 for r in neg
             if rc.classify(" ".join(filter(None, [r["body"], r["pros"], r["cons"]])),
                            kind="review", rating=r["rating"]) == "претензия")
check("класс претензии на 1-2★", claims == len(neg), f"{claims} из {len(neg)}")
print(f"3. класс обращения: 1–2★ с текстом {len(neg)}, распознано претензией {claims}")

# ── 4. вердикт публикации на опубликованных вопросах (не должен схлопнуться в «всё запретить») ─
b0 = bad
q = db.query("""SELECT kind, draft_text, draft_route, draft_confidence, draft_grounding, body
                FROM raw_feedback WHERE kind='question' AND posted_ok
                ORDER BY posted_at DESC LIMIT 60""")
allowed = sum(1 for r in q if gate.verdict(r)[0])
print(f"4. publish_gate на 60 опубликованных вопросах: разрешено {allowed}, запрещено {len(q) - allowed}")

print(f"\nИТОГО: проверок {ok + bad}, провалов {bad}")
sys.exit(1 if bad else 0)
