# поток: prc
"""Solutions Print cabinet -> MSK enters; daily 08:07, 13:12, 18:17 Moscow.

Default is a read-only preview. --apply enables the user-authorized recurrent
replacement. Interrupted writes are recovered from the frozen local plan before
another source is fetched. Draft enters do not inflate stock during card updates.
"""
import argparse
import fcntl
import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from core import ms_api
from core.db import query
from . import anomaly, blacklist, catalog, journal, loader, novelty
from .cbr import effective_rate, to_kopecks
from .profiles import ORG_DIGITAL, STORE_REMOTE, Profile
from .run import write_reports
from .solutionsprint_source import collect, CATEGORIES, SCOPE

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "logs/prc_solutionsprint_state.json"
LOCK = ROOT / "logs/prc_solutionsprint.lock"
OUT = ROOT / "docs/reports/prc_solutionsprint"
SUPPLIER = "9263b7de-97bf-11f0-0a80-015900374bc3"
RUB = "20d3bd8c-bde9-47cc-ba24-b299ba80b6d7"
PROFILE = Profile(key="s_print_msk", title="Солюшнс принт МСК", mail_folder="",
                  columns={}, stock_map={}, supplier_ids=[SUPPLIER])

ORIGINAL = re.compile(r"\b(?:оригинал\w*|original\w*|oem)\b", re.I)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temp.replace(path)


def normalize(snapshot, moment):
    if not snapshot.get("complete") or snapshot.get("region") != "Москва":
        raise RuntimeError("Incomplete source or non-Moscow price region")
    rows, rates = [], {}
    seen = set()
    for index, item in enumerate(snapshot["rows"], 1):
        code = item["supplier_code"]
        if not re.fullmatch(r"[\w./-]+", code) or code in seen:
            raise RuntimeError("Duplicate or invalid supplier code")
        seen.add(code)
        if item["currency"] not in ("RUB", "USD"):
            raise RuntimeError("Unsupported price currency")
        source_price = Decimal(item["price"])
        if not source_price.is_finite() or source_price < 0:
            raise RuntimeError("Invalid supplier price")
        if source_price and item["currency"] not in rates:
            rate, rate_date = effective_rate(moment.date(), item["currency"], "1.03")
            rates[item["currency"]] = {"rate": str(rate), "date": rate_date}
        stocks = [Decimal(str(item[k])) for k in ("msk", "spb")]
        if any(not n.is_finite() or n < 0 for n in stocks):
            raise RuntimeError("Exact nonnegative warehouse stocks required")
        defective = "брак" in item["name"].casefold()
        quantity = Decimal(0) if defective else sum(stocks)
        qty = int(quantity) if quantity == quantity.to_integral_value() else float(quantity)
        price = (Decimal(to_kopecks(item["price"], Decimal(rates[item["currency"]]["rate"]))) / 100
                 if source_price else Decimal(0))
        if price < 0:
            raise RuntimeError("Negative supplier price")
        rows.append({"row": index, "article": f"SP{code}_MSK", "name": item["name"],
                     "qty": qty, "price_raw": str(price),
                     "stock_raw": f"Москва={item['msk']}; СПб={item['spb']}",
                     "source_defective": defective, "source_composite": "/" in code,
                     "source_scope": item.get("supplier_scope_verified") is True,
                     "source_ambiguous": bool(item.get("ambiguous"))})
    return rows, rates


def owned_doc(doc):
    return (bool(re.fullmatch(r"s_print_msk_\d{4}-\d{2}-\d{2}_p\d+(?:_old\d+)*", doc.get("name", "")))
            and ms_api.meta_id(doc, "store") == STORE_REMOTE
            and ms_api.meta_id(doc, "organization") == ORG_DIGITAL)


def existing_docs():
    result = list(ms_api.iter_rows("/entity/enter", {
        "filter": ["name~=s_print_msk_", f"store={ms_api.BASE}/entity/store/{STORE_REMOTE}"]}, page=20))
    if any(not owned_doc(d) for d in result):
        raise RuntimeError("Unexpected MSK enter scope; manual review required")
    return result


def positions(doc_id):
    return list(ms_api.iter_rows(f"/entity/enter/{doc_id}/positions", page=100))


def position_signature(rows):
    return sorted((r["assortment"]["meta"]["href"], str(Decimal(str(r["quantity"])).normalize()),
                   int(r["price"])) for r in rows)


def ensure_scope(current, expected):
    if not owned_doc(current) or current["id"] != expected["id"]:
        raise RuntimeError("Enter identity/scope changed")
    if current.get("updated") != expected.get("updated"):
        raise RuntimeError("Existing enter changed since preview; refusing deletion")


