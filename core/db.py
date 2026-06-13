"""core/db.py — подключение к PostgreSQL и низкоуровневые хелперы.

Подключение по DATABASE_URL из /opt/mp-analytics/.env (секреты не хардкодим).
Помощники: выполнение SQL, выборка, применение SQL-файла (миграции) и
идемпотентный UPSERT (принцип 4 ARCHITECTURE.md — повторный сбор без дублей).
"""
import os
from pathlib import Path
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # /opt/mp-analytics
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан в .env")


@contextmanager
def get_conn():
    """Соединение как контекст: commit при успехе, rollback при ошибке, close в конце."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(sql, params=None):
    """DDL/DML без возврата строк. Возвращает число затронутых строк."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def query(sql, params=None):
    """SELECT → список словарей (по именам колонок)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def apply_sql_file(path):
    """Применить SQL-файл целиком (миграции). Весь файл — одна транзакция."""
    sql = Path(path).read_text(encoding="utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def upsert(table, rows, conflict_cols, update_cols=None):
    """Идемпотентная вставка: INSERT ... ON CONFLICT (conflict_cols) DO UPDATE/NOTHING.

    rows         — список dict с одинаковым набором колонок;
    conflict_cols — натуральный ключ (колонки UNIQUE/PK);
    update_cols  — что обновлять при конфликте; по умолчанию все, кроме conflict_cols.
    Возвращает число обработанных строк.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict_cols]
    col_sql = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    conflict_sql = ", ".join(conflict_cols)
    if update_cols:
        set_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        action = f"DO UPDATE SET {set_sql}"
    else:
        action = "DO NOTHING"
    sql = (f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) "
           f"ON CONFLICT ({conflict_sql}) {action}")
    values = [[r[c] for c in cols] for r in rows]
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, values)
            return len(values)


if __name__ == "__main__":
    # быстрый self-check подключения
    row = query("SELECT current_database() AS db, version() AS v")[0]
    print("БД:", row["db"])
    print("Версия:", row["v"].split(",")[0])
