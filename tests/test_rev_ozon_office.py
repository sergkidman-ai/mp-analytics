"""Exercise actual Chromium navigation on a local intercepted seller UI fixture."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID
from tools.ozon_lk.office import collect

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, "Install the office collector requirements for its browser test")
class OfficeReviewTest(unittest.TestCase):
    def test_reopen_paginate_deduplicate_and_update(self):
        with TemporaryDirectory() as tmp, sqlite3.connect(":memory:") as db, sync_playwright() as pw:
            for poll in range(2):
                ctx = pw.chromium.launch_persistent_context(str(Path(tmp)/"profile"),headless=True)
                page = ctx.new_page()
                def respond(route):
                    second = "?page=2" in route.request.url
                    rid = str(UUID(int=2 if second else 1))
                    status = "Обработан" if second else "Нет ответа продавца"
                    reply = "Спасибо!" if poll and not second else ""
                    nxt = "" if second else '<a href="?page=2">Следующая</a>'
                    route.fulfill(content_type="text/html; charset=utf-8",body=f"""
                       <header>Тестовый магазин</header><table><thead><tr><th>Отзыв</th><th>Статус</th>
                       <th>Ответ продавца</th></tr></thead><tbody><tr data-review-id="{rid}">
                       <td>Тест</td><td>{status}</td><td>{reply}</td></tr></tbody></table>{nxt}""")
                page.route("**/*",respond)
                try:
                    rows=collect.scan(page,"Тестовый магазин",10)
                    self.assertEqual(len(rows),2)
                    self.assertEqual(rows[str(UUID(int=2))]["state"],"unknown")
                    self.assertEqual(rows[str(UUID(int=1))]["state"],"answered" if poll else "unanswered")
                    self.assertEqual(collect.save(db,"oz_acc1",rows),0 if poll else 2)
                    self.assertEqual(collect.save(db,"oz_acc2",rows),0 if poll else 2)
                finally:
                    ctx.close()
