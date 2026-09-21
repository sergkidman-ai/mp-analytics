# поток: rev
"""collectors/feedback_autosend.py — АВТООТПРАВКА позитив-шаблонов отзывов (draft_route='auto').

ЧТО СЮДА ПОПАДАЕТ (сверено с кодом 07.09.2026; прежняя формулировка «единственный источник auto —
пустые ★5 без текста» была неверна и стоила 5 опубликованных ответов не по адресу). Маршрут auto
ставит `draft_review()` в reports/feedback_draft_run.py, веток пять:
  1. негатив ≤3★ БЕЗ текста → общий хендофф по QR или шаблон типа жалобы, conf 0.9;
  2. негатив ≤3★ С текстом на «мёртвой» карточке (card_rating.verdict().alive=False) → DEAD_TEXT,
     conf 0.85 — правило Сергея 24.08.2026, карточку под убой ответом не спасают;
  3. 4★ БЕЗ текста → нейтральная благодарность, conf 0.9;
  4. ★5 БЕЗ текста → позитивная ротация, conf 0.95;
  5. ★5 С текстом → та же позитивная ротация, conf 0.8, — НО с 07.09.2026 только если в тексте нет
     маркера проблемы: `review_flagged()` (дефект / несовместимость / сожаление / «?») снимает
     отзыв с авто-маршрута независимо от рейтинга (гейт A0, reports/feedback_today.py).
Негатив ≤3★ с текстом на живой карточке, 4★ с текстом и все вопросы идут route='review' — через
модерацию в Telegram. Пейсинг: максимум FEEDBACK_AUTOSEND_BATCH (default 5) на канал за
цикл, пауза 3-5 сек между отправками внутри канала, свежие сначала (ORDER BY created_at DESC —
одновременно закрывает и «сначала свежие», и «затем старые от новых к старым», весь бэклог
капельно по вызовам). Порция своя на КАЖДУЮ категорию канала (свежие ≤FEEDBACK_BACKLOG_DAYS дней /
бэклог) — свежие отправляются первыми и не соревнуются со старьём за квоту. Отправка — через
collectors.feedback_send.post_answer (единый choke point: FEEDBACK_LIVE_SEND + FEEDBACK_LIVE_ACCOUNTS
применяются автоматически; дневной лимит канала FEEDBACK_BACKLOG_DAILY_CAP — ТОЛЬКО для бэклога,
apply_cap=True; свежие отзывы и всё, что одобрил оператор в Telegram, лимитом не режутся).
Перед раздачей слотов run() снимает с очереди неотвечаемые по правилам площадки (пустые Ozon-отзывы,
см. mark_no_text) и неотправляемые из-за закрытого API площадки (ответы на отзывы Ozon,
см. mark_no_api) — иначе порция канала сгорала бы на заведомо отбойных вызовах API.

Запуск:  ./venv/bin/python collectors/feedback_autosend.py
"""
import os
import sys
import time
import random
import pathlib
from collections import defaultdict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                          # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                                      # noqa: E402
from collectors import feedback_send as fs               # noqa: E402

BATCH = int(os.environ.get("FEEDBACK_AUTOSEND_BATCH", "5"))
# п.9 пакета 21.09.2026: первую неделю автопубликации вопросов — уведомление в чат о КАЖДОЙ отправке,
# чтобы оператор видел, что ушло без кнопки. Дата — включительно; потом строка просто перестаёт работать.
NOTIFY_Q_UNTIL = os.environ.get("FEEDBACK_AUTO_Q_NOTIFY_UNTIL", "2026-09-28")


