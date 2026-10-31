# поток: fin
"""Independent PRC delivery of loss and incomplete-shipment alerts to dragon."""
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import requests

from core import db
from core.ozon_expense_basis import BasisUnavailable
from customer_guard.domain import ACTIVE, non_positive_profit, utcnow
from customer_guard.notifications import message, transport
from customer_guard.rules import expense_basis
from customer_guard import store


def settings():
    recipient = os.getenv('TG_CUSTOMER_GUARD_LOSS_EXTRA_ID', '').strip()
    username = os.getenv('TG_CUSTOMER_GUARD_LOSS_EXTRA_USERNAME', '').strip().lstrip('@').lower()
    since = os.getenv('TG_CUSTOMER_GUARD_LOSS_EXTRA_SINCE', '').strip()
    if not recipient.isdecimal() or int(recipient) <= 0 or not username or not since:
        return None
    start = datetime.fromisoformat(since.replace('Z', '+00:00'))
    if start.tzinfo is None:
        raise ValueError('extra_loss_since_requires_timezone')
    return recipient, username, start.astimezone(timezone.utc)


@lru_cache(maxsize=2)
def checked_transport(recipient, username):
    root, _ = transport()  # Existing bot identity and Natalia's chat must still be valid.
    try:
        response = requests.get(root + '/getChat', params={'chat_id': recipient}, timeout=(5, 10))
        data = response.json()
    except Exception:
        raise RuntimeError('extra_recipient_identity_check_failed') from None
    chat = data.get('result') or {}
    if not (data.get('ok') and chat.get('type') == 'private'
            and str(chat.get('id')) == recipient
            and (chat.get('username') or '').lower() == username):
        raise RuntimeError('extra_recipient_identity_mismatch')
    return root


def _candidate_exists(recipient, since, now):
    return bool(db.query("""SELECT 1 FROM customer_guard_incidents i
      JOIN customer_guard_orders o ON o.id=i.order_id
      JOIN customer_guard_checks c ON c.id=o.latest_check_id
      CROSS JOIN customer_guard_runtime r
      WHERE i.kind IN ('non_positive_profit','partial_demand_coverage')
      AND i.delivery IN ('sent','uncertain')
      AND i.opened_at>=%s AND i.resolved_at IS NULL AND i.check_id=c.id
      AND c.generation=o.generation AND c.calculated_at>=r.activated_at
      AND r.enabled AND r.notifications_enabled AND o.mp_status=ANY(%s)
      AND (o.account='oz_acc2' OR o.account='wb_acc2' AND coalesce((r.settings->>'wb_enabled')::boolean,false)
        OR o.account IN ('oz_acc1','wb_acc1','ya_acc1')
          AND coalesce((r.settings->>'digital_enabled')::boolean,false))
      AND (i.kind='partial_demand_coverage' AND c.result->>'reason'='partial_demand_coverage'
        OR i.kind='non_positive_profit' AND o.ms_status=ANY(%s))
      AND o.observed_at>=%s AND o.ms_checked_at>=%s
      AND NOT EXISTS (SELECT 1 FROM customer_guard_extra_delivery e
        WHERE e.incident_id=i.id AND e.recipient=%s) LIMIT 1""",
      (since, list(ACTIVE), ['Подтвержден','Собирается','Неукомплектован'],
       now-timedelta(minutes=5), now-timedelta(minutes=5), recipient)))


def _claim(recipient, since, now):
    with store.transaction() as cur:
        cur.execute("""SELECT i.id,i.kind,o.posting_number,o.ms_status,o.account,o.platform,c.result
          FROM customer_guard_incidents i JOIN customer_guard_orders o ON o.id=i.order_id
          JOIN customer_guard_checks c ON c.id=o.latest_check_id
          JOIN customer_guard_jobs j ON j.order_id=o.id CROSS JOIN customer_guard_runtime r
          WHERE i.kind IN ('non_positive_profit','partial_demand_coverage')
          AND i.delivery IN ('sent','uncertain')
          AND i.opened_at>=%s AND i.resolved_at IS NULL AND i.check_id=c.id
          AND c.generation=o.generation AND c.calculated_at>=r.activated_at
          AND r.enabled AND r.notifications_enabled AND j.state='done' AND j.error IS NULL
          AND (o.account='oz_acc2' OR o.account='wb_acc2' AND coalesce((r.settings->>'wb_enabled')::boolean,false)
            OR o.account IN ('oz_acc1','wb_acc1','ya_acc1')
              AND coalesce((r.settings->>'digital_enabled')::boolean,false))
          AND o.mp_status=ANY(%s)
          AND (i.kind='partial_demand_coverage' AND c.result->>'reason'='partial_demand_coverage'
            OR i.kind='non_positive_profit' AND o.ms_status=ANY(%s))
          AND o.observed_at>=%s AND o.ms_checked_at>=%s
          AND NOT EXISTS (SELECT 1 FROM customer_guard_extra_delivery e
            WHERE e.incident_id=i.id AND e.recipient=%s)
          ORDER BY i.id LIMIT 1 FOR UPDATE OF i,o SKIP LOCKED""",
          (since,list(ACTIVE),['Подтвержден','Собирается','Неукомплектован'],
           now-timedelta(minutes=5),now-timedelta(minutes=5),recipient))
        row=cur.fetchone()
        if not row or not (row['kind']=='partial_demand_coverage'
                           and row['result'].get('reason')=='partial_demand_coverage'
                           or row['kind']=='non_positive_profit' and non_positive_profit(row['result'])):
            return None
        cur.execute("""INSERT INTO customer_guard_extra_delivery
          (incident_id,recipient,delivery,attempted_at) VALUES(%s,%s,'sending',%s)
          ON CONFLICT DO NOTHING RETURNING incident_id""",(row['id'],recipient,now))
        return dict(row) if cur.fetchone() else None


