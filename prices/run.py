# поток: prc
# -*- coding: utf-8 -*-
"""
CLI загрузки прайса поставщика в МойСклад.

    ./venv/bin/python -m prices.run --supplier colortek              # сухой прогон (по умолчанию)
    ./venv/bin/python -m prices.run --supplier colortek --apply      # запись в МС
    ./venv/bin/python -m prices.run --supplier colortek --file p.xlsx --no-db

По умолчанию НИЧЕГО не пишет: запись в МойСклад включается явным --apply. Отчёты
(пропущенные строки, ненайденные товары) пишутся всегда — они и есть вход для шага 2.
"""
import argparse
import csv
import sys
import datetime as dt
from pathlib import Path

from . import anomaly, novelty
from .cbr import effective_rate
from .loader import (SKIP_REASONS, apply_to_ms, build_docs, card_updates,
                     classify, existing_docs, now_msk, summarize)
from .parser import parse
from .profiles import get_profile

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "prc"


def read_source(profile, file_path):
    """(bytes прайса, имя файла, откуда взят)."""
    if file_path:
        path = Path(file_path)
        return path.read_bytes(), path.name, "file"
    from .mailbox import fetch_latest_price          # импорт здесь: без почты работает --file
    letter = fetch_latest_price(profile.mail_folder, pattern=profile.file_pattern)
    if not letter:
        raise SystemExit(f"в папке «{profile.mail_folder}» нет письма с прайсом")
    return letter["content"], letter["filename"], "mail"


