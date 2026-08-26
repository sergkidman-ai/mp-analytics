# поток: rev
"""Разведка личного кабинета Ozon — ТОЛЬКО ЧТЕНИЕ. Ничего не отправляет и не меняет.

Зачем. `/v1/review/*` закрыт навсегда (403, Premium Plus не будет ни у одного юрлица), поэтому
отзывы Ozon можно вести только через ЛК. Разведка 23.08.2026 показала: рвётся не скрипт, а
сессия — из-за смены IP/ASN, дампа cookies вместо профиля и эмуляции внутренних запросов вместо
кликов. Отсюда схема: постоянный профиль браузера на машине сотрудника, логин один раз руками.
Этот скрипт — первый шаг: убедиться, что профиль живёт, и снять селекторы формы ответа.

Скрипт автономный: ни БД, ни `.env`, ни доступа к серверу ему не нужно — он рассчитан на ноутбук.

ЧЕГО ОН НЕ ДЕЛАЕТ, ПО УСТРОЙСТВУ: не печатает текст в поля, не нажимает кнопки отправки, не
трогает статусы отзывов. В коде нет ни одного `fill()`, ни одного `click()` по кнопке отправки;
`_assert_readonly()` роняет прогон, если такой вызов появится при правке файла.

Запуск (см. README.md рядом):
    python recon.py --login                      # один раз: открыть браузер и войти руками
    python recon.py --review-url "https://seller.ozon.ru/app/reviews?review_id=019eff22-..."
"""
import argparse
import pathlib
import re
import sys
import datetime as dt

LK = "https://seller.ozon.ru"
REVIEWS = LK + "/app/reviews"
DEFAULT_PROFILE = pathlib.Path.home() / ".ozon-lk-profile"
DEFAULT_OUT = pathlib.Path.home() / "ozon-lk-recon"

# Всё, к чему прикасаться запрещено. Список — для человека и для проверки ниже.
FORBIDDEN = ("отправить", "ответить", "опубликовать", "сохранить", "submit", "send")


def _assert_readonly():
    """Страховка от будущей правки: в этом файле не должно быть ввода и отправки.

    Имена методов склеиваются из кусков нарочно — записанные целиком, они нашли бы сами себя.
    """
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    for name in ("fill", "type", "press", "set_input_files", "click"):
        bad = "." + name + "("
        if bad in src:
            sys.exit(f"recon.py обязан быть read-only, а в нём есть {bad} — прогон остановлен")


def _stamp():
    return dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _log(out, name, text):
    p = out / name
    p.write_text(text, encoding="utf-8")
    return p


def _probe(page, selectors):
    """Сколько узлов даёт каждый селектор-кандидат. Так выясняем, чем цепляться за вёрстку."""
    res = {}
    for s in selectors:
        try:
            res[s] = page.locator(s).count()
        except Exception as e:
            res[s] = f"ошибка: {type(e).__name__}"
    return res


def run(args):
    from playwright.sync_api import sync_playwright

    out = pathlib.Path(args.out) / _stamp()
    out.mkdir(parents=True, exist_ok=True)
    profile = pathlib.Path(args.profile)
    fresh = not profile.exists()
    report = [f"# Разведка ЛК Ozon — {_stamp()}", "",
              f"профиль: `{profile}` ({'создан сейчас' if fresh else 'существующий'})", ""]
    xhr = []

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,                 # антибот: только видимый браузер и живой профиль
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # Пишем ТОЛЬКО метод, URL и статус. Тела запросов и заголовки не трогаем: там сессия.
        page.on("response", lambda r: xhr.append(
            (r.request.method, re.sub(r"\?.*$", "", r.url)[:140], r.status))
            if "/api/" in r.url or "graphql" in r.url else None)

        if args.login:
            page.goto(LK, wait_until="domcontentloaded")
            print("Браузер открыт. Войдите в ЛК руками (логин, пароль, СМС), дойдите до раздела")
            print("«Отзывы» и вернитесь сюда. Профиль сохранится — второй раз входить не нужно.")
            input("Готово? Нажмите Enter, чтобы закрыть браузер: ")
            ctx.close()
            print(f"Профиль сохранён: {profile}")
            return

        page.goto(REVIEWS, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        url_now = page.url
        alive = "/app/" in url_now and "login" not in url_now.lower()
        report += [f"после перехода на «Отзывы» URL: `{url_now}`",
                   f"**сессия жива: {'ДА' if alive else 'НЕТ — нужен перелогин'}**", ""]
        page.screenshot(path=str(out / "01_reviews.png"), full_page=False)

        if not alive:
            _log(out, "recon.md", "\n".join(report))
            ctx.close()
            print("Сессия не жива — запустите с --login и войдите руками.")
            print(f"Отчёт: {out}")
            return

        cand_list = ["[data-widget]", "[class*='review']", "[class*='Review']",
                     "article", "[data-testid]", "textarea"]
        report += ["## Список отзывов — сколько узлов дают селекторы-кандидаты", ""]
        report += [f"- `{k}` → {v}" for k, v in _probe(page, cand_list).items()] + [""]

        if args.review_url:
            page.goto(args.review_url, wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            page.screenshot(path=str(out / "02_review.png"), full_page=False)
            cand_form = ["textarea", "[contenteditable='true']", "button",
                         "[class*='answer']", "[class*='Answer']", "[class*='reply']"]
            report += ["## Карточка отзыва — форма ответа", "",
                       f"URL: `{args.review_url}`", ""]
            report += [f"- `{k}` → {v}" for k, v in _probe(page, cand_form).items()] + [""]
            btns = []
            for i in range(min(page.locator("button").count(), 40)):
                try:
                    t = (page.locator("button").nth(i).inner_text() or "").strip()
                except Exception:
                    t = ""
                if t:
                    btns.append(t[:60])
            report += ["### Кнопки на странице (текстом)", ""]
            report += [f"- {b}{'   ← ОТПРАВКА, не трогать' if any(f in b.lower() for f in FORBIDDEN) else ''}"
                       for b in btns] + [""]
            _log(out, "review.html", page.content())
            report += ["Полный HTML карточки: `review.html` (для разбора вёрстки офлайн).", ""]

        report += ["## Внутренние запросы ЛК (метод · URL · статус)", "",
                   "Разведка 23.08 предупреждала: эмуляция этих запросов вместо кликов ловится",
                   "антиботом быстрее всего. Список нужен для понимания, а не для повторения.", ""]
        seen = []
        for m, u, s in xhr:
            if (m, u) not in [(a, b) for a, b, _ in seen]:
                seen.append((m, u, s))
        report += [f"- `{m}` {u} → {s}" for m, u, s in seen[:40]] + [""]

        ctx.close()

    p = _log(out, "recon.md", "\n".join(report))
    print(f"Готово. Отчёт: {p}")
    print("Пришлите на сервер файлы recon.md и review.html — по ним соберу транспорт.")


if __name__ == "__main__":
    _assert_readonly()
    ap = argparse.ArgumentParser(description="Разведка ЛК Ozon, только чтение")
    ap.add_argument("--login", action="store_true", help="разовый вход руками, сохранить профиль")
    ap.add_argument("--review-url", help="ссылка на конкретный отзыв из отчёта")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE), help="папка постоянного профиля")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="куда класть отчёт и скриншоты")
    a = ap.parse_args()
    if not a.login and not a.review_url:
        print("Нечего делать: укажите --login (первый запуск) или --review-url.")
        sys.exit(2)
    run(a)
