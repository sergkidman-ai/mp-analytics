import os, sys, json, gzip, urllib.request, urllib.parse
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv
load_dotenv("/opt/mp-analytics/.env")
MS = "https://api.moysklad.ru/api/remap/1.2"
TOK = os.getenv("MOYSKLAD_TOKEN")
def get(path):
    url = MS + path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOK}",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            d = gzip.decompress(d)
        return json.loads(d)
if __name__ == "__main__":
    # sanity + orgs
    orgs = get("/entity/organization")
    print("ORG_COUNT", orgs.get("meta",{}).get("size"))
    for o in orgs.get("rows", []):
        print(json.dumps({"name":o.get("name"),"inn":o.get("inn"),"kpp":o.get("kpp"),
                          "legalTitle":o.get("legalTitle"),"id":o.get("id")[:8]+"..."}, ensure_ascii=False))

def post(path, payload):
    import urllib.request, gzip, json as _j
    data=_j.dumps(payload, ensure_ascii=False).encode()
    req=urllib.request.Request(MS+path, data=data, method="POST", headers={
        "Authorization": f"Bearer {TOK}", "Accept-Encoding":"gzip",
        "Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d=r.read()
            if r.headers.get("Content-Encoding")=="gzip": d=gzip.decompress(d)
            return r.status, _j.loads(d)
    except urllib.error.HTTPError as e:
        d=e.read()
        try:
            if e.headers.get("Content-Encoding")=="gzip": d=gzip.decompress(d)
        except: pass
        return e.code, _j.loads(d.decode(errors="replace"))

def put(path, payload):
    import urllib.request, gzip, json as _j
    data=_j.dumps(payload, ensure_ascii=False).encode()
    req=urllib.request.Request(MS+path, data=data, method="PUT", headers={
        "Authorization": f"Bearer {TOK}", "Accept-Encoding":"gzip", "Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d=r.read()
            if r.headers.get("Content-Encoding")=="gzip": d=gzip.decompress(d)
            return r.status, _j.loads(d)
    except urllib.error.HTTPError as e:
        d=e.read()
        try:
            if e.headers.get("Content-Encoding")=="gzip": d=gzip.decompress(d)
        except: pass
        return e.code, _j.loads(d.decode(errors="replace"))
