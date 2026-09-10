# поток: rev
"""Отложенные вызовы модели пачкой (Anthropic Message Batches API, −50 % к цене).

Идея простая: движок ответов не ждёт модель. Дойдя до места, где нужен вызов, он кладёт готовый
промпт в очередь и бросает `LlmPending` — запись остаётся без черновика до следующего тика.
Пачка уходит, когда накопилось BATCH_MIN запросов ИЛИ самый старый ждёт дольше BATCH_MAX_WAIT;
ответы складываются в `feedback_llm_result`, и следующий прогон той же записи берёт готовый текст
вместо нового вызова. Для движка разница между «ответ из батча» и «ответ из API» невидима.

Почему так, а не «собрать всё и подождать в том же прогоне» (как делает старый `feedback_llm.run`):
батч отвечает до часа, а цикл отзывов идёт каждые 2 часа и не должен час висеть на `sleep`.

Ключ запроса `req_key` — md5 промпта вместе с записью. Он же защита от двойной оплаты: тот же
вопрос с тем же CARD_DATA второй раз в очередь не встанет, а на готовый ответ придёт попадание.

Ручной режим. Оператор, нажавший «перегенерировать», ждать час не может — вокруг такого вызова
ставится `synchronous()`, и модуль пропускает вызов мимо очереди (одиночный вызов, полная цена).

CLI:
    ./venv/bin/python -m reports.llm_batch --tick     # забрать готовое + отправить назревшее
    ./venv/bin/python -m reports.llm_batch --status   # что в очереди и в полёте
    ./venv/bin/python -m reports.llm_batch --flush    # отправить очередь немедленно
"""
import hashlib
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import db                                    # noqa: E402
from reports import llm_pricing                        # noqa: E402

# Порог отправки: 20 запросов (просьба Сергея 10.09.2026) или 30 минут ожидания самого старого.
BATCH_MIN = int(os.environ.get("FEEDBACK_BATCH_MIN", "20"))
BATCH_MAX_WAIT_MIN = int(os.environ.get("FEEDBACK_BATCH_WAIT_MIN", "30"))
# Anthropic принимает до 100 000 запросов в батче; наш поток на порядки меньше, ограничение —
# страховка от разового выброса (например, backfill за 90 дней).
BATCH_CAP = int(os.environ.get("FEEDBACK_BATCH_CAP", "500"))

_SYNC = False       # ручной режим: вызовы идут мимо очереди


class LlmPending(Exception):
    """Запрос ушёл в очередь батча, ответа ещё нет.

    Не ошибка: запись просто остаётся без черновика до следующего тика — то же состояние, в
    котором она оказывается при сбое модели, и пайплайн его уже умеет (`_gather` возьмёт снова).
    Ловящий обязан отличать это от настоящего сбоя и НЕ слать алерт."""


def enabled():
    return not _SYNC and os.environ.get("FEEDBACK_LLM_BATCH", "1") == "1"


class synchronous:
    """Контекст ручного вызова: внутри него батч выключен, модель отвечает сразу."""

    def __enter__(self):
        global _SYNC
        self.prev = _SYNC
        _SYNC = True
        return self

    def __exit__(self, *a):
        global _SYNC
        _SYNC = self.prev
        return False


