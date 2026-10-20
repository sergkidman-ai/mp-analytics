# поток: fin
"""Private prc outbox. Ambiguous delivery is never automatically repeated."""
import os
from functools import lru_cache
from datetime import timedelta
from decimal import Decimal

import requests
from core import db
from customer_guard import store
from customer_guard.domain import ACTIVE, utcnow


@lru_cache(maxsize=1)
def transport():
    token = os.environ["TG_PRC_BOT_TOKEN"].strip()
    recipient = os.environ["TG_PRC_NATALIA_ID"].strip()
    root = "https://api.telegram.org/bot"+token
    try:
        bot = requests.get(root+"/getMe", timeout=(5,10)).json()
        chat = requests.get(root+"/getChat", params={"chat_id":recipient}, timeout=(5,10)).json()
    except Exception:
        raise RuntimeError("prc_identity_check_failed") from None
    if not (bot.get("ok") and bot["result"].get("username","").lower()=="ds_prc_bot"
            and chat.get("ok") and chat["result"].get("type")=="private"
            and chat["result"].get("username","").lower()=="pro_natalya"):
        raise RuntimeError("prc_identity_mismatch")
    return root, recipient


def message(row):
    if row["kind"] == "missing_ms":
        return (f"Заказ не найден в МС\nOzon · Дисквэр · {row['posting_number']}\n"
                "После получения Пультом прошло более 10 минут. Отправление активно в Ozon; "
                "проверка МС выполнена успешно, заказ не найден.\n"
                "Пульт → Склад → Заказы покупателей.")
    r = row["result"]
    rub = lambda key: f"{Decimal(r[key]):.2f} ₽"
    return (f"Ниже целевого пола\nOzon · Дисквэр · {row['posting_number']}\n"
            f"МС: {row['ms_status']}\nЦена: {rub('price')}; себестоимость: {rub('cost')} "
            f"({'факт отгрузки' if r['cost_kind']=='actual' else 'предварительная оценка'}).\n"
            f"Пол: {rub('floor')}; отклонение: {rub('gap')}.\n"
            f"Расчётный остаток: {rub('estimated_net')} — по средней доле расходов кабинета, "
            "не фактическая прибыль заказа.\n"+
            ("Соответствие состава не подтверждено; проверен общий итог.\n"
             if r.get("allocation")!="single_market_line" else "")+
            "Пульт → Склад → Заказы покупателей.")


def claim(order_id, recipient, now):
    with store.transaction() as cur:
        cur.execute("""SELECT i.id,i.kind,o.posting_number,o.ms_status,c.result
          FROM customer_guard_incidents i JOIN customer_guard_orders o ON o.id=i.order_id
          JOIN customer_guard_checks c ON c.id=o.latest_check_id
          JOIN customer_guard_jobs j ON j.order_id=o.id CROSS JOIN customer_guard_runtime r
          WHERE i.order_id=%s AND i.delivery='pending' AND i.resolved_at IS NULL
          AND r.enabled AND r.notifications_enabled AND c.calculated_at>=r.activated_at
          AND i.check_id=c.id AND c.generation=o.generation AND j.state='done' AND j.error IS NULL
          AND o.mp_status=ANY(%s) AND o.observed_at>=%s AND o.ms_checked_at>=%s
          AND o.ms_due_at<=%s AND i.kind=c.result->>'signal'
          AND (i.kind='missing_ms' AND o.ms_id IS NULL OR
               i.kind='below_floor' AND o.ms_status=ANY(%s)
               AND o.mp_status IN ('awaiting_packaging','awaiting_deliver'))
          FOR UPDATE OF i,o SKIP LOCKED LIMIT 1""",
          (order_id,list(ACTIVE),now-timedelta(seconds=60),now-timedelta(seconds=60),now,
           ["Подтвержден","Собирается","Неукомплектован"]))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("""UPDATE customer_guard_incidents SET delivery='sending',
          recipient=%s,attempted_at=%s,delivery_error=NULL WHERE id=%s""", (recipient,now,row["id"]))
        return dict(row)


def complete(incident_id, status, message_id=None, error=None):
    db.execute("""UPDATE customer_guard_incidents SET delivery=%s,
      telegram_message_id=%s,delivery_error=%s WHERE id=%s AND delivery='sending'""",
      (status,message_id,error,incident_id))
    return status


def send_current(order_id):
    # Do not call Telegram at all when there is nothing eligible to deliver.
    candidates=db.query("""SELECT 1 FROM customer_guard_incidents i
      CROSS JOIN customer_guard_runtime r WHERE i.order_id=%s AND i.delivery='pending'
      AND i.resolved_at IS NULL AND r.enabled AND r.notifications_enabled LIMIT 1""", (order_id,))
    if not candidates:
        return "none"
    root, recipient = transport()
    row = claim(order_id,recipient,utcnow())
    if not row:
        return "stale_or_disabled"
    # Stop/changed-order guard immediately before the external side effect.
    valid=db.query("""SELECT 1 FROM customer_guard_incidents i
      JOIN customer_guard_orders o ON o.id=i.order_id
      JOIN customer_guard_checks c ON c.id=o.latest_check_id CROSS JOIN customer_guard_runtime r
      WHERE i.id=%s AND i.delivery='sending' AND i.resolved_at IS NULL
      AND i.check_id=c.id AND c.generation=o.generation AND r.enabled AND r.notifications_enabled
      AND o.mp_status=ANY(%s)""",(row["id"],list(ACTIVE)))
    if not valid:
        return complete(row["id"],"suppressed",error="changed_before_send")
    try:
        response=requests.post(root+"/sendMessage",
            json={"chat_id":recipient,"text":message(row),"disable_web_page_preview":True},
            timeout=(5,15))
        data=response.json()
    except Exception:
        return complete(row["id"],"uncertain",error="transport_or_response_unknown")
    if response.status_code==200 and data.get("ok") and isinstance(data.get("result",{}).get("message_id"),int):
        return complete(row["id"],"sent",data["result"]["message_id"])
    # Only an explicit Bot API rejection establishes non-delivery.
    code=data.get("error_code")
    rejected = data.get("ok") is False and isinstance(code,int) and 400 <= code < 500
    return complete(row["id"],"failed" if rejected else "uncertain",
                    error="telegram_rejected" if rejected else "telegram_result_unknown")


def recover_interrupted(now):
    db.execute("""UPDATE customer_guard_incidents SET delivery='uncertain',
      delivery_error='sender_interrupted' WHERE delivery='sending' AND attempted_at<%s""",
      (now-timedelta(minutes=2),))
