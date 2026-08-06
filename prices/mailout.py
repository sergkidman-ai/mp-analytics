# поток: prc
# -*- coding: utf-8 -*-
"""
Отправка отчёта прогона на почту (список новинок оператору).

Шлём с нашего же ящика, с которого забираем прайсы: SMTP-хост выводится из MAIL_HOST
(imap.<домен> -> smtp.<домен>), логин и пароль — те же MAIL_USER / MAIL_PASS.

    ./venv/bin/python -m prices.mailout --to operator@example.ru \
        --file docs/prc/kaktus_msk_2026-08-06_unmatched.txt
"""
import argparse
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("MP_ENV_PATH", "/opt/mp-analytics/.env"))


def smtp_host():
    host = os.getenv("MAIL_HOST", "")
    return "smtp." + host.split(".", 1)[1] if host.startswith("imap.") else host


def send(to, subject, body, attachments=(), dry_run=False):
    """Письмо с вложениями. dry_run — собрать и показать, но не отправлять."""
    user, password = os.getenv("MAIL_USER"), os.getenv("MAIL_PASS")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    for path in attachments:
        path = Path(path)
        subtype = "csv" if path.suffix.lower() == ".csv" else "plain"
        msg.add_attachment(path.read_bytes(), maintype="text", subtype=subtype,
                           filename=path.name)
    if dry_run:
        return f"(сухой прогон) {smtp_host()} -> {to}: {subject}, вложений {len(attachments)}"
    with smtplib.SMTP_SSL(smtp_host(), 465, context=ssl.create_default_context()) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    return f"отправлено на {to}: {subject}, вложений {len(attachments)}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Отправка отчёта прогона на почту")
    ap.add_argument("--to", required=True)
    ap.add_argument("--file", required=True, action="append",
                    help="вложение; можно повторять")
    ap.add_argument("--subject")
    ap.add_argument("--body", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    files = [Path(f) for f in args.file]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        raise SystemExit("нет файла: " + ", ".join(missing))
    subject = args.subject or f"Необработанные товары: {', '.join(f.stem for f in files)}"
    print(send(args.to, subject, args.body, files, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