def write_reports(profile, moment, ready, skipped, watch, out_dir):
    """Отчёты прогона: все пропущенные строки + ненайденные в формате внешнего загрузчика."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = moment.strftime("%Y-%m-%d")
    skipped_path = out_dir / f"{profile.key}_{stamp}_skipped.csv"
    with skipped_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["строка", "артикул", "наименование", "остаток", "кол-во",
                         "цена прайса", "причина", "пояснение", "наименование МС",
                         "группа", "подгруппа"])
        for row in skipped:
            writer.writerow([row["row"], row["article"], row["name"], row["stock_raw"],
                             row["qty"] or "", row["price_raw"] or "", row["reason"],
                             SKIP_REASONS.get(row["reason"], ""), row.get("ms_name", ""),
                             row.get("group", ""), row.get("subgroup", "")])

    # Формат внешнего загрузчика (name;price;quantity;msId;defective;Barcode;sku) —
    # чтобы список новинок читался теми же глазами, что и «Необработанные товары МС».
    unmatched = [r for r in skipped if r["reason"] in ("not_found", "ambiguous")]
    unmatched_path = out_dir / f"{profile.key}_{stamp}_unmatched.txt"
    with unmatched_path.open("w", encoding="utf-8") as fh:
        for row in unmatched:
            price = row["price_raw"] if row["price_raw"] not in (None, "") else ""
            fh.write(f"{row['name']};{price};{row['qty'] or ''};;;;{row['article']}\n")

    # Список наблюдения: неполные цветовые комплекты. Не новинки и не брак — ждём,
    # пока поставщик дозаведёт цвета (правило 5), поэтому отдельным файлом.
    watch_path = out_dir / f"{profile.key}_{stamp}_watch.csv"
    with watch_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["артикул", "наименование", "цена прайса", "не хватает цветов",
                         "группа", "подгруппа"])
        for row in watch:
            writer.writerow([row["article"], row["name"], row["price_raw"] or "",
                             row.get("missing", ""), row.get("group", ""),
                             row.get("subgroup", "")])
    return skipped_path, unmatched_path, len(unmatched), watch_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Загрузка прайса поставщика в МойСклад")
    ap.add_argument("--supplier", required=True, help="ключ профиля, например colortek")
    ap.add_argument("--apply", action="store_true", help="писать в МойСклад (иначе сухой прогон)")
    ap.add_argument("--file", help="локальный файл прайса вместо письма")
    ap.add_argument("--no-cards", action="store_true", help="не обновлять описание/закупочную в карточках")
    ap.add_argument("--no-db", action="store_true", help="не писать журнал в Postgres")
    ap.add_argument("--out", default=str(OUT_DIR), help="каталог отчётов")
    ap.add_argument("--ignore-anomalies", action="store_true",
                    help="грузить, несмотря на блокировку по аномалиям цены (осознанно)")
    args = ap.parse_args(argv)

    profile = get_profile(args.supplier)
    moment = now_msk()
    content, filename, source_kind = read_source(profile, args.file)

    rows, header_row = parse(content, profile)
    rate, rate_date = effective_rate(moment.date(), profile.currency, profile.markup)
    ready, skipped = classify(rows, profile, rate)

    base = anomaly.baseline(profile, use_db=not args.no_db)
    ready, dropped, flags, anom = anomaly.screen(ready, profile, base)
    skipped += dropped

    # Чёрный список: артикулы, которые уже были в «необработанных» и человек их забраковал.
    # Из загрузки они не выбывают (в МС их всё равно нет) — уходит только повторный показ.
    black_hits = 0
    if not args.no_db:
        from . import blacklist
        skipped, black_hits = blacklist.mark(skipped, blacklist.load_set())

    # Отсев новинок по правилам ассортимента (объём заправки, промывка, комплектность цветов).
    # Комплект считаем по всему прайсу, кроме отсечённых категорий: цвет мог уже быть у нас.
    universe = ready + [r for r in skipped if r["reason"] != "category_off"]
    skipped, novelty_stats, watch = novelty.screen(skipped, universe)

    docs = build_docs(ready, profile, moment)
    stale = existing_docs(profile)
    updates = [] if args.no_cards else card_updates(ready, profile)

    skipped_path, unmatched_path, unmatched_n, watch_path = write_reports(
        profile, moment, ready, skipped, watch, Path(args.out))
    anomaly_path = anomaly.write_report(
        flags, Path(args.out) / f"{profile.key}_{moment:%Y-%m-%d}_anomalies.csv")
    info = summarize(ready, skipped, docs, stale, updates, rate, rate_date, profile, filename)

    print(f"[{profile.title}] {filename} — строк {info['rows_total']} (шапка в строке {header_row})")
    print(f"  курс: {rate:.6f} ₽ / {profile.currency} (ЦБ на {rate_date} × {profile.markup})")
    print(f"  к загрузке: {info['rows_loaded']} позиций на {info['sum_rub']:,.2f} ₽ "
          f"в {info['docs']} документах")
    for reason, count in sorted(info["skipped_by_reason"].items(), key=lambda kv: -kv[1]):
        print(f"  пропущено {count:>5} — {SKIP_REASONS.get(reason, reason)}")
    print(f"  прошлых документов группы на «Удаленном складе»: {info['stale_docs']}")
    print(f"  карточек к обновлению: {info['card_updates']}")
    median = (f"{anom['median_ratio']:.3f}" if anom["median_ratio"] is not None
              else "нет базы сравнения")
    print(f"  аномалии цены: {anom['flags']} (снято с загрузки {anom['dropped']}); "
          f"сверено с нашей прошлой закупочной {anom['compared']}, медиана {median}")
    for reason in anomaly.reasons(anom, profile):
        print(f"  ⚠ {reason}")
    print(f"  отчёты: {skipped_path.name}, {unmatched_path.name} ({unmatched_n} строк "
          f"новинок, из них снято чёрным списком {black_hits}), {anomaly_path.name}")
    if novelty_stats:
        print(f"  отсев новинок по правилам ассортимента: "
              + "; ".join(f"{novelty.NOVELTY_REASONS[k][0]} — {v}"
                          for k, v in sorted(novelty_stats.items(), key=lambda kv: -kv[1])))
    print(f"  список наблюдения (неполные комплекты): {len(watch)} → {watch_path.name}")

    status, error = "ok", None
    if args.apply:
        if not ready:
            raise SystemExit("нечего грузить — запись в МойСклад отменена")
        if anom["blocked"] and not args.ignore_anomalies:
            raise SystemExit("запись отменена проверкой аномалий цены: "
                             + "; ".join(anomaly.reasons(anom, profile))
                             + f". Смотреть {anomaly_path}; грузить всё равно — --ignore-anomalies")
        print("  --- запись в МойСклад ---")
        try:
            apply_to_ms(docs, stale, updates, moment=moment, log=print)
        except Exception as exc:
            status, error = "error", str(exc)[:2000]
            print(f"  ОШИБКА: {error}")
    else:
        print("  сухой прогон: в МойСклад ничего не записано (нужен --apply)")

    if not args.no_db:
        from . import journal
        load_id = journal.save({
            "supplier_key": profile.key, "load_date": moment.date(), "moment": moment,
            "source_file": filename, "source_kind": source_kind, "currency": profile.currency,
            "rate": rate, "rate_date": rate_date,
            "rows_total": info["rows_total"], "rows_loaded": info["rows_loaded"],
            "rows_skipped": info["rows_skipped"],
            "docs": info["docs"] if args.apply else 0,
            "stale_docs": info["stale_docs"] if args.apply else 0,
            "card_updates": info["card_updates"] if args.apply else 0,
            "sum_rub": info["sum_rub"], "dry_run": not args.apply,
            "status": status, "error": error,
        }, ready, skipped)
        print(f"  журнал: prc_price_load #{load_id}")

    return 1 if status == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
