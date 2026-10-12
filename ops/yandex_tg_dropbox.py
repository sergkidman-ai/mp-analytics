#!/usr/bin/env python3
# поток: ev
"""Collect relevant posts from Yandex Market's public Telegram channel into Dropbox.

Files use the existing ``*_note.txt`` intake, so ops.diary_inbox imports them into
the home diary. This deliberately does not consume Telegram getUpdates: that belongs
to the running Dropbox bot and must remain its only poller.
"""
import os
import re
import sys
from datetime import datetime, timedelta
from html.parser import HTMLParser
from time import sleep
import requests

sys.path.insert(0, "/opt/mp-analytics")
from ops.mp_news import classify
from core import db

CHANNEL = "market_marketplace"
URL = f"https://t.me/s/{CHANNEL}"
DROPBOX = "/opt/mp-analytics/dropbox"
MAX_PAGES = 12
PAGE_SIZE_DAYS = 90


class _Posts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self.current = None
        self.in_text = False
        self.text_depth = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag == "div" and "tgme_widget_message_wrap" in classes:
            self.current = {"id": "", "date": "", "text": [], "title": ""}
            self.depth = 1
        elif tag == "div" and self.current is not None:
            self.depth += 1
        if self.current is None:
            return
        if tag == "div" and "tgme_widget_message" in classes:
            self.current["id"] = a.get("data-post", "").rsplit("/", 1)[-1]
        elif tag == "time" and a.get("datetime"):
            self.current["date"] = a["datetime"]
        elif tag == "div" and "tgme_widget_message_text" in classes:
            self.in_text = True
            self.text_depth = self.depth
        elif self.in_text and tag == "br":
            self.current["text"].append("\n")
        elif self.in_text and tag in ("b", "strong"):
            self.current["text"].append("\n")

    def handle_endtag(self, tag):
        if tag == "div" and self.current is not None:
            if self.in_text and self.depth == self.text_depth:
                self.in_text = False
                self.text_depth = None
            self.depth -= 1
            if self.depth > 0:
                return
            body = re.sub(r"[ \t]+", " ", "".join(self.current["text"]))
            body = re.sub(r"\n\s*\n+", "\n", body).strip()
            if self.current["id"].isdigit() and body:
                self.current["text"] = body
                self.posts.append(self.current)
            self.current = None
            self.in_text = False

    def handle_data(self, data):
        if self.in_text and self.current is not None:
            self.current["text"].append(data)


def parse_posts(html):
    parser = _Posts()
    parser.feed(html)
    return parser.posts


def fetch_page(before=None):
    url = URL + (f"?before={int(before)}" if before else "")
    for attempt in range(3):
        try:
            response = requests.get(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible; mp-analytics/1.0)"},
                timeout=(5, 15))
            response.raise_for_status()
            return response.text
        except requests.RequestException:
            if attempt == 2:
                raise
            sleep(2 + 2 * attempt)


def collect(dry=False, days=PAGE_SIZE_DAYS):
    from zoneinfo import ZoneInfo
    msk = ZoneInfo("Europe/Moscow")
    cutoff = datetime.now(msk) - timedelta(days=days)
    before = None
    saved = 0
    examined = 0
    for _ in range(MAX_PAGES):
        posts = parse_posts(fetch_page(before))
        if not posts:
            break
        oldest_id = None
        reached_cutoff = False
        for post in posts:
            try:
                published = datetime.fromisoformat(post["date"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            oldest_id = int(post["id"]) if oldest_id is None else min(oldest_id, int(post["id"]))
            if published.astimezone(msk) < cutoff:
                reached_cutoff = True
                continue
            examined += 1
            text = post["text"]
            lines = [line.strip() for line in text.splitlines()
                     if line.strip() and re.search(r"[a-zа-яё0-9]", line, re.I)]
            title = (lines[0] if lines else "Новости Маркета")[:200]
            importance, rule = classify(title, text, platform="yandex")
            if importance != "alert":
                continue
            post_date = published.astimezone(msk).date()
            fname = f"{post_date:%Y%m%d}_000000_yandex_tg_{post['id']}_note.txt"
            path = os.path.join(DROPBOX, fname)
            already_imported = db.query(
                "SELECT 1 FROM diary_inbox WHERE fname=%s LIMIT 1", (fname,))
            same_mail_event = db.query(
                "SELECT 1 FROM biz_events WHERE kind='mp' AND platform='yandex' "
                "AND event_date=%s AND lower(title)=lower(%s) LIMIT 1",
                (post_date, title))
            if os.path.exists(path) or already_imported or same_mail_event:
                continue
            # Keep the first line as the real announcement title; the source marker
            # lets diary_inbox assign the Yandex platform without changing that title.
            full_text = (f"{title}\nПлощадка: Маркет\nДата новости: {post_date:%d.%m.%Y}\n\n"
                         f"Источник: https://t.me/{CHANNEL}/{post['id']}\n"
                         f"Правило отбора: {rule}\n\n{text}\n")
            if not dry:
                os.makedirs(DROPBOX, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(full_text)
                os.replace(tmp, path)
            saved += 1
        if reached_cutoff or oldest_id is None or oldest_id <= 1:
            break
        before = oldest_id
    print(f"Маркет Telegram: просмотрено {examined}, новых важных {saved}" +
          (" (dry)" if dry else ""))
    return saved


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Новости Маркета из Telegram -> Dropbox")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--days", type=int, default=PAGE_SIZE_DAYS)
    args = ap.parse_args()
    try:
        collect(dry=args.dry, days=args.days)
    except requests.RequestException as exc:
        print(f"Маркет Telegram: ошибка получения канала ({type(exc).__name__})")
        sys.exit(1)
