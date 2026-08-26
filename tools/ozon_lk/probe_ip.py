# поток: rev
"""Проверка канала до Ozon — пускает нас этот адрес или нет. ТОЛЬКО ЧТЕНИЕ.

Зачем. 26.08.2026 выяснилось: с сервера (Beget AS198610) Ozon не открывается вообще — ни ЛК, ни
покупательский сайт. Механика защиты: 307-редирект ставит cookie `__Secure-ETC` и гоняет по кругу
`?__rr=1..5`, дальше отдаёт JS-челлендж `fab_chlg_…`. У пропущенного посетителя челлендж решается
сам; у забракованного адреса — нет, сколько ни жди.

Этот скрипт меряет любой канал одинаково: прямой, через прокси/релей, с мобильного модема.
Прогон занимает секунды и ничего никуда не отправляет.

    python probe_ip.py                                  # прямой канал
    python probe_ip.py --proxy socks5://ХОСТ:ПОРТ       # через релей
    python probe_ip.py --proxy ... --browser            # + настоящий Chromium (нужен playwright)

Адрес прокси передаётся аргументом или переменной окружения OZON_PROBE_PROXY — в код не вписывать.
"""
import argparse
import json
import os
import re
import sys
import urllib.request

IPINFO = "https://ipinfo.io/json"
SITES = ["https://www.ozon.ru/", "https://seller.ozon.ru/"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/151.0.0.0 Safari/537.36")
BLOCK_MARKS = ("__rr=", "fab_chlg", "не робот", "нет соединения")
# Наш ключ идёт первым: в .env адрес мобильного порта лежит как OZON_PROXY_URL.
PROXY_KEYS = ("OZON_PROXY_URL", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")


def _opener(proxy):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _pw_proxy(proxy):
    """Playwright ждёт логин и пароль отдельными полями, а покупные прокси дают их прямо в адресе."""
    m = re.match(r"^(\w+)://(?:([^:@/]+):([^@/]*)@)?(.+)$", proxy)
    if not m:
        return {"server": proxy}
    scheme, user, pwd, host = m.groups()
    out = {"server": f"{scheme}://{host}"}
    if user:
        out["username"], out["password"] = user, pwd or ""
    return out


def _get(opener, url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=timeout) as r:
        return r.geturl(), r.status, r.read(60000).decode("utf-8", "replace")


def whoami(opener):
    try:
        _, _, body = _get(opener, IPINFO, 15)
        d = json.loads(body)
        return f"{d.get('ip')} · {d.get('org')} · {d.get('city')}, {d.get('country')}"
    except Exception as e:
        return f"не определился ({type(e).__name__})"


def check_site(opener, url):
    try:
        final, status, body = _get(opener, url)
    except Exception as e:
        msg = str(e)
        # Исчерпанный лимит редиректов = та самая петля ?__rr=1..5, а не сетевая ошибка.
        if "307" in msg or "redirect" in msg.lower():
            return False, "БЛОК (петля редиректов ?__rr — адрес забракован на входе)"
        return False, f"сеть: {type(e).__name__} {msg[:50]}"
    title = (re.search(r"<title>([^<]*)</title>", body) or [None, ""])[1].strip()
    hit = [m for m in BLOCK_MARKS if m in final or m in body]
    if hit:
        return False, f"БЛОК ({', '.join(hit)}) · {title[:40]}"
    return True, f"HTTP {status} · {title[:40] or 'без title'}"


def check_browser(proxy):
    """Настоящий Chromium: единственная честная проверка — JS-челлендж решается или нет."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright не установлен — шаг пропущен"
    opts = {"proxy": _pw_proxy(proxy)} if proxy else {}
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"], **opts)
        page = br.new_page(locale="ru-RU", timezone_id="Europe/Moscow",
                           viewport={"width": 1440, "height": 900}, user_agent=UA)
        try:
            page.goto("https://seller.ozon.ru/app/reviews", wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(15000)
            title, url = page.title(), page.url
            body = page.inner_text("body")[:300]
        except Exception as e:
            br.close()
            return False, f"{type(e).__name__}: {str(e)[:70]}"
        br.close()
    blocked = any(m in url or m in body or m in title for m in BLOCK_MARKS)
    return not blocked, f"{'БЛОК' if blocked else 'ПРОШЛИ'} · {title[:50]} · {url[:60]}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Пускает ли Ozon этот канал")
    ap.add_argument("--proxy", default=os.getenv("OZON_PROBE_PROXY"),
                    help="socks5://хост:порт или http://хост:порт (можно через OZON_PROBE_PROXY)")
    ap.add_argument("--proxy-file", help="файл вида KEY=value, взять адрес прокси оттуда "
                                         "(OZON_PROXY_URL / HTTPS_PROXY / HTTP_PROXY / ALL_PROXY); "
                                         "значение остаётся внутри скрипта и не печатается")
    ap.add_argument("--browser", action="store_true", help="дополнительно проверить настоящим Chromium")
    a = ap.parse_args()

    if a.proxy_file and not a.proxy:
        for ln in open(a.proxy_file, encoding="utf-8", errors="replace"):
            k, _, v = ln.strip().partition("=")
            if k.strip().upper() in PROXY_KEYS and v:
                a.proxy = v.strip().strip('"').strip("'")
                break
        if not a.proxy:
            sys.exit(f"в {a.proxy_file} нет HTTPS_PROXY/HTTP_PROXY/ALL_PROXY")

    if a.proxy and a.proxy.startswith("socks"):
        # urllib socks5 не умеет; у мобильных прокси всегда есть и HTTP-порт — берите его.
        print("ВНИМАНИЕ: для этой проверки нужен HTTP(S)-порт прокси, socks5 не поддержан.")

    op = _opener(a.proxy)
    print("канал:  ", "через прокси" if a.proxy else "прямой")
    print("нас видно как:", whoami(op))
    ok_all = True
    for s in SITES:
        ok, msg = check_site(op, s)
        ok_all &= ok
        print(f"  {'OK ' if ok else 'НЕТ'} {s:<28} {msg}")
    if a.browser:
        ok, msg = check_browser(a.proxy)
        print(f"  {'OK ' if ok else ('—  ' if ok is None else 'НЕТ')} chromium{'':<20} {msg}")
        ok_all = ok_all and bool(ok)
    print("\nВЕРДИКТ:", "канал годится для ЛК" if ok_all else
          "канал заблокирован — браузерный путь к Ozon с него не начинать")
    sys.exit(0 if ok_all else 1)
