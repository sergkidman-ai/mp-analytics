# поток: rev
"""Short Batch/result/publication tick between the full collection cycles."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from feedback_bot import recovery_runtime,tg_moderation as bot
from feedback_bot.recovery_cycle import publication_phase, queue_cases, send_cards, report as status_report, send_status_if_due
from collectors.feedback_preflight import reconcile_unknown,notify_readback_reviews
from reports import recovery_budget, recovery_review, llm_batch


def main():
    os.umask(0o077)
    recovery_runtime.setup()
    recovery_budget.setup()
    saved_edits=recovery_runtime.refresh_edit_cards(bot.edit_text,bot._kb)
    from reports import recovery_archive_publish
    if os.getenv('FEEDBACK_LLM_PIPELINE_READY','0')!='1':
        print(json.dumps({'blocked':'LLM validation incomplete',
                          'archive_publication':recovery_archive_publish.tick()}))
        return
    report={'saved_edits_refreshed':saved_edits,'collected':llm_batch.collect(verbose=False)}
    report['review']=recovery_review.tick()
    from reports import recovery_history
    report['historical_analysis']=recovery_history.tick()
    report['submitted']=llm_batch.flush_if_due(force=True,verbose=False)
    report['reconciliation']=reconcile_unknown()
    report['publication_review_cards']=notify_readback_reviews()
    queue_cases()
    report['publication']=publication_phase()
    report['archive_publication']=recovery_archive_publish.tick()
    queue_cases()
    report['moderation_delivery']=send_cards()
    report['budget']=recovery_budget.status()
    report['status_notified']=send_status_if_due(status_report())
    Path('/root/rev-recovery/review-cycle-result.json').write_text(json.dumps(report,default=str,indent=2))
    print(json.dumps(report,default=str),flush=True)


if __name__=='__main__':main()