def _complete(incident_id,recipient,status,message_id=None,error=None):
    db.execute("""UPDATE customer_guard_extra_delivery SET delivery=%s,
      telegram_message_id=%s,delivery_error=%s WHERE incident_id=%s AND recipient=%s
      AND delivery='sending'""",(status,message_id,error,incident_id,recipient))
    return status


def recover_interrupted(now):
    db.execute("""UPDATE customer_guard_extra_delivery SET delivery='uncertain',
      delivery_error='sender_interrupted' WHERE delivery='sending' AND attempted_at<%s""",
      (now-timedelta(minutes=2),))


def send_pending(limit=3, now=None):
    config=settings()
    if not config:
        return {'sent':0,'failed':0,'uncertain':0,'suppressed':0}
    recipient,username,since=config
    now=now or utcnow()
    result={'sent':0,'failed':0,'uncertain':0,'suppressed':0}
    if not _candidate_exists(recipient,since,now):
        return result
    root=checked_transport(recipient,username)
    for _ in range(limit):
        row=_claim(recipient,since,now)
        if not row:
            break
        fresh=True
        if row['kind']=='non_positive_profit':
            try:
                fresh=expense_basis(row['account'])['version']==row['result']['rule_version']
            except BasisUnavailable:
                fresh=False
        valid=bool(db.query("""SELECT 1 FROM customer_guard_incidents i
          JOIN customer_guard_orders o ON o.id=i.order_id
          JOIN customer_guard_checks c ON c.id=o.latest_check_id CROSS JOIN customer_guard_runtime r
          WHERE i.id=%s AND i.resolved_at IS NULL AND i.check_id=c.id
          AND c.generation=o.generation AND r.enabled AND r.notifications_enabled
          AND (o.account='oz_acc2' OR o.account='wb_acc2' AND coalesce((r.settings->>'wb_enabled')::boolean,false)
            OR o.account IN ('oz_acc1','wb_acc1','ya_acc1')
              AND coalesce((r.settings->>'digital_enabled')::boolean,false))
          AND o.mp_status=ANY(%s)
          AND (i.kind='partial_demand_coverage' AND c.result->>'reason'='partial_demand_coverage'
            OR i.kind='non_positive_profit' AND o.ms_status=ANY(%s))
          AND o.observed_at>=%s AND o.ms_checked_at>=%s LIMIT 1""",
          (row['id'],list(ACTIVE),['Подтвержден','Собирается','Неукомплектован'],
           now-timedelta(minutes=5),now-timedelta(minutes=5))))
        if not (fresh and valid):
            status=_complete(row['id'],recipient,'suppressed',error='changed_before_send')
        else:
            try:
                response=requests.post(root+'/sendMessage',json={
                    'chat_id':recipient,'text':message(row),'disable_web_page_preview':True},
                    timeout=(5,15))
                data=response.json()
            except Exception:
                status=_complete(row['id'],recipient,'uncertain',error='transport_or_response_unknown')
            else:
                ack=(response.status_code==200 and data.get('ok')
                     and isinstance(data.get('result',{}).get('message_id'),int))
                if ack:
                    status=_complete(row['id'],recipient,'sent',data['result']['message_id'])
                else:
                    code=data.get('error_code')
                    rejected=(data.get('ok') is False and isinstance(code,int) and 400<=code<500)
                    status=_complete(row['id'],recipient,'failed' if rejected else 'uncertain',
                                     error='telegram_rejected' if rejected else 'telegram_result_unknown')
        result[status]+=1
    return result