def prepare(snapshot, out):
    if snapshot.get("categories") != CATEGORIES or snapshot.get("scope") != SCOPE:
        raise RuntimeError("Source category/manufacturer scope is obsolete; fetch a fresh catalogue")
    moment = loader.now_msk()
    rows, rates = normalize(snapshot, moment)
    black = blacklist.load_set()
    candidates, skipped = [], []
    for row in rows:
        if row["source_defective"]:
            skipped.append({**row, "qty": 0, "reason": "defective"})
        elif not row["source_scope"] or ORIGINAL.search(row["name"]):
            skipped.append({**row, "qty": 0, "reason": "out_of_scope"})
        elif row["source_composite"]:
            skipped.append({**row, "reason": "composite_code"})
        elif row["source_ambiguous"]:
            skipped.append({**row, "reason": "duplicate"})
        elif not row["qty"]:
            skipped.append({**row, "reason": "no_stock"})
        elif blacklist.norm(row["article"]) in black or blacklist.in_rules(row["article"], PROFILE.key):
            skipped.append({**row, "reason": "blacklisted"})
        else:
            candidates.append(row)
    ready, dropped = loader.classify(candidates, PROFILE, Decimal(1))
    skipped += dropped
    # Preserve only exact Moscow supplier cards. Do not remap archived articles by analogy.
    exact = []
    for row in ready:
        card = row["card"]
        if ORIGINAL.search(card.get("name", "")):
            skipped.append({**row, "qty": 0, "reason": "out_of_scope"})
        elif card.get("article") != row["article"] or ms_api.meta_id(card, "supplier") != SUPPLIER:
            skipped.append({**row, "reason": "archived" if row.get("archived_swap") else "foreign"})
        else:
            exact.append(row)
    # A DB failure is not the same as having no historical price baseline.
    base = {r["article"]: (Decimal(r["price_rub"]), anomaly.SRC_LOAD)
            for r in query(anomaly.PREV_SQL, (PROFILE.key,))}
    ready, dropped, flags, checks = anomaly.screen(exact, PROFILE, base)
    skipped += dropped
    for row in skipped:
        if row["reason"] == "defective":
            row["qty"] = 0
    skipped, _ = blacklist.mark(skipped, black, supplier_key=PROFILE.key)
    skipped, _, watch = novelty.screen(skipped, ready + skipped)
    docs = loader.build_docs(ready, PROFILE, moment)
    stale = existing_docs()
    # Complete before snapshots also make rollback/reconciliation possible.
    for doc in stale:
        doc["saved_positions"] = positions(doc["id"])
    updates = loader.card_updates(ready, PROFILE)
    for patch in updates:
        if "buyPrice" in patch:
            patch["buyPrice"]["currency"] = ms_api.ref("currency", RUB)
    # Correct currency even when the numeric value happens to be unchanged.
    for item in ready:
        card = item["card"]
        if card["meta"]["type"] == "product" and ms_api.meta_id(card.get("buyPrice") or {}, "currency") != RUB:
            patch = next((p for p in updates if p["id"] == card["id"]), None)
            if patch is None:
                patch = {"id": card["id"], "meta": card["meta"]}
                updates.append(patch)
            patch["buyPrice"] = {"value": item["price_kop"], "currency": ms_api.ref("currency", RUB)}
    current_cards = {r["card"]["id"]: r["card"] for r in ready}
    updates = [p for p in updates if not patch_matches(current_cards[p["id"]], p)]
    if len(updates) + 3 * len(docs) + len(stale) > 10000:
        raise RuntimeError("MS entity change budget exceeded; review required")
    old_count = sum(len(d["saved_positions"]) for d in stale if d.get("applicable"))
    # A failed/missing source must never silently replace most of the stock.
    if old_count and len(ready) < old_count * 0.7:
        raise RuntimeError(f"Ready positions fell from {old_count} to {len(ready)}; review required")
    if not ready or checks["blocked"]:
        raise RuntimeError("Empty price list or price anomaly gate blocked")
    write_reports(PROFILE, moment, ready, skipped, watch, out)
    anomaly.write_report(flags, out / "anomalies.csv")
    run_id = moment.strftime("%Y%m%d%H%M%S%f")
    for doc in docs:
        doc["final_name"] = doc["name"]
        doc["name"] += "_old" + run_id
        doc["applicable"] = False
    plan = {"run_id": run_id, "moment": moment.isoformat(), "source_at": snapshot["collected_at"],
            "rates": rates, "docs": docs, "stale": stale, "updates": updates,
            "ready": ready, "skipped": skipped, "watch": watch,
            "summary": {"source_rows": len(rows), "ready": len(ready), "skipped": len(skipped),
                        "reasons": dict(Counter(r["reason"] for r in skipped)), "new_docs": len(docs),
                        "old_docs": len(stale), "card_updates": len(updates),
                        "total_quantity": sum(r["qty"] for r in ready),
                        "sum_rub": str(sum(Decimal(r["price_kop"]) * Decimal(str(r["qty"])) / 100 for r in ready))}}
    save(out / "plan.json", plan)
    save(out / "summary.json", {"status": "preview", **plan["summary"]})
    return plan