def _key(model, purpose, r, content):
    h = hashlib.md5()
    for part in (model, purpose, str((r or {}).get("platform")), str((r or {}).get("account")),
                 str((r or {}).get("kind")), str((r or {}).get("ext_id")), content):
        h.update((part or "").encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def ask(model, system_text, content, max_tokens, r, purpose="draft"):
    """Готовый ответ модели на этот промпт — или LlmPending, если он ещё не пришёл.

    Порядок: готовый результат → уже стоит в очереди → поставить в очередь. Вызов API здесь
    не делается никогда: батч отправляет только `flush_if_due()`."""
    k = _key(model, purpose, r, content)
    got = db.query("SELECT raw FROM feedback_llm_result WHERE req_key=%s", (k,))
    if got and (got[0]["raw"] or "").strip():
        db.execute("UPDATE feedback_llm_result SET used_at=now() WHERE req_key=%s", (k,))
        return got[0]["raw"]
    db.execute("""INSERT INTO feedback_llm_queue
                    (req_key, platform, account, kind, ext_id, purpose, model, max_tokens,
                     system_text, content)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (req_key) DO NOTHING""",
               (k, (r or {}).get("platform"), (r or {}).get("account"), (r or {}).get("kind"),
                str((r or {}).get("ext_id") or ""), purpose, model, int(max_tokens),
                system_text, content))
    raise LlmPending(f"запрос в очереди батча ({purpose}, {model})")


def pending_count():
    return db.query("SELECT count(*) n FROM feedback_llm_queue WHERE batch_id IS NULL")[0]["n"]


def _due():
    """Пора ли отправлять: набралось BATCH_MIN или самый старый ждёт дольше BATCH_MAX_WAIT_MIN."""
    row = db.query("""SELECT count(*) n, min(created_at) oldest
                        FROM feedback_llm_queue WHERE batch_id IS NULL""")[0]
    if not row["n"]:
        return False, 0
    waited = db.query("SELECT EXTRACT(EPOCH FROM (now()-%s))/60 m", (row["oldest"],))[0]["m"]
    return (row["n"] >= BATCH_MIN or float(waited or 0) >= BATCH_MAX_WAIT_MIN), row["n"]


def flush_if_due(force=False, verbose=True):
    """Отправить накопившуюся очередь одним батчем на модель. → id батча или None."""
    due, n = _due()
    if not n or not (due or force):
        if verbose and n:
            print(f"  батч: в очереди {n} — рано (порог {BATCH_MIN} шт / {BATCH_MAX_WAIT_MIN} мин)",
                  flush=True)
        return None
    rows = db.query("""SELECT * FROM feedback_llm_queue WHERE batch_id IS NULL
                        ORDER BY created_at LIMIT %s""", (BATCH_CAP,))
    # Один батч = одна модель: у Anthropic модель задаётся в каждом запросе, но разбор стоимости
    # и статус батча читаются по модели, поэтому берём самую массовую и остальных ждём следующим.
    model = max({r["model"] for r in rows}, key=lambda m: sum(x["model"] == m for x in rows))
    rows = [r for r in rows if r["model"] == model]
    from anthropic.types.messages.batch_create_params import Request
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from reports.llm_client import client_for
    client = client_for(model)
    reqs = []
    for r in rows:
        sysparam = ([{"type": "text", "text": r["system_text"], "cache_control": {"type": "ephemeral"}}]
                    if not model.lower().startswith("deepseek") else r["system_text"])
        reqs.append(Request(custom_id=r["req_key"],
                            params=MessageCreateParamsNonStreaming(
                                model=model, max_tokens=int(r["max_tokens"]), system=sysparam,
                                messages=[{"role": "user", "content": r["content"]}])))
    batch = client.messages.batches.create(requests=reqs)
    db.execute("INSERT INTO feedback_llm_batch (batch_id, model, n_req) VALUES (%s,%s,%s)",
               (batch.id, model, len(reqs)))
    db.execute("""UPDATE feedback_llm_queue SET batch_id=%s, submitted_at=now()
                   WHERE req_key = ANY(%s)""", (batch.id, [r["req_key"] for r in rows]))
    if verbose:
        print(f"  батч отправлен: {batch.id} · {len(reqs)} запросов · {model} · −50 % к цене",
              flush=True)
    return batch.id


def collect(verbose=True):
    """Забрать результаты завершившихся батчей. → сколько ответов записано."""
    live = db.query("SELECT * FROM feedback_llm_batch WHERE status='submitted' ORDER BY created_at")
    if not live:
        return 0
    from reports.llm_client import client_for
    from reports.feedback_llm import _text_of
    got = 0
    for b in live:
        client = client_for(b["model"])
        try:
            st = client.messages.batches.retrieve(b["batch_id"])
        except Exception as e:
            if verbose:
                print(f"  батч {b['batch_id']}: статус не получен ({type(e).__name__})", flush=True)
            continue
        if getattr(st, "processing_status", "") != "ended":
            if verbose:
                print(f"  батч {b['batch_id']}: ещё считается", flush=True)
            continue
        ok = err = tin = tout = 0
        for res in client.messages.batches.results(b["batch_id"]):
            if getattr(res.result, "type", "") != "succeeded":
                err += 1
                continue
            msg = res.result.message
            u = getattr(msg, "usage", None)
            ti = getattr(u, "input_tokens", 0) or 0
            to = getattr(u, "output_tokens", 0) or 0
            tin += ti
            tout += to
            db.execute("""INSERT INTO feedback_llm_result
                            (req_key, batch_id, model, raw, tokens_in, tokens_out)
                          VALUES (%s,%s,%s,%s,%s,%s)
                          ON CONFLICT (req_key) DO UPDATE SET raw=EXCLUDED.raw,
                            tokens_in=EXCLUDED.tokens_in, tokens_out=EXCLUDED.tokens_out""",
                       (res.custom_id, b["batch_id"], b["model"], _text_of(msg), ti, to))
            ok += 1
        _log_cost(b["model"] + llm_pricing.BATCH_SUFFIX, ok, tin, tout)
        db.execute("""UPDATE feedback_llm_batch SET status='ended', ended_at=now(), n_ok=%s, n_err=%s
                       WHERE batch_id=%s""", (ok, err, b["batch_id"]))
        db.execute("DELETE FROM feedback_llm_queue WHERE batch_id=%s", (b["batch_id"],))
        got += ok
        if verbose:
            c = llm_pricing.cost(b["model"] + llm_pricing.BATCH_SUFFIX, tin, tout)
            print(f"  батч {b['batch_id']}: готово {ok}, ошибок {err}, ≈${c:.4f}", flush=True)
    return got


def _log_cost(model, calls, tin, tout):
    if not calls:
        return
    db.execute("""INSERT INTO feedback_llm_cost_log (day, model, calls, tokens_in, tokens_out, cost_usd)
                  VALUES (current_date, %s, %s, %s, %s, %s)
                  ON CONFLICT (day, model) DO UPDATE SET
                      calls = feedback_llm_cost_log.calls + EXCLUDED.calls,
                      tokens_in = feedback_llm_cost_log.tokens_in + EXCLUDED.tokens_in,
                      tokens_out = feedback_llm_cost_log.tokens_out + EXCLUDED.tokens_out,
                      cost_usd = feedback_llm_cost_log.cost_usd + EXCLUDED.cost_usd""",
               (model, calls, tin, tout, llm_pricing.cost(model, tin, tout)))


def tick(verbose=True):
    """Один такт: забрать готовое → отправить назревшее. Ответы разбирает уже движок ответов."""
    got = collect(verbose=verbose)
    bid = flush_if_due(verbose=verbose)
    return {"collected": got, "submitted": bid, "pending": pending_count()}


def status():
    q = db.query("""SELECT purpose, model, count(*) n, min(created_at) oldest
                      FROM feedback_llm_queue WHERE batch_id IS NULL GROUP BY 1,2""")
    print(f"В очереди (не отправлено): {sum(r['n'] for r in q)}")
    for r in q:
        print(f"  {r['purpose']:6} {r['model']:20} {r['n']:4} шт, старейший {str(r['oldest'])[:16]}")
    for b in db.query("""SELECT * FROM feedback_llm_batch ORDER BY created_at DESC LIMIT 5"""):
        print(f"  батч {b['batch_id']} {b['status']:9} {b['n_req']:4} зап., ok {b['n_ok']}, "
              f"err {b['n_err']}, {str(b['created_at'])[:16]}")
    unused = db.query("SELECT count(*) n FROM feedback_llm_result WHERE used_at IS NULL")[0]["n"]
    print(f"Готовых ответов, ещё не разобранных движком: {unused}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(str(pathlib.Path(__file__).resolve().parent.parent / ".env"))
    if "--status" in sys.argv:
        status()
    elif "--flush" in sys.argv:
        print(flush_if_due(force=True))
    else:
        print(tick())
