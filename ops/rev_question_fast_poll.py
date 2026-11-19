# поток: rev
"""Queue new questions for drafting. The review cycle delivers the finished card."""
import json
import os
import datetime as dt
from pathlib import Path

from collectors import feedback_recovery as collect
from collectors import yandex_questions as yq
from core import db
from feedback_bot import recovery_runtime as runtime


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
    return {'channels':channels,'new_question_cards':0}


if __name__=='__main__':
    os.umask(0o077)
    runtime.setup()
    result=run()
    Path('/root/rev-recovery/question-fast-poll-latest.json').write_text(json.dumps(result))
    print(json.dumps(result))