def get_or_none(path):
    try:
        return ms_api.get(path)
    except ms_api.MsError as exc:
        if exc.status == 404:
            return None
        raise


def verify_document(doc, expected):
    if not owned_doc(doc) or position_signature(positions(doc["id"])) != position_signature(expected["positions"]):
        raise RuntimeError("Enter readback mismatch")


def superseded_pending(plan, state, state_path=STATE):
    """Retire a vanished batch without recreating obsolete stock after a manual import."""
    created = set(state.get("created", {}).values())
    if not created:
        return False
    live = {ident for ident in created if get_or_none(f"/entity/enter/{ident}") is not None}
    if live == created:
        return False
    if live:
        raise RuntimeError("Some staged enters were removed externally; refusing partial recovery")
    current = existing_docs()
    staged_names = {d["name"] for d in plan["docs"]}
    if any(d["name"] in staged_names for d in current):
        raise RuntimeError("Unrecorded staged enter remains; refusing to abandon pending run")
    if not current or not all(d.get("applicable") for d in current):
        raise RuntimeError("Staged enters disappeared without a complete posted replacement; review required")
    # Keep the original state and replacement identities for audit; no MS writes here.
    out = Path(state["out"])
    save(out / "superseded_state.json", state)
    save(out / "replacement_documents.json", current)
    state = dict(state, status="superseded", reason="All staged enters removed externally",
                 superseded_at=datetime.now(timezone.utc).isoformat())
    save(state_path, state)
    print("Previous staged batch removed externally; fetching a fresh catalogue", flush=True)
    return True


def clean_description(value):
    return (value or "").replace("\xa0", " ").strip()


def validate_ready(ready):
    """Recheck every position before posting, including cards with no field delta."""
    expected = {r["card"]["id"]: r for r in ready}
    if len(expected) != len(ready):
        raise RuntimeError("Several source articles map to one MS position")
    kinds = {r["card"]["meta"]["type"] for r in ready}
    for kind in kinds:
        ids = [r["card"]["id"] for r in ready if r["card"]["meta"]["type"] == kind]
        for start in range(0, len(ids), 20):
            batch = ids[start:start + 20]
            cards = list(ms_api.iter_rows(f"/entity/{kind}", {"filter": [f"id={i}" for i in batch]}, page=20))
            if {c["id"] for c in cards} != set(batch):
                raise RuntimeError("Position missing/archived since preview")
            for card in cards:
                if (card.get("archived") or "брак" in card.get("name", "").casefold() or
                        ORIGINAL.search(card.get("name", "")) or
                        not expected[card["id"]].get("source_scope") or
                        card.get("article") != expected[card["id"]]["article"] or
                        ms_api.meta_id(card, "supplier") != SUPPLIER):
                    raise RuntimeError("Position identity/archive/defect changed since preview")


def patch_matches(card, patch):
    if "description" in patch and clean_description(card.get("description")) != clean_description(patch["description"]):
        return False
    if "buyPrice" in patch:
        if int((card.get("buyPrice") or {}).get("value", 0)) != patch["buyPrice"]["value"]:
            return False
        if ms_api.meta_id(card.get("buyPrice") or {}, "currency") != RUB:
            return False
    return True


def wait_other_writers():
    # The existing nightly price writer and the one-time description fills share webhooks.
    result = subprocess.run(["systemctl", "list-units", "--state=running,activating", "--no-legend", "--plain"],
                            capture_output=True, text=True, check=True)
    conflicts = [line.split()[0] for line in result.stdout.splitlines()
                 if "prc-enter-buyprice.service" in line or re.search(r"card-.*description.*\.service", line)]
    if conflicts:
        raise RuntimeError("Another MS card writer is active; retry next timer: " + ", ".join(conflicts))


