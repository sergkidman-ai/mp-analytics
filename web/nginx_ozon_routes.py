# поток: mkt
"""web/nginx_ozon_routes.py — добавляет в nginx маршруты страниц маркетинга Ozon.

Зачем: в `/etc/nginx/sites-available/bi.metaverseworld.ru` явными `location =` прописаны
только `/marketing`, `/margin-control`, `/wb-bids` (+ их API). Всё остальное падает
в общий `location /` на `:8090` — это финансовый `web/app.py`, где страниц Ozon нет,
и браузер получает 404. Сами страницы на `:8092` живы и отвечают 200.

Ставим два ПРЕФИКСНЫХ блока (`^~ /ozon-` и `^~ /api/ozon-`), чтобы следующая страница
Ozon заработала без правки nginx.

Запуск (от root):  ./venv/bin/python web/nginx_ozon_routes.py
                   nginx -t && systemctl reload nginx
Идемпотентно: повторный запуск ничего не делает.
"""
import pathlib
import sys

CONF = pathlib.Path("/etc/nginx/sites-available/bi.metaverseworld.ru")
ANCHOR = "    # Возврат из СберБизнес ID."
BLOCK = """    # Маркетинг Ozon (домен mkt) — страницы и API одним префиксом: /ozon-economics,
    # /ozon-margin-control, /ozon-bids, /ozon-queries и то, что появится дальше. Без этого
    # блока они падали в `location /` на :8090 (финансовый app.py) и отдавали 404.
    location ^~ /ozon- {
        auth_basic "Пульт бизнеса";
        auth_basic_user_file /etc/nginx/.htpasswd-bi;
        proxy_pass http://127.0.0.1:8092;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
    location ^~ /api/ozon- {
        auth_basic "Пульт бизнеса";
        auth_basic_user_file /etc/nginx/.htpasswd-bi;
        proxy_pass http://127.0.0.1:8092;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }

"""


def main():
    if not CONF.exists():
        sys.exit(f"нет файла {CONF}")
    conf = CONF.read_text()
    if "location ^~ /ozon-" in conf:
        print("маршруты Ozon уже есть — ничего не меняю")
        return
    if ANCHOR not in conf:
        sys.exit(f"не нашёл якорь «{ANCHOR}» — вставить блок вручную перед /sber/callback")
    CONF.with_suffix(".bak").write_text(conf)
    CONF.write_text(conf.replace(ANCHOR, BLOCK + ANCHOR, 1))
    print(f"добавлено 2 блока; бэкап — {CONF.with_suffix('.bak')}")
    print("дальше:  nginx -t && systemctl reload nginx")


if __name__ == "__main__":
    main()