def _notify_question(r, text):
    """Сбой уведомления отправку не отменяет и цикл не роняет — ответ уже на площадке."""
    if time.strftime("%Y-%m-%d") > NOTIFY_Q_UNTIL:
        return
    try:
        import html
        from feedback_bot import tg_moderation as tm
        g = r.get("draft_grounding") if isinstance(r.get("draft_grounding"), dict) else {}
        msg = ("🤖 <b>Автоответ на вопрос отправлен</b> (без кнопки, п.9)\n"
               f"{html.escape(r['platform'])}/{html.escape(r['account'])} · "
               f"{html.escape(str(g.get('request_class') or '—'))} · источник "
               f"{html.escape(str(g.get('source') or '—'))}\n"
               f"<b>Товар:</b> {html.escape((r.get('product_name') or '')[:90])}\n"
               f"<b>Вопрос:</b> {html.escape((r.get('body') or '')[:300])}\n"
               f"<b>Ответ:</b> {html.escape((text or '')[:500])}")
        for cid in tm.NOTIFY_IDS:
            tm.send(cid, msg)
    except Exception as e:
        _log(f"уведомление об автоответе не ушло: {type(e).__name__}: {e}")


def _log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] feedback_autosend: {msg}", flush=True)


# Ozon НЕ принимает ответ на отзыв без текста: /v1/review/comment/create → 400 «cannot comment on
# empty review». Это ограничение площадки, а не наша ошибка, поэтому такие отзывы снимаем с очереди
# флагом skipped_no_text (миграция 061), а не копим как неудачные отправки. У WB и Яндекса пустые ★5
# отвечаются нормально — фильтр строго по Озону.
_NO_TEXT_SQL = """platform='ozon' AND kind='review'
    AND coalesce(body,'')='' AND coalesce(pros,'')='' AND coalesce(cons,'')=''"""


# Отзывы Ozon: без подписки Premium Plus /v1/review/comment/create отдаёт 403 на КАЖДЫЙ ответ.
# Подписки не будет ни у одного юрлица (решение Сергея 24.08.2026) — значит канал закрыт целиком,
# и такие строки надо снимать с очереди, а не копить в «застрявших». Пути ответа два: появится
# работа через личный кабинет — снимаем флаг, вернётся подписка — снимаем флаг.
_NO_API_SQL = "platform='ozon' AND kind='review'"


def mark_no_api():
    """Пометить неотправляемые по закрытому API (ответы на отзывы Ozon). Возвращает число новых пометок.

    Как и mark_no_text, чинит уже сгоревшие: строки с posted_ok=false по этой причине переводим
    в skipped_no_api и обнуляем posted_at/posted_ok, чтобы они не висели в суточной сводке как
    ошибки отправки. Стоп-кран по ним снимается отдельно (DELETE из feedback_send_attempts) —
    иначе строка ⛔ «Застряло» осталась бы навсегда."""
    healed = db.execute(f"""UPDATE raw_feedback SET posted_at=NULL, posted_ok=NULL, skipped_no_api=true
                            WHERE {_NO_API_SQL} AND posted_ok=false""")
    # NOT skipped_no_text — у строки должна быть ОДНА причина парковки: пустой отзыв Озон не примет
    # и с подпиской, это ограничение правил площадки, а не закрытого канала.
    fresh = db.execute(f"""UPDATE raw_feedback SET skipped_no_api=true
                           WHERE {_NO_API_SQL} AND NOT skipped_no_api AND NOT skipped_no_text
                             AND posted_at IS NULL AND draft_route='auto'""")
    if healed or fresh:
        _log(f"неотправляемых (Ozon-отзывы, API закрыт): помечено {fresh}, снято с ошибок отправки {healed}")
        db.execute("""DELETE FROM feedback_send_attempts a USING raw_feedback f
                      WHERE a.platform=f.platform AND a.account=f.account AND a.kind=f.kind
                        AND a.ext_id=f.ext_id AND f.skipped_no_api""")
    return fresh


