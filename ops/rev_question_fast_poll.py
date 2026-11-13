# поток: rev
"""Collect recent marketplace questions and show a manual Rev card promptly.

The five-minute review cycle fills the same card with a verified draft later.
This path neither calls paid models nor publishes to buyers.
"""
import json
import os
import datetime as dt
from pathlib import Path

from collectors import feedback_recovery as collect
from collectors import yandex_questions as yq
from core import db
from feedback_bot import tg_moderation as bot, recovery_runtime as runtime
from reports import recovery_review as review, publish_gate


def run():
    collect.MAX_PAGES=1
    collect.KEEP_RECORDS=False
    channels=[]
    for account in ('wb_acc1','wb_acc2'):
        collect.stage(account+':question',lambda account=account:collect.collect_wb(account,'question'))
    for account in ('oz_acc1','oz_acc2'):
        collect.stage(account+':question',lambda account=account:collect.collect_ozon(account))
    def market_recent():
        key,business=yq._cfg()
        names,shops=yq._names(),yq._shop_names()
        today=dt.datetime.now(dt.timezone.utc).date()
        collect._collect_yandex_job('question',key,business,names,shops,
            {'dateFrom':(today-dt.timedelta(days=7)).isoformat(),'dateTo':today.isoformat()})
    collect.stage('ya_acc1:question',market_recent)
    channels=list(collect.report)
    # Only newly collected questions; full historic paging remains in the normal cycle.
    db.execute('''INSERT INTO feedback_moderation(platform,account,kind,ext_id)
        SELECT platform,account,kind,ext_id FROM raw_feedback
        WHERE kind='question' AND created_at>=now()-interval '24 hours'
          AND NOT is_answered AND NOT coalesce(posted_ok,false)
          AND NOT skipped_old AND NOT skipped_no_api
          AND coalesce(recovery_active_unanswered,true)
        ON CONFLICT(platform,account,kind,ext_id) DO NOTHING''')
    ids=db.query('''SELECT m.id FROM feedback_moderation m JOIN raw_feedback f
        USING(platform,account,kind,ext_id)
        WHERE m.kind='question' AND m.state='queued' AND m.tg_msg_id IS NULL
          AND m.final_text IS NULL AND m.decided_at IS NULL
          AND f.created_at>=now()-interval '24 hours'
          AND NOT f.is_answered AND NOT coalesce(f.posted_ok,false)
          AND NOT f.skipped_old AND NOT f.skipped_no_api
          AND coalesce(f.recovery_active_unanswered,true)
        ORDER BY f.created_at ASC LIMIT 20''')
    sent=0
    for item in ids:
        with runtime.card_lock(item['id']):
            row=review.load(item['id'])
            if not row or row['state']!='queued' or row.get('tg_msg_id') or row.get('decided_at'):
                continue
            row['id']=item['id']
            allowed,_,_=publish_gate.verdict_full(row)
            manual=publish_gate.operator_can_send(row)
            markup=bot._kb(row['id'],allow_send=allowed and manual,
                           allow_override=not allowed and manual)
            message=bot.send(bot.NOTIFY_IDS[0],bot._card(row),reply_markup=markup)
            if message:
                bot._set(row['id'],'carded',tg_chat_id=int(bot.NOTIFY_IDS[0]),
                         tg_msg_id=message,carded_at='now()')
                sent+=1
    return {'channels':channels,'new_question_cards':sent}


if __name__=='__main__':
    os.umask(0o077)
    runtime.setup()
    result=run()
    Path('/root/rev-recovery/question-fast-poll-latest.json').write_text(json.dumps(result))
    print(json.dumps(result))
