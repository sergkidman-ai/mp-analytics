#!/usr/bin/env python3
# поток: mkt
"""Close (stop -> status 7) the dormant CPM campaigns on acc2 approved by Sergey 18.09.
Guards per campaign: status 9, payment cpm, budget total 0, zero spend in 28 days. Otherwise skip."""
import sys, json, time, requests, collections
sys.path.insert(0, "/opt/mp-analytics")
from core import db
from ops.wb_ad_enroll import WB_ADS_HOST, _token, composition
from ops.wb_ad_campaign_new import campaign_budget, note

APPLY = "--apply" in sys.argv
ACC = "wb_acc2"
comp = composition(ACC)
cpm = sorted(a for a, c in comp.items() if c["payment"] == "cpm" and c["status"] == 9)
cpc_members = set().union(*(c["nms"] for c in comp.values() if c["payment"] == "cpc"))
only_cpm = set().union(*(comp[a]["nms"] for a in cpm)) - cpc_members
sold = db.query("""select count(distinct article) n, coalesce(sum(qty),0) q from sales
                    where platform='wb' and account=%s and period_from>='2026-06-01' and qty>0
                      and article = any(%s)""", (ACC, [str(n) for n in only_cpm]))[0]
print(f"cpm status 9: {len(cpm)}; positions only in these (not in any cpc): {len(only_cpm)}, "
      f"sold since 01.06: {sold['n']} positions / {sold['q']} pcs")
H = {"Authorization": _token(ACC)}
done = 0
for aid in cpm:
    sp = db.query("select coalesce(sum(spend),0) s from wb_ad_nm_daily where account=%s and advert_id=%s "
                  "and dt>current_date-28", (ACC, aid))[0]["s"]
    bud = None
    for _ in range(5):
        code, txt = campaign_budget(ACC, aid)
        try:
            bud = json.loads(txt).get("total"); break
        except Exception:
            time.sleep(4)
    if bud != 0 or float(sp) != 0:
        print(f"  {aid}: SKIP budget {bud} spend28 {sp}"); continue
    if not APPLY:
        print(f"  {aid}: would stop ({len(comp[aid]['nms'])} positions)"); continue
    r = requests.get(WB_ADS_HOST + "/adv/v0/stop", params={"id": aid}, headers=H, timeout=30)
    note(ACC, "stop", {"id": aid}, r.status_code, r.text)
    print(f"  {aid}: stop HTTP {r.status_code} {r.text[:80]}")
    done += r.status_code == 200
    time.sleep(1.2)
if APPLY:
    time.sleep(3)
    after = composition(ACC)
    still = [a for a in cpm if a in after]
    dup = collections.Counter(n for c in after.values() for n in c["nms"])
    print(f"stopped OK {done}; still active/paused among them: {still}; "
          f"positions in 2+ campaigns now: {sum(1 for v in dup.values() if v > 1)}")
