# поток: fin
"""Durable upload intent written BEFORE any WB price POST (SQLite FULL synchronous)."""
import json
import sqlite3
import time


class Journal:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY, account TEXT NOT NULL, created REAL NOT NULL,
            state TEXT NOT NULL, upload_id INTEGER, payload TEXT NOT NULL,
            error TEXT, checked REAL, terminal INTEGER NOT NULL DEFAULT 0)''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, created REAL NOT NULL, upload INTEGER,
            state TEXT, detail TEXT)''')
        self.conn.commit()

    def event(self, uid, state, detail):
        self.conn.execute('INSERT INTO events(created,upload,state,detail) VALUES(?,?,?,?)',
                          (time.time(), uid, state, json.dumps(detail, ensure_ascii=False)))

    def intent(self, account, rows):
        with self.conn:
            uid = self.conn.execute('INSERT INTO uploads(account,created,state,payload) VALUES(?,?,?,?)',
                                    (account, time.time(), 'intent', json.dumps(rows, ensure_ascii=False))).lastrowid
            self.event(uid, 'intent', {'n': len(rows)})
        return uid

    def update(self, uid, state, *, upload_id=None, error=None, terminal=False, detail=None):
        with self.conn:
            self.conn.execute('UPDATE uploads SET state=?,upload_id=COALESCE(?,upload_id),error=?,checked=?,terminal=? WHERE id=?',
                              (state, upload_id, error, time.time(), int(terminal), uid))
            self.event(uid, state, detail or {'error': error})

    def pending(self, account):
        return [dict(x) for x in self.conn.execute('SELECT * FROM uploads WHERE account=? AND terminal=0 ORDER BY id', (account,))]

    def recent_failures(self, account, seconds=3600):
        rows = self.conn.execute("SELECT id,state,payload FROM uploads WHERE account=? AND created>? AND state IN ('rejected','partial')",
                                 (account, time.time() - seconds))
        counts = {}
        for row in rows:
            unconfirmed = None
            if row['state'] == 'partial':
                event = self.conn.execute("SELECT detail FROM events WHERE upload=? AND state='partial' ORDER BY id DESC LIMIT 1", (row['id'],)).fetchone()
                if event:
                    unconfirmed = set(json.loads(event[0]).get('unconfirmed', []))
            for item in json.loads(row['payload']):
                nm = item['nm_id']
                if unconfirmed is not None and nm not in unconfirmed:
                    continue
                counts[nm] = counts.get(nm, 0) + 1
        return counts