def mark_no_text():
    """Пометить неотвечаемые (пустые Ozon-отзывы) до раздачи слотов. Возвращает число новых пометок.

    Заодно чинит уже сгоревшие: строки, которые до появления флага получили posted_ok=false по этой
    самой причине, переводим в skipped_no_text и обнуляем posted_at/posted_ok — иначе они навсегда
    висят в суточной сводке как ошибки отправки. Повторно в очередь они не вернутся (фильтр ниже)."""
    healed = db.execute(f"""UPDATE raw_feedback SET posted_at=NULL, posted_ok=NULL, skipped_no_text=true
                            WHERE {_NO_TEXT_SQL} AND posted_ok=false""")
    fresh = db.execute(f"""UPDATE raw_feedback SET skipped_no_text=true
                           WHERE {_NO_TEXT_SQL} AND NOT skipped_no_text AND posted_at IS NULL""")
    if healed or fresh:
        _log(f"неотвечаемых (Ozon без текста): помечено {fresh}, снято с ошибок отправки {healed}")
    return fresh


def _candidates():
    """{(platform, account, bucket): [строки]} — bucket 'fresh' (отзыв не старше FEEDBACK_BACKLOG_DAYS)
    либо 'backlog'. Разделение нужно, потому что дневной лимит канала применяется ТОЛЬКО к бэклогу:
    свежие отзывы должны уходить сразу, не соревнуясь со старьём за квоту."""
    rows = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,payload,
        body,pros,cons,draft_grounding,draft_route,draft_confidence,
        draft_text,created_at FROM raw_feedback
        WHERE draft_route='auto' AND is_answered=false AND posted_at IS NULL
        AND draft_text IS NOT NULL AND NOT skipped_old AND NOT skipped_no_text
        AND NOT skipped_no_api
        ORDER BY created_at DESC""")
    buckets = defaultdict(list)
    for r in rows:
        bucket = "backlog" if fs.is_backlog(r) else "fresh"
        buckets[(r["platform"], r["account"], bucket)].append(r)
    return buckets


def run():
    mark_no_text()                                # слоты цикла — только на отвечаемые отзывы
    mark_no_api()                                 # и только на те, куда вообще есть путь ответа
    buckets = _candidates()
    total_sent, total_fail, capped = 0, 0, 0
    if not buckets:
        _log("нечего отправлять (пусто в draft_route=auto)")
        return {"sent": 0, "fail": 0, "channels": 0, "capped": 0}

    # свежие раньше бэклога: если канал упрётся в лимит, упрётся именно старьё
    for (plat, acc, bucket) in sorted(buckets, key=lambda k: (k[0], k[1], k[2] != "fresh")):
        rows = buckets[(plat, acc, bucket)]
        portion = rows[:BATCH]                     # анти-залповая порция — на каждую категорию своя
        cap_note = ""
        if bucket == "backlog":
            cap_note = f", лимит канала {fs.backlog_sent_today(plat, acc)}/{fs._backlog_cap()}"
        _log(f"{plat}/{acc} [{bucket}]: {len(rows)} в очереди, отправляю порцию {len(portion)}{cap_note}")
        for i, r in enumerate(portion):
            ok, detail = fs.post_answer(dict(r), r["draft_text"], apply_cap=(bucket == "backlog"))
            if ok and detail == "sent":
                total_sent += 1
                if r["kind"] == "question":
                    _notify_question(r, r["draft_text"])
            elif ok and detail == "dry-run:daily-cap":
                capped += 1
                break                              # лимит канала выбран — остальное старьё завтра
            elif not ok:
                total_fail += 1
                _log(f"  ОШИБКА {plat}/{acc} {r['kind']}={r['ext_id']}: {detail}")
            if i < len(portion) - 1:
                time.sleep(random.uniform(3, 5))

    chans = {(p, a) for p, a, _b in buckets}
    _log(f"итог: отправлено {total_sent}, ошибок {total_fail}, упёрлось в лимит {capped}, "
         f"каналов затронуто {len(chans)}")
    return {"sent": total_sent, "fail": total_fail, "channels": len(chans), "capped": capped}


if __name__ == "__main__":
    run()
