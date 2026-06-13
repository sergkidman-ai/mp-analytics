import os
from dotenv import load_dotenv
import psycopg2

load_dotenv("/opt/mp-analytics/.env")
dsn = os.getenv("DATABASE_URL")
print("DATABASE_URL найден:", bool(dsn))

conn = psycopg2.connect(dsn)
cur = conn.cursor()
cur.execute("SELECT version();")
print("Ответ БД:", cur.fetchone()[0])
cur.close()
conn.close()
print("Связь Python → PostgreSQL работает.")