def apply(plan, out, state_path=STATE):
    wait_other_writers()
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("status") == "pending" and state.get("run_id") != plan["run_id"]:
        raise RuntimeError("Recover previous run before creating another")
    if state.get("status") == "pending":
        allowed = set(state["created"].values()) | {d["id"] for d in plan["stale"]}
        # An unsaved create response may leave a draft discoverable by its unique name.
        staged_names = {d["name"] for d in plan["docs"]}
        if any(d["id"] not in allowed and d["name"] not in staged_names for d in existing_docs()):
            raise RuntimeError("MSK enters replaced externally during pending run; refusing stale recovery")
    if state.get("run_id") != plan["run_id"]:
        validate_ready(plan["ready"])
        # Preflight all old documents BEFORE the first write.
        for old in plan["stale"]:
            current = ms_api.get(f"/entity/enter/{old['id']}")
            ensure_scope(current, old)
            if position_signature(positions(old["id"])) != position_signature(old["saved_positions"]):
                raise RuntimeError("Old enter positions changed")
        if {d["id"] for d in existing_docs()} != {d["id"] for d in plan["stale"]}:
            raise RuntimeError("MSK enter set changed since preparation")
        state = {"status": "pending", "run_id": plan["run_id"], "out": str(out),
                 "created": {}, "cards": [], "deleted": [], "last_batch_at": 0, "canary_done": False}
        save(state_path, state)
    # Deterministic unique draft names allow recovery of a successful request whose response was lost.
    for index, spec in enumerate(plan["docs"]):
        key = str(index)
        if key in state["created"]:
            doc = ms_api.get(f"/entity/enter/{state['created'][key]}")
        else:
            found = list(ms_api.iter_rows("/entity/enter", {"filter": f"name={spec['name']}"}, page=20))
            if len(found) > 1:
                raise RuntimeError("Ambiguous staged document")
            doc = found[0] if found else ms_api.post("/entity/enter", {k: v for k, v in spec.items() if k != "final_name"})
            state["created"][key] = doc["id"]
            save(state_path, state)
        verify_document(doc, spec)
    card_map = {r["card"]["id"]: r for r in plan["ready"]}
    remaining = [p for p in plan["updates"] if p["id"] not in state["cards"]]
    for start in range(0, len(remaining), 50):
        wait_other_writers()
        delay = max(0, state["last_batch_at"] + 60 - time.time())
        if delay:
            time.sleep(delay)
        if len(state["cards"]) >= 200 and not state["canary_done"]:
            # Persistent time prevents restarting the ten-minute pause on recovery.
            until = state.setdefault("canary_until", time.time() + 600)
            save(state_path, state)
            while time.time() < until:
                time.sleep(min(30, until - time.time()))
            started = time.monotonic()
            ms_api.get("/entity/customerorder", {"limit": 1, "order": "updated,desc"})
            if time.monotonic() - started > 10:
                raise RuntimeError("MS operational canary is slow; stop")
            state["canary_done"] = True
            save(state_path, state)
        batch = remaining[start:start + 50]
        sending = []
        for patch in batch:
            card = ms_api.get(f"/entity/product/{patch['id']}")
            wanted = card_map[patch["id"]]
            if (card.get("archived") or "брак" in card.get("name", "").casefold() or
                    card.get("article") != wanted["article"] or ms_api.meta_id(card, "supplier") != SUPPLIER):
                raise RuntimeError("Product identity/archive/defect changed since preview")
            if not patch_matches(card, patch):
                sending.append(patch)
        # Save intent before mutation, then verify even after an ambiguous transport failure.
        save(out / f"card_batch_{start:05d}.json", {"intent": sending})
        if sending:
            state["last_batch_at"] = time.time()
            save(state_path, state)
            ms_api.post("/entity/product", sending)
        for patch in batch:
            if not patch_matches(ms_api.get(f"/entity/product/{patch['id']}"), patch):
                raise RuntimeError("Product update readback mismatch")
            state["cards"].append(patch["id"])
        save(state_path, state)
        print(f"verified card updates={len(state['cards'])}/{len(plan['updates'])}", flush=True)
    validate_ready(plan["ready"])
    # Recheck old documents immediately before switching stocks. Recovery may have already deleted some.
    allowed = set(state["created"].values()) | {d["id"] for d in plan["stale"]}
    if any(d["id"] not in allowed for d in existing_docs()):
        raise RuntimeError("MSK enter set changed during card updates; refusing replacement")
    for old in plan["stale"]:
        current = get_or_none(f"/entity/enter/{old['id']}")
        if current:
            ensure_scope(current, old)
            if position_signature(positions(old["id"])) != position_signature(old["saved_positions"]):
                raise RuntimeError("Old positions changed before replacement")
    for index, spec in enumerate(plan["docs"]):
        doc_id = state["created"][str(index)]
        current = ms_api.get(f"/entity/enter/{doc_id}")
        verify_document(current, spec)
        if not current.get("applicable"):
            ms_api.put(f"/entity/enter/{doc_id}", {"applicable": True})
        current = ms_api.get(f"/entity/enter/{doc_id}")
        if not current.get("applicable"):
            raise RuntimeError("New enter not applicable")
    # Only after ALL new documents are verified and posted may old stock disappear.
    for old in plan["stale"]:
        path = f"/entity/enter/{old['id']}"
        current = get_or_none(path)
        if current:
            ensure_scope(current, old)
            ms_api.delete(path)
        if get_or_none(path) is not None:
            raise RuntimeError("Old enter deletion not confirmed")
        if old["id"] not in state["deleted"]:
            state["deleted"].append(old["id"])
            save(state_path, state)
    for index, spec in enumerate(plan["docs"]):
        doc_id = state["created"][str(index)]
        current = ms_api.get(f"/entity/enter/{doc_id}")
        if current["name"] != spec["final_name"]:
            ms_api.put(f"/entity/enter/{doc_id}", {"name": spec["final_name"]})
        final = ms_api.get(f"/entity/enter/{doc_id}")
        verify_document(final, spec)
        if final["name"] != spec["final_name"] or not final.get("applicable"):
            raise RuntimeError("Final enter name/posting mismatch")
    if {d["id"] for d in existing_docs()} != set(state["created"].values()):
        raise RuntimeError("Unexpected MSK documents after replacement")
    if not state.get("load_id"):
        moment = datetime.fromisoformat(plan["moment"])
        # A DB commit can succeed just before the local state write is interrupted.
        previous = query("SELECT id FROM prc_price_load WHERE supplier_key=%s AND source_file=%s "
                         "AND status='ok' AND dry_run=false ORDER BY id DESC LIMIT 1",
                         (PROFILE.key, str(out / "source.json")))
        state["load_id"] = previous[0]["id"] if previous else journal.save({
            "supplier_key": PROFILE.key, "load_date": moment.date(), "moment": moment,
            "source_file": str(out / "source.json"), "source_kind": "cabinet", "currency": "RUB",
            "rate": Decimal(1), "rate_date": None, "rows_total": plan["summary"]["source_rows"],
            "rows_loaded": len(plan["ready"]), "rows_skipped": len(plan["skipped"]),
            "docs": len(plan["docs"]), "stale_docs": len(plan["stale"]),
            "card_updates": len(plan["updates"]), "sum_rub": plan["summary"]["sum_rub"],
            "dry_run": False, "status": "ok", "error": None}, plan["ready"], plan["skipped"])
        save(state_path, state)
    novelties = [{"name": r["name"], "article": r["article"], "price": Decimal(r["price_raw"]), "reason": r["reason"]}
                 for r in plan["skipped"] if r["reason"] in ("not_found", "ambiguous", "foreign")]
    catalog.sync(novelties, PROFILE.key, PROFILE.default_chip, PROFILE.article_re)
    state["status"] = "complete"
    save(state_path, state)
    save(out / "summary.json", {"status": "complete", "load_id": state["load_id"], **plan["summary"]})
    print("Replacement verified: " + json.dumps(plan["summary"], ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--source", type=Path, help="Saved complete catalogue snapshot for preview")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Solutions Print run is active")
        if args.apply and STATE.exists():
            state = json.loads(STATE.read_text())
            if state.get("status") == "pending":
                old_out = Path(state["out"])
                old_plan = json.loads((old_out / "plan.json").read_text())
                if not superseded_pending(old_plan, state):
                    apply(old_plan, old_out)
                    return 0
        out = args.out or OUT / loader.now_msk().strftime("%Y%m%d_%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        snapshot = json.loads(args.source.read_text()) if args.source else collect(out)
        if args.apply:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["collected_at"])).total_seconds()
            if age < 0 or age > 3600:
                raise RuntimeError("Source older than one hour; fetch a fresh catalogue")
        save(out / "source.json", snapshot)
        plan = prepare(snapshot, out)
        print("Preview: " + json.dumps(plan["summary"], ensure_ascii=False), flush=True)
        if args.apply:
            apply(plan, out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Do not leak HTTP response bodies / tokens into systemd logs.
        print(f"Solutions Print failed: {type(exc).__name__}: " +
              (f"HTTP {exc.status}; API codes=" + str([
                  e.get("code") for e in exc.body.get("errors", [])
              ] if isinstance(exc.body, dict) else []) if isinstance(exc, ms_api.MsError) else
               str(exc) if isinstance(exc, RuntimeError)
               else "see local run artifacts; credentials redacted"), file=sys.stderr)
        sys.exit(1)
