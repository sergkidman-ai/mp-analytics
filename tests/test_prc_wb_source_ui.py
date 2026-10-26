# поток: prc
"""Exercise the real novelty dialog in Chromium; every network request is intercepted."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from prices import ms_import
from tests.test_prc_wb_source import TN, KINDS


@unittest.skipUnless(sync_playwright, "Playwright unavailable")
class NoveltyDialog(unittest.TestCase):
    def test_cactus_pair_keeps_two_names_and_mutual_links(self):
        html = Path("web/static/novelties.html").read_text()
        pos = html.rfind("load();")
        html = html[:pos] + html[pos:].replace("load();", "", 1)
        name = ("Тонер Cactus CS-TSGHPBK-500 черный флакон 500гр. "
                "для принтера Samsung CLT-K404; HP W2070")
        calls, errors = [], []
        with patch.object(ms_import, "query", return_value=[]), \
             patch.object(ms_import, "tc_kinds", return_value=KINDS), sync_playwright() as pw:
            sources = ms_import.novelty_wb_variants("", name, "CS-TSGHPBK-500", 2)
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))

            def handle(route):
                url = urlparse(route.request.url)
                if url.path == "/":
                    route.fulfill(content_type="text/html", body=html)
                    return
                data = {"ok": True}
                if url.path == "/api/novelties/ms-form":
                    cards = [dict(external_code=str(7243+i),code=f"{7243+i}ca",
                                  article="CS-TSGHPBK-500" if i == 0 else "",
                                  name=sources[i]["name"],description=name,folder="Тест",
                                  weight="1",buy_price="100",wb=sources[i]["name"],
                                  code128="",supplier_code="CS-TSGHPBK-500",
                                  link=str(7244-i)) for i in range(2)]
                    data.update(id=7330,supplier="Cactus",article="CS-TSGHPBK-500",name=name,
                                n=2,wb_source_code="",wb_source=sources[0],wb_sources=sources,
                                wb_pair_locked=True,wb_max_chars=55,wb_fact_options={},
                                folders=["Тест"],kinds=KINDS,kind="Тонер",printers=["принтер"],
                                printer="принтер",warn=[],cards=cards)
                elif url.path == "/api/novelties/series":
                    data.update(series="Бизнес",why="confirmed")
                elif url.path == "/api/novelties/ms-create":
                    body = route.request.post_data_json
                    self.assertTrue(body["dry"])
                    calls.append(body)
                    data.update(log=["Проба без записи"])
                route.fulfill(content_type="application/json",body=json.dumps(data,ensure_ascii=False))

            page.route("**/*", handle)
            try:
                page.goto("http://wbname.test/")
                page.evaluate("toMs(7330, null, '')")
                page.wait_for_function("document.querySelectorAll('.f-wb').length === 2")
                self.assertEqual([page.locator(".f-wb").nth(i).input_value() for i in range(2)],
                                 [s["name"] for s in sources])
                self.assertTrue(page.locator("#mscount").is_disabled())
                self.assertEqual([page.locator(".f-link").nth(i).input_value() for i in range(2)],
                                 ["7244","7243"])
                page.locator("#msdry").click()
                page.wait_for_function("document.querySelector('#mslog').textContent.includes('Проба без записи')")
                self.assertEqual(calls[0]["wb_source_tokens"],[s["token"] for s in sources])
                self.assertEqual([c["wb"] for c in calls[0]["cards"]],[s["name"] for s in sources])
                self.assertEqual(errors,[])
            finally:
                browser.close()

    def test_derived_name_confirmation_recalculation_and_card_count(self):
        html = Path("web/static/novelties.html").read_text()
        # Isolate this dialog from unrelated table loading; run its actual JS unchanged.
        pos = html.rfind("load();")
        html = html[:pos] + html[pos:].replace("load();", "", 1)
        calls = []
        with patch.object(ms_import, "query", return_value=[]), \
             patch.object(ms_import, "tc_kinds", return_value=KINDS), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            def handle(route):
                request = route.request
                url = urlparse(request.url)
                if url.path == "/":
                    route.fulfill(content_type="text/html", body=html)
                    return
                data = {"ok": True}
                if url.path == "/api/novelties/ms-form":
                    n = int(parse_qs(url.query).get("n", [1])[0])
                    source = ms_import.wb_details("", TN)
                    data.update(id=18958, supplier="Одиссей", article="WB TN2355", name=TN,
                                wb_source_code="", wb_source=source, wb_max_chars=55,
                                wb_fact_options={"kind":KINDS,"color":["черный"]},
                                folders=["Тест"], kinds=KINDS, kind="Картридж", printers=["МФУ"],
                                printer="МФУ", warn=[], cards=[{
                                    "external_code":str(9990+i),"code":f"{9990+i}wb","article":"A" if not i else "",
                                    "name":TN,"description":TN,"folder":"Тест","weight":"0.759",
                                    "buy_price":"289.63","wb":source["name"],"code128":"barcode",
                                    "supplier_code":"A","link":""} for i in range(n)])
                elif url.path == "/api/novelties/wb-name-preview":
                    body=request.post_data_json
                    data["wb_source"]=ms_import.wb_details(body["source_code"],TN,body["facts"],body["evidence"])
                elif url.path == "/api/novelties/series":
                    data.update(series="Комфорт",why="подтверждённые параметры")
                elif url.path == "/api/novelties/ms-create":
                    body=request.post_data_json
                    if not body["dry"]:
                        raise AssertionError("Live creation is forbidden in UI test")
                    calls.append(body)
                    data.update(log=["Проба без записи"])
                route.fulfill(content_type="application/json", body=json.dumps(data,ensure_ascii=False))

            page.route("**/*", handle)
            try:
                page.goto("http://wbname.test/")
                page.evaluate("toMs(18958, null, '')")
                self.assertEqual(page.locator(".f-wb").input_value(),"Картридж TN2355 для Brother")
                self.assertTrue(page.locator(".f-wb").evaluate("el => el.readOnly"))
                page.locator(".f-name").fill("Проверенное наименование")
                page.locator("#mscount").fill("2")
                page.locator("#mscount").dispatch_event("change")
                page.wait_for_function("document.querySelectorAll('.f-wb').length === 2")
                self.assertEqual(page.locator(".f-name").first.input_value(),"Проверенное наименование")
                page.locator("#mswbdetails").evaluate("el => el.open=true")
                page.locator('[data-wbfact="color"]').fill("черный")
                self.assertTrue(page.locator("#msdry").is_disabled())
                page.locator("#mswbevidence").fill("Тестовая спецификация поставщика: Black")
                page.locator("#mswbrecalc").click()
                page.wait_for_function("!document.querySelector('#msdry').disabled")
                self.assertEqual(page.locator(".f-wb").nth(1).input_value(),"Картридж TN2355 для Brother черный")
                page.locator("#mscount").fill("3")
                page.locator("#mscount").dispatch_event("change")
                page.wait_for_function("document.querySelectorAll('.f-wb').length === 3 && !document.querySelector('#msdry').disabled")
                self.assertEqual(page.locator(".f-wb").nth(2).input_value(),"Картридж TN2355 для Brother черный")
                page.locator("#mswbevidence").fill("")
                page.locator("#mswbrecalc").click()
                page.wait_for_function("document.querySelector('#mswbstatus').textContent.includes('источник')")
                self.assertTrue(page.locator("#msgo").is_disabled())
                page.locator('[data-wbfact="color"]').fill("")
                page.locator("#mswbrecalc").click()
                page.wait_for_function("!document.querySelector('#msdry').disabled")
                page.locator("#msdry").click()
                page.wait_for_function("document.querySelector('#mslog').textContent.includes('Проба без записи')")
                self.assertEqual(len(calls),1)
                self.assertEqual(calls[0]["wb_source_token"],ms_import.wb_details("",TN)["token"])
                self.assertEqual(calls[0]["wb_facts"],{})
                self.assertTrue(all(c["wb"]=="Картридж TN2355 для Brother" for c in calls[0]["cards"]))
                self.assertEqual(errors,[])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
