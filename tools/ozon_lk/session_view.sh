#!/bin/bash
# поток: rev
# Показать серверный браузер человеку: виртуальный экран + VNC только на localhost.
#
# Зачем. Вход в ЛК Ozon подтверждается СМС, то есть первый раз войти обязан человек. Браузер при
# этом должен остаться на сервере — иначе профиль будет не тот, ради которого всё затевается.
# Xvfb даёт экран, x11vnc отдаёт его картинку, SSH-туннель доносит до вашего компьютера.
#
# Безопасность: x11vnc слушает ТОЛЬКО 127.0.0.1 (-localhost), наружу порт не открыт. Попасть на
# него можно исключительно через SSH-туннель, то есть пройдя вашу же SSH-аутентификацию. Поэтому
# отдельного VNC-пароля нет: он был бы вторым слабым секретом поверх сильного.
#
#   ./tools/ozon_lk/session_view.sh start   — поднять экран и показ
#   ./tools/ozon_lk/session_view.sh stop    — погасить всё
set -u
DISP=":99"
PORT=5900
GEOM="1440x900x24"

start() {
    pgrep -f "Xvfb ${DISP}" >/dev/null || { Xvfb ${DISP} -screen 0 ${GEOM} >/dev/null 2>&1 & sleep 2; }
    pgrep -f "x11vnc.*${DISP}" >/dev/null || {
        x11vnc -display ${DISP} -localhost -nopw -forever -shared -rfbport ${PORT} \
               -quiet >/dev/null 2>&1 &
        sleep 2
    }
    pgrep -f "Xvfb ${DISP}" >/dev/null && echo "  экран ${DISP} поднят" || { echo "  ОШИБКА: Xvfb не стартовал"; exit 1; }
    ss -ltn "sport = :${PORT}" | grep -q "127.0.0.1:${PORT}" \
        && echo "  показ слушает 127.0.0.1:${PORT} (наружу закрыт)" \
        || { echo "  ОШИБКА: x11vnc не слушает"; exit 1; }
    cat <<TXT

  Дальше на СВОЁМ компьютере:
    1) прокинуть туннель:   ssh -L ${PORT}:127.0.0.1:${PORT} root@<адрес сервера>
    2) открыть VNC-клиент и подключиться к  127.0.0.1:${PORT}
       (macOS: Finder → ⌘K → vnc://127.0.0.1:${PORT} · Windows: UltraVNC / TightVNC / RealVNC)

  Здесь, в этой же сессии, запустить браузер на этом экране:
    DISPLAY=${DISP} ./venv/bin/python tools/ozon_lk/recon.py --login

  Вы увидите настоящее окно браузера, войдёте в ЛК руками (логин, пароль, СМС) — профиль
  сохранится на сервере. После входа погасите показ:  ./tools/ozon_lk/session_view.sh stop
TXT
}

stop() {
    pkill -f "x11vnc.*${DISP}" 2>/dev/null && echo "  показ выключен" || echo "  показ не был запущен"
    pkill -f "Xvfb ${DISP}" 2>/dev/null && echo "  экран погашен" || echo "  экран не был поднят"
}

case "${1:-}" in
    start) start ;;
    stop)  stop ;;
    *) echo "Использование: $0 start|stop"; exit 2 ;;
esac
