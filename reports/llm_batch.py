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
import uuid

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


def message_content(content):
    import json
    if isinstance(content,str) and content.startswith('REV_MULTIMODAL_V1:'):
        blocks=json.loads(content[len('REV_MULTIMODAL_V1:'):])
        if not isinstance(blocks,list):raise ValueError('Invalid multimodal content')
        return blocks
    return content


def _key(model, purpose, r, content, system_text=''):
    import json
    if not isinstance(content,str):content=json.dumps(content,ensure_ascii=False,sort_keys=True)
    h = hashlib.md5()
    for part in (model, purpose, str((r or {}).get("platform")), str((r or {}).get("account")),
                 str((r or {}).get("kind")), str((r or {}).get("ext_id")), content, system_text):
        h.update((part or "").encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def ask(model, system_text, content, max_tokens, r, purpose="draft"):
    """Готовый ответ модели на этот промпт — или LlmPending, если он ещё не пришёл.

    Порядок: готовый результат → уже стоит в очереди → поставить в очередь. Вызов API здесь
    не делается никогда: батч отправляет только `flush_if_due()`."""
    from reports.archive_question_policy import require_paid_model_allowed
    require_paid_model_allowed(r or {})
    k = _key(model, purpose, r, content, system_text)
    if isinstance(content,list):
        import json
        content='REV_MULTIMODAL_V1:'+json.dumps(content,ensure_ascii=False,sort_keys=True)
    got = db.query("SELECT raw FROM feedback_llm_result WHERE req_key=%s", (k,))
    if got and (got[0]["raw"] or "").strip():
        db.execute("UPDATE feedback_llm_result SET used_at=now() WHERE req_key=%s", (k,))
        return got[0]["raw"]
    # A changing catalogue/corpus must not leave multiple unpaid prompts for the same stage.
    # Reserved/uncertain submissions and paid results are deliberately preserved.
    db.execute('''DELETE FROM feedback_llm_queue WHERE platform=%s AND account=%s AND kind=%s
        AND ext_id=%s AND purpose=%s AND req_key<>%s AND batch_id IS NULL AND submission_key IS NULL''',
        ((r or {}).get('platform'), (r or {}).get('account'), (r or {}).get('kind'),
         str((r or {}).get('ext_id') or ''), purpose, k))
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
                        FROM feedback_llm_queue WHERE batch_id IS NULL AND submission_key IS NULL""")[0]
    if not row["n"]:
        return False, 0
    waited = db.query("SELECT EXTRACT(EPOCH FROM (now()-%s))/60 m", (row["oldest"],))[0]["m"]
    return (row["n"] >= BATCH_MIN or float(waited or 0) >= BATCH_MAX_WAIT_MIN), row["n"]


def prune_unpaid():
    """Retire obsolete unpaid prompts only; preserve all uncertain/paid submissions."""
    obsolete = db.execute('''DELETE FROM feedback_llm_queue q USING raw_feedback f
        WHERE (q.platform,q.account,q.kind,q.ext_id)=(f.platform,f.account,f.kind,f.ext_id)
        AND q.batch_id IS NULL AND q.submission_key IS NULL AND (
            f.is_answered OR coalesce(f.posted_ok,false) OR f.skipped_old OR f.skipped_no_api
            OR NOT coalesce(f.recovery_active_unanswered,true)
            OR f.draft_grounding->>'verification_method'='owner_approved_simple_review_20261111'
            OR EXISTS(SELECT 1 FROM feedback_moderation m
                WHERE (m.platform,m.account,m.kind,m.ext_id)=(f.platform,f.account,f.kind,f.ext_id)
                AND (m.final_text IS NOT NULL OR m.decided_at IS NOT NULL
                     OR m.state IN ('sent','skipped','edited','approved_blocked'))))''')
    superseded = db.execute('''DELETE FROM feedback_llm_queue q USING (
        SELECT req_key,row_number() OVER(PARTITION BY platform,account,kind,ext_id,purpose
            ORDER BY created_at DESC,req_key) AS rank FROM feedback_llm_queue
        WHERE batch_id IS NULL AND submission_key IS NULL) newer
        WHERE q.req_key=newer.req_key AND newer.rank>1''')
    return {'obsolete':obsolete,'superseded':superseded}


def flush_if_due(force=False, verbose=True):
    """Отправить накопившуюся очередь одним батчем на модель. → id батча или None."""
    from reports import batch_health
    prune_unpaid()
    if batch_health.blocked():
        return None
    if os.getenv('FEEDBACK_LLM_PIPELINE_READY', '0') != '1':
        if verbose:
            print('Batch blocked: credentials, search capability and budget pricing require validation', flush=True)
        return None
    db.execute('ALTER TABLE feedback_llm_queue ADD COLUMN IF NOT EXISTS submission_key text')
    due, n = _due()
    if not n or not (due or force):
        if verbose and n:
            print(f"  батч: в очереди {n} — рано (порог {BATCH_MIN} шт / {BATCH_MAX_WAIT_MIN} мин)",
                  flush=True)
        return None
    rows = db.query("""SELECT * FROM feedback_llm_queue WHERE batch_id IS NULL AND submission_key IS NULL
                        ORDER BY CASE WHEN purpose LIKE 'verify%%' THEN 0 WHEN purpose='image_evidence' THEN 1 ELSE 2 END,
                        created_at LIMIT %s""", (BATCH_CAP,))
    if not rows:
        return None
    from reports.archive_question_policy import is_archive_question
    rows=[row for row in rows if not is_archive_question(row)]
    if not rows:
        return None
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
                                messages=[{"role": "user", "content": message_content(r["content"])}])))
    from reports import recovery_budget
    from decimal import Decimal
    recovery_budget.setup()
    prices = llm_pricing.PRICING.get(model)
    if not prices:
        raise RuntimeError('Unpriced model blocked before batch submission')
    def upper(row):
        content=message_content(row['content'])
        if isinstance(content,list):
            # Images are locally bounded to 1000px. Conservative 4,000 tokens/image;
            # base64 bytes must not be counted as text tokens.
            size=len(row['system_text'].encode())+sum(len(x.get('text','').encode()) if x.get('type')=='text' else 4000 for x in content)
        else:size=len((row['system_text']+content).encode('utf-8'))
        return ((size+4096)*prices[0]*1.25
                +int(row['max_tokens'])*prices[1])/1e6*llm_pricing.BATCH_DISCOUNT
    spent = recovery_budget.status()
    available = Decimal(os.getenv('FEEDBACK_LLM_DAILY_TARGET_USD', '1')) - Decimal(str(spent['spent_usd'])) - Decimal(str(spent['reserved_usd']))
    # The $1 target is soft. A real waiting queue may use the already-authorized
    # $2 hard cap; never leave affordable verification jobs stuck until midnight.
    cap=min(Decimal('2'),Decimal(os.getenv('FEEDBACK_LLM_DAILY_CAP_USD','2')))
    hard_available=cap-Decimal(str(spent['spent_usd']))-Decimal(str(spent['reserved_usd']))
    if rows and not any(Decimal(str(upper(row)))<=available for row in rows):
        available=hard_available
    selected, upper_cost = [], 0.0
    for row in rows:
        if Decimal(str(upper_cost+upper(row))) <= available:
            selected.append(row)
            upper_cost += upper(row)
    if not selected:
        return None
    rows = selected
    reqs = [request for request in reqs if request['custom_id'] in {r['req_key'] for r in rows}]
    reservation = hashlib.sha256(('|'.join(sorted(r['req_key'] for r in rows)) + uuid.uuid4().hex).encode()).hexdigest()
    if not recovery_budget.reserve(reservation, 'anthropic', upper_cost):
        return None  # previous submission may have succeeded; reconcile instead of paying twice
    db.execute('UPDATE feedback_llm_queue SET submission_key=%s WHERE req_key=ANY(%s)',
               (reservation,[r['req_key'] for r in rows]))
    try:
        batch = client.messages.batches.create(requests=reqs)
    except Exception as error:
        detail = batch_health.record(error)
        if batch_health.rejected(error):
            # A definitive HTTP rejection created no job and incurred no inference cost.
            # Timeouts/5xx keep both the reservation and submission key for reconciliation.
            with db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute('UPDATE rev_llm_budget SET actual_usd=0 WHERE request_key=%s', (reservation,))
                    cur.execute('UPDATE feedback_llm_queue SET submission_key=NULL WHERE submission_key=%s AND batch_id IS NULL', (reservation,))
        if verbose:
            print(detail, flush=True)
        return None
    db.execute("INSERT INTO feedback_llm_batch (batch_id, model, n_req) VALUES (%s,%s,%s)",
               (batch.id, model, len(reqs)))
    db.execute("""UPDATE feedback_llm_queue SET batch_id=%s, submitted_at=now()
                   WHERE req_key = ANY(%s)""", (batch.id, [r["req_key"] for r in rows]))
    batch_health.clear()
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
            from reports import batch_health
            batch_health.record(e, source='batch_read')
            if verbose:
                print(f"  батч {b['batch_id']}: статус не получен ({type(e).__name__})", flush=True)
            continue
        if getattr(st, "processing_status", "") != "ended":
            if verbose:
                print(f"  батч {b['batch_id']}: ещё считается", flush=True)
            continue
        ok = err = tin = tout = 0
        actual_cost = 0.0
        saved_results = []
        try:
            results = list(client.messages.batches.results(b["batch_id"]))
        except Exception as error:
            from reports import batch_health
            batch_health.record(error, source='batch_read')
            continue
        for res in results:
            if getattr(res.result, "type", "") != "succeeded":
                err += 1
                from reports import batch_health
                from types import SimpleNamespace
                item_error = getattr(res.result,'error',None)
                if item_error:
                    error_type = getattr(item_error,'type','')
                    code = {'billing_error':402,'authentication_error':401,'rate_limit_error':429}.get(error_type,400)
                    batch_health.record(SimpleNamespace(status_code=code,
                        body={'type':error_type,'message':getattr(item_error,'message','')}), source='batch_result')
                continue
            msg = res.result.message
            u = getattr(msg, "usage", None)
            ti = getattr(u, "input_tokens", 0) or 0
            to = getattr(u, "output_tokens", 0) or 0
            tin += ti
            tout += to
            pin, pout = llm_pricing.PRICING[b['model']]
            actual_cost += ((ti + (getattr(u, 'cache_creation_input_tokens', 0) or 0) * 1.25
                             + (getattr(u, 'cache_read_input_tokens', 0) or 0) * 0.1) * pin
                            + to * pout) / 1e6 * llm_pricing.BATCH_DISCOUNT
            saved_results.append((res.custom_id, b['batch_id'], getattr(msg, 'model', b['model']),
                                  _text_of(msg), ti, to))
            ok += 1
        # Results, spend and queue removal commit together; a restart cannot count spend twice.
        with db.get_conn() as conn:
          with conn.cursor() as cur:
            cur.execute("SELECT status FROM feedback_llm_batch WHERE batch_id=%s FOR UPDATE", (b['batch_id'],))
            if cur.fetchone()[0] == 'ended':
                continue
            for saved in saved_results:
              cur.execute("""INSERT INTO feedback_llm_result
                            (req_key, batch_id, model, raw, tokens_in, tokens_out)
                          VALUES (%s,%s,%s,%s,%s,%s)
                          ON CONFLICT (req_key) DO UPDATE SET raw=EXCLUDED.raw,
                            tokens_in=EXCLUDED.tokens_in, tokens_out=EXCLUDED.tokens_out""",
                       saved)
            cur.execute("SELECT req_key,submission_key FROM feedback_llm_queue WHERE batch_id=%s ORDER BY req_key", (b['batch_id'],))
            queue_rows = cur.fetchall()
            keys = [row[0] for row in queue_rows]
            reservations = {row[1] for row in queue_rows if row[1]}
            if len(reservations) > 1:
                raise RuntimeError('Batch has multiple reservations; manual reconciliation required')
            reservation = next(iter(reservations), hashlib.sha256('|'.join(keys).encode()).hexdigest())
            if ok + err == b['n_req']:
                cur.execute('UPDATE rev_llm_budget SET actual_usd=%s WHERE request_key=%s',
                            (actual_cost, reservation))
            cur.execute("""INSERT INTO feedback_llm_cost_log(day,model,calls,tokens_in,tokens_out,cost_usd)
                VALUES(current_date,%s,%s,%s,%s,%s) ON CONFLICT(day,model) DO UPDATE SET
                calls=feedback_llm_cost_log.calls+EXCLUDED.calls,
                tokens_in=feedback_llm_cost_log.tokens_in+EXCLUDED.tokens_in,
                tokens_out=feedback_llm_cost_log.tokens_out+EXCLUDED.tokens_out,
                cost_usd=feedback_llm_cost_log.cost_usd+EXCLUDED.cost_usd""",
                (b['model']+llm_pricing.BATCH_SUFFIX,ok,tin,tout,actual_cost))
            cur.execute("""UPDATE feedback_llm_batch SET status='ended', ended_at=now(), n_ok=%s, n_err=%s
                       WHERE batch_id=%s""", (ok, err, b["batch_id"]))
            cur.execute("DELETE FROM feedback_llm_queue WHERE batch_id=%s", (b["batch_id"],))
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
