# поток: inv
"""invoice_bot/alfa_scope_check.py — есть ли у ключа Альфы право на платёжные поручения.

Мы ждём от банка scope платёжек (`signature`/`payment`) на БОЕВОЙ ключ: без него
`POST /jp/v2/payments` отдаёт 403 `insufficient_scope` (см. docs/ALFA_SCOPE_REQUEST.md).
Этот скрипт отвечает на вопрос «уже выдали?» НИЧЕГО НЕ СОЗДАВАЯ:

  • GET  /jp/v2/payments/{несуществующий uuid}  — чтение, платёжку не создаёт;
  • POST /jp/v2/payments с ПУСТЫМ телом        — создать платёжку таким запросом нельзя,
    но банк проверяет скоуп ДО валидации тела, поэтому ответ различает случаи:
      403 insufficient_scope → права нет;
      400 invalid_request    → права ЕСТЬ (упёрлись в отсутствующие поля) → можно в бой;
  • GET  /jp/v1/statement/transactions — контроль, что ключ/mTLS вообще живы (ожидаем 200);
  • GET  /pp/v1/accounts — всегда 403: метод для ФЛ, для ЮЛ скоуп не выдаётся (2026-07-29,
    поддержка Альфы), остаток на р/с в контуре оплаты не используется.

Запуск (по умолчанию — контур из .env, `--both` — прод и песочница подряд):
  ./venv/bin/python invoice_bot/alfa_scope_check.py
  ./venv/bin/python invoice_bot/alfa_scope_check.py --both
"""
import os
import sys
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/opt/mp-analytics")

PROBE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "mp-analytics/alfa/scope-probe"))


def check(env=None):
    """Возвращает True, если скоуп платёжек на этом контуре есть."""
    if env:
        os.environ["ALFA_ENV"] = env
    # импорт внутри функции: _cfg() читает ALFA_ENV в момент вызова, а load_dotenv
    # уже заданную переменную не перезаписывает — так --both проверяет оба контура
    from collectors.alfa_statement import _cfg, _session
    cfg = _cfg()
    s = _session(cfg)
    prod = cfg["env"] != "sandbox"
    print(f"\n=== контур {cfg['env']} ({cfg['base']})")

    acc = os.getenv("ALFA_ACCOUNT_PROD" if prod else "ALFA_ACCOUNT")
    d = (date.today() - timedelta(days=1)).isoformat()
    r = s.get(f"{cfg['base']}/jp/v1/statement/transactions",
              params={"accountNumber": acc, "statementDate": d, "page": 1}, timeout=60)
    print(f"  GET  выписка ................ {r.status_code}"
          f"{'  ← ключ/mTLS живы' if r.status_code == 200 else '  ← ключ или mTLS не работают: ' + r.text[:120]}")

    r = s.get(f"{cfg['base']}/jp/v2/payments/{PROBE_ID}", timeout=60)
    print(f"  GET  платёжка (нет такой) ... {r.status_code} {r.json().get('error', '') if r.headers.get('content-type', '').startswith('application/json') else ''}")

    r = s.post(f"{cfg['base']}/jp/v2/payments", json={}, timeout=60)
    err = ""
    try:
        err = (r.json() or {}).get("error", "")
    except ValueError:
        err = r.text[:80]
    granted = r.status_code != 403
    print(f"  POST платёжка (пустое тело) . {r.status_code} {err}")
    print("  ВЕРДИКТ: скоуп платёжек " + ("ЕСТЬ — можно отправлять черновики" if granted
                                          else "НЕ ВЫДАН — ждём банк (403 insufficient_scope)"))
    return granted


def main():
    envs = ["prod", "sandbox"] if "--both" in sys.argv else [None]
    results = {}
    for env in envs:
        # каждый контур — в отдельном процессе не нужен: _cfg() читает env заново,
        # а requests.Session создаётся своя под свой mTLS-сертификат
        results[env or os.getenv("ALFA_ENV", "sandbox")] = check(env)
    print()
    for env, ok in results.items():
        print(f"{env:8} — скоуп платёжек: {'да' if ok else 'НЕТ'}")
    # код возврата: 0 — скоуп есть везде, где проверяли; 1 — где-то нет (удобно для крона)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
