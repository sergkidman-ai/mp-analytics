# поток: fin
"""Idempotent subscription setup. Receiver must be live before this command can write."""
import argparse
import json
import os
import time
from pathlib import Path
from uuid import uuid4
import requests
from shipment_guard import config


def register():
    secret=os.environ['SHIPMENT_GUARD_WEBHOOK_SECRET']
    url='https://bi.metaverseworld.ru/hooks/ms-shipment/'+secret
    # Invalid payload checks TLS, secret and routing, without adding an event/job.
    try:
        probe=requests.post(url,params={'requestId':str(uuid4())},json={'events':[]},timeout=5)
        if probe.status_code!=400: raise RuntimeError('receiver_not_ready_'+str(probe.status_code))
        h={'Authorization':'Bearer '+os.environ['MOYSKLAD_TOKEN'],'Accept-Encoding':'gzip'}
        api=config.MS+'/entity/webhook'
        response=requests.get(api,headers=h,params={'limit':100},timeout=20)
        if response.status_code!=200: raise RuntimeError('subscription_list_unavailable')
        before=response.json()['rows'];created=[]
        for action in ('CREATE','UPDATE','DELETE'):
            matches=[x for x in before if x.get('url')==url and x.get('entityType')=='demand' and x.get('action')==action]
            if matches:
                if not matches[0].get('enabled'):raise RuntimeError('own_subscription_disabled')
                created.append({'id':matches[0]['id'],'action':action,'existing':True});continue
            body={'url':url,'entityType':'demand','action':action,'method':'POST','enabled':True}
            if action=='UPDATE':body['diffType']='FIELDS'
            time.sleep(1);response=requests.post(api,headers=h,json=body,timeout=20)
            if response.status_code not in (200,201):raise RuntimeError('subscription_create_failed_'+str(response.status_code))
            created.append({'id':response.json()['id'],'action':action,'existing':False})
        time.sleep(1);response=requests.get(api,headers=h,params={'limit':100},timeout=20)
        if response.status_code!=200:raise RuntimeError('subscription_verification_unavailable')
        after={x['id']:x for x in response.json()['rows']}
        for old in before:
            if any(old.get(k)!=after.get(old['id'],{}).get(k) for k in ('url','enabled','entityType','action')):
                raise RuntimeError('existing_subscription_changed')
        result={'created':created,'old_subscriptions_preserved':len(before)}
        print(json.dumps(result,ensure_ascii=False))
    except requests.RequestException:
        # requests exceptions contain the secret-bearing URL; never propagate them.
        raise RuntimeError('subscription_network_error') from None


def main():
    p=argparse.ArgumentParser();p.add_argument('--register',action='store_true',required=True);p.parse_args();register()
if __name__=='__main__':main()
