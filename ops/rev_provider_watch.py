# поток: rev
"""Free DeepSeek balance check and durable alert delivery, independent of processing."""
import json
import os
from pathlib import Path
import sys
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlparse
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core import db
from reports import batch_health, recovery_budget


def check_deepseek():
    token=os.getenv('DEEPSEEK_API_KEY')
    if not token:
        batch_health.record(SimpleNamespace(status_code=401,body={}), 'deepseek', source='balance')
        return {'available':False,'reason':'missing_key'}
    if urlparse(os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com/anthropic')).hostname!='api.deepseek.com':
        batch_health.observe('deepseek-balance','balance_unavailable',
            'Для настроенного посредника нет подтверждённого API баланса. Контролируются реальные отказы поиска.',
            severity='warning',blocks_work=False,source='balance')
        return {'available':None,'reason':'custom_provider'}
    try:
        response=requests.get('https://api.deepseek.com/user/balance',
            headers={'Authorization':'Bearer '+token},timeout=(8,20))
        if not response.ok:
            error=SimpleNamespace(status_code=response.status_code,body={})
            if response.status_code in (401,402,403):
                batch_health.record(error,'deepseek',source='balance')
            else:
                # A failed balance read cannot erase a proven search/billing failure.
                batch_health.observe('deepseek-balance','balance_check_failed',
                    'Не удалось проверить баланс DeepSeek: HTTP '+str(response.status_code)+'.',
                    blocks_work=False,source='balance',transient=True)
            return {'available':None,'http_status':response.status_code}
        data=response.json()
        if not isinstance(data.get('is_available'),bool):
            raise ValueError('Invalid balance response')
        usd=next((Decimal(x['total_balance']) for x in data.get('balance_infos',[])
                  if x.get('currency')=='USD'),None)
        if usd is not None and not usd.is_finite():
            raise ValueError('Invalid balance amount')
    except Exception as error:
        batch_health.observe('deepseek-balance','balance_check_failed',
            'Не удалось проверить остаток DeepSeek ('+type(error).__name__+'). Реальные запросы продолжаются.',
            severity='warning',blocks_work=False,source='balance',transient=True)
        return {'available':None,'reason':'balance_check_failed'}
    if not data['is_available']:
        batch_health.record(SimpleNamespace(status_code=402,body={}), 'deepseek', source='balance')
    else:
        batch_health.clear('deepseek',source='balance')
    low=Decimal(os.getenv('FEEDBACK_DEEPSEEK_BALANCE_WARN_USD','1'))
    critical=Decimal(os.getenv('FEEDBACK_DEEPSEEK_BALANCE_CRITICAL_USD','0.25'))
    if usd is not None and data['is_available'] and usd<low:
        batch_health.observe('deepseek-balance','low_balance',
            'Остаток DeepSeek: $'+str(usd)+'. Пополните баланс заранее: platform.deepseek.com → Top up.',
            severity='critical' if usd<critical else 'warning',blocks_work=False,source='balance')
    elif data['is_available']:
        batch_health.clear('deepseek-balance',source='balance')
    return {'available':data['is_available'],'usd':str(usd) if usd is not None else None}


def check_queue():
    row=db.query('''SELECT count(*) AS n FROM feedback_llm_queue q JOIN raw_feedback f
        USING(platform,account,kind,ext_id) WHERE NOT f.is_answered AND NOT coalesce(f.posted_ok,false)
        AND NOT f.skipped_old AND NOT f.skipped_no_api AND coalesce(f.recovery_active_unanswered,true)''')[0]
    stalled=db.query('''SELECT NOT EXISTS(SELECT 1 FROM feedback_llm_batch
        WHERE status='submitted' OR created_at>now()-interval '2 hours') AS stalled''')[0]['stalled']
    money=recovery_budget.status()
    available=Decimal(os.getenv('FEEDBACK_LLM_DAILY_TARGET_USD','1'))-money['spent_usd']-money['reserved_usd']
    failure=db.query("SELECT 1 FROM rev_batch_health WHERE provider='anthropic' AND blocks_work")
    if row['n'] and stalled and available>Decimal('0.10') and not failure:
        batch_health.observe('pipeline','queue_stalled',
            'Запросов в очереди: '+str(row['n'])+'. Более 2 часов нет новых Batch. Требуется проверка службы отзывов.',
            severity='critical',blocks_work=False,source='queue')
    else:
        batch_health.clear('pipeline')
    return {'queued_requests':row['n'],'stalled':bool(row['n'] and stalled and not failure and available>Decimal('0.10'))}


def main():
    os.umask(0o077)
    batch_health.setup()
    recovery_budget.setup()
    result={'deepseek':check_deepseek(),'queue':check_queue(),
            'alerts_delivered':batch_health.deliver_alerts()}
    Path('/root/rev-recovery/provider-watch-result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
