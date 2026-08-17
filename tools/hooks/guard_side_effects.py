#!/usr/bin/env python3
# поток: infra
"""PreToolUse-хук: SIDE-EFFECT / PRODUCTION GUARD.

Последняя линия защиты. Работает ДАЖЕ когда permissions отключены режимом запуска
(`--dangerously-skip-permissions` ночного режима permission-правила игнорирует, хуки — нет).

Модель автономности (чем выше ущерб и хуже обратимость — тем меньше автономности):

    AUTONOMOUS         — молча пропускаем (решают permissions из settings.json)
    AUTONOMOUS + LOG   — пропускаем, но пишем строку в logs/guard_side_effects.jsonl
    ASK                — поднимаем до вопроса человеку, даже если permissions разрешают
    HARD DENY          — блокируем; снять можно только правкой этого файла

Хук НЕ выдаёт решение "allow": он умеет только ПОДНИМАТЬ планку (ask/deny). Поэтому он
не перекрывает deny-правила пользователя и композируется с settings.json.

Что защищаем (классы):
    secret_read       чтение/печать значений секретов (.env, secrets/, credentials, ключи)
    secret_exfil      передача секрета наружу (в т.ч. секрет литералом в командной строке)
    bank_prod         боевые банковские действия: платёжки, запись в МС, ротация секретов
    mp_write          записи на маркетплейсы: цены, ставки, карточки/габариты, ответы клиентам
    db_destructive    необратимые операции в БД (с разбором цели: prod / dev / read-only)
    git_dangerous     force-push, переписывание истории, коммит секретов, смена ветки
                      в ОБЩЕМ чекауте (CLAUDE.md правило 14)
    fs_destructive    массовые удаления вне песочницы, снос pgdata/secrets/venv/.git
    service_prod      рестарт/останов боевых сервисов и контейнера БД
    obfuscation       `curl | sh`, eval/base64-исполнение — распознанный high-risk => fail closed

ПРИНЦИПЫ (важные, не менять не подумав):
  * Приложению ПОЛЬЗОВАТЬСЯ секретом из окружения не запрещаем: `./venv/bin/python
    collectors/wb.py` секретов в командной строке не несёт и проходит свободно.
    Запрещаем Клоду ЧИТАТЬ, ПЕЧАТАТЬ, ОТПРАВЛЯТЬ наружу и КОММИТИТЬ ЗНАЧЕНИЕ секрета.
  * curl'ы разные: GET к API и POST-«прочитать список» (у Ozon читающие ручки — POST)
    автономны; POST/PUT/PATCH/DELETE по ПИШУЩЕМУ пути — ask/deny.
  * `DELETE FROM` без разбора цели не блокируем: на dev/локальной базе — автономно,
    на боевой с WHERE — ask, без WHERE или DROP/TRUNCATE — deny.
  * Не смогли классифицировать нерелевантную команду — пропускаем (не мешаем работе).
    Но если распознали high-risk и при этом не смогли разобрать команду — fail closed.
  * В журнал пишем причину и ОБЕЗЗАРАЖЕННУЮ команду. Значения секретов не логируем никогда.

Тесты: tools/hooks/test_guard_side_effects.py (python3 -m unittest).
"""

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone

REPO = "/opt/mp-analytics"
LOG_PATH = os.path.join(REPO, "logs", "guard_side_effects.jsonl")

# ─────────────────────────── уровни ───────────────────────────
ALLOW = "allow"          # молча пропустить
LOG = "log"              # пропустить + записать
ASK = "ask"              # спросить человека
DENY = "deny"            # заблокировать

_RANK = {ALLOW: 0, LOG: 1, ASK: 2, DENY: 3}


class Verdict:
    __slots__ = ("tier", "cls", "reason")

    def __init__(self, tier, cls, reason):
        self.tier, self.cls, self.reason = tier, cls, reason

    def __repr__(self):  # для тестов
        return f"Verdict({self.tier},{self.cls},{self.reason!r})"


def worst(verdicts):
    """Самый строгий вердикт из списка (None, если пусто)."""
    real = [v for v in verdicts if v is not None]
    if not real:
        return None
    return max(real, key=lambda v: _RANK[v.tier])


# ─────────────────────── словари распознавания ───────────────────────

# Пути, значение которых Клод не должен видеть/печатать/отправлять/коммитить.
SECRET_PATH_RE = re.compile(
    r"(^|/)\.env($|[._-])"          # .env, .env.bak_*, .env.prod
    r"|(^|/)secrets(/|$)"
    r"|\.(key|pem|p12|pfx|jks|keystore)$"
    r"|\.credentials\.json"
    r"|(^|/)\.(aws|ssh)/"
    r"|id_rsa|id_ed25519|\.netrc|\.pgpass",
    re.I,
)

# Имена переменных-секретов (для $EXPANSION и `echo $X`).
SECRET_NAME_RE = re.compile(
    r"[A-Z0-9_]*(API_KEY|APIKEY|AUTHKEY|TOKEN|SECRET|PASSWORD|PASSWD|"
    r"CLIENT_KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*"
)

# Секрет ЛИТЕРАЛОМ: значение в «кредентальной позиции». Ловим ровно это,
# чтобы не путать ключи с git-sha и прочими длинными строками.
SECRET_LITERAL_RE = re.compile(
    r"(authkey|api[_-]?key|apikey|access[_-]?token|token|secret|password|passwd|"
    r"client[_-]?secret|bearer)\s*[=:]\s*[\"']?([A-Za-z0-9_\-\.]{12,})",
    re.I,
)

# Команды, которые ВЫВОДЯТ содержимое файла в контекст.
READERS = {
    "cat", "less", "more", "bat", "tac", "nl", "head", "tail", "strings",
    "xxd", "od", "hexdump", "base64", "sed", "awk", "grep", "rg", "egrep",
    "fgrep", "cut", "sort", "uniq", "tr", "jq", "vi", "vim", "nano", "view",
}
# Команды, безопасные над секретным файлом: не раскрывают значений.
SECRET_SAFE_CMDS = {
    "wc", "ls", "stat", "test", "file", "sha256sum", "md5sum", "sha1sum",
    "git", "chmod", "chown", "touch", "mkdir", "du", "find", "basename",
    "dirname", "realpath", "readlink",
}
COPIERS = {"cp", "mv", "ln", "install", "rsync", "scp", "tar", "zip", "unzip"}
NET_CMDS = {"curl", "wget", "http", "httpie", "nc", "ncat", "telnet", "scp",
            "sftp", "rsync", "ssh"}

# Хосты, куда наши секреты ходят ШТАТНО (это аутентификация, а не утечка).
KNOWN_API_HOSTS = (
    "wildberries.ru", "wb.ru", "ozon.ru", "yandex.ru", "yandex.net",
    "moysklad.ru", "alfabank.ru", "sberbank.ru", "sber.ru", "telegram.org",
    "github.com", "b2b-rapid1.ru", "thecartridge.ru", "anthropic.com",
    "deepseek.com", "openai.com", "localhost", "127.0.0.1",
)
MP_HOSTS = ("wildberries.ru", "wb.ru", "ozon.ru", "yandex.ru", "moysklad.ru")

# Пишущие пути маркетплейс-API (POST по ним = изменение витрины продавца).
MP_WRITE_PATH_RE = re.compile(
    r"/(prices?|discounts-prices-api|update|upload|import|create|delete|remove|"
    r"archive|bids?|cpm|answers?|reply|feedbacks?/answer|questions?/answer|"
    r"cards/update|content/v2/cards|product/import|stocks?/?(update|import)|"
    r"promotion|adv/v\d/(save|start|pause|stop|budget|deposit))",
    re.I,
)
HTTP_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Боевые банковские действия.
BANK_WRITE_RE = re.compile(
    r"alfa_payment_draft|payment_draft|alfa_pay|sber_pay|payment_send|"
    r"sber_secret_rotate|secret_rotate",
    re.I,
)
BANK_ANY_RE = re.compile(r"alfa_|sber_|/jp/v2/payments|baas\.alfabank", re.I)

# Скрипты, которые ПИШУТ на площадки.
MP_WRITE_SCRIPT_RE = re.compile(
    r"wb_card_content|ozon_dims|ozon_attributes|wb_bid_ladder|ozon_bids|"
    r"feedback_send|review_answers|question_answers|price_apply|dims_apply|"
    r"_push|_upload|_publish",
    re.I,
)

# Защищённые пути ФС: снос = потеря боевых данных.
PROTECTED_FS_RE = re.compile(
    r"(^|/)(pgdata|secrets|venv|\.git|incoming|dropbox|reports/data|node_modules)(/|$)"
    r"|(^|/)\.env"
    r"|^/(etc|var|usr|bin|sbin|boot|root|home|opt)/?$"
    r"|^/$|^~/?$",
)
# Песочницы, где сносить можно свободно.
SANDBOX_RE = re.compile(r"^(/tmp/|/var/tmp/|\./?scratch|.*/scratchpad/)")

# Боевые сервисы.
PROD_UNITS_RE = re.compile(
    r"mp-dashboard|mp-marketing|mp-postgres|dropbox-bot|nginx|postgres|docker", re.I
)

PROD_DB_RE = re.compile(r"mp_analytics|mp-postgres|5433|DATABASE_URL", re.I)
DEV_DB_RE = re.compile(r"(test|dev|tmp|scratch|sandbox|staging)[_-]?db|db[_-]?(test|dev)"
                       r"|:memory:|/tmp/.*\.(db|sqlite)", re.I)


# ─────────────────────────── утилиты ───────────────────────────

def redact(text):
    """Обеззараживание строки перед логом/сообщением: значения секретов не показываем."""
    if not text:
        return text
    out = SECRET_LITERAL_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    out = re.sub(r"(-u|--user)\s+\S+", r"\1 <redacted>", out)
    out = re.sub(r"(Bearer|Basic)\s+[A-Za-z0-9._\-]+", r"\1 <redacted>", out)
    return out[:400]


def split_segments(command):
    """Команда → список сегментов (по ; && || | и подстановкам $(...) / `...`)."""
    segments, degraded = [], False
    inner = re.findall(r"\$\(([^()]*)\)|`([^`]*)`", command)
    for a, b in inner:
        if a:
            segments.append(a)
        if b:
            segments.append(b)
    stripped = re.sub(r"\$\([^()]*\)|`[^`]*`", " ", command)
    for part in re.split(r"(?:\|\||&&|;|\||\n)", stripped):
        part = part.strip()
        if part:
            segments.append(part)
    return segments or [command], degraded


def parse(segment):
    """Сегмент → (env-присваивания dict, команда, аргументы list, разбор_деградировал)."""
    degraded = False
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.split()
        degraded = True
    env = {}
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        k, _, v = tokens[0].partition("=")
        env[k] = v
        tokens = tokens[1:]
    # `env VAR=x cmd` — обёртка, её снимаем; голый `env` (или `env VAR=x` без команды) —
    # это ДАМП окружения, его снимать нельзя, иначе детектор секретов ослепнет
    if tokens and os.path.basename(tokens[0]) == "env":
        rest = [t for t in tokens[1:] if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        if not rest:
            return env, "env", tokens[1:], degraded
    if tokens and os.path.basename(tokens[0]) in ("env", "timeout", "sudo", "nohup", "time"):
        drop = 1
        if os.path.basename(tokens[0]) == "timeout" and len(tokens) > 1:
            drop = 2
        tokens = tokens[drop:]
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            k, _, v = tokens[0].partition("=")
            env[k] = v
            tokens = tokens[1:]
    if not tokens:
        return env, "", [], degraded
    return env, os.path.basename(tokens[0]), tokens[1:], degraded


def hosts_of(text):
    return re.findall(r"https?://([A-Za-z0-9_.\-]+)", text)


def is_known_host(host):
    return any(host == h or host.endswith("." + h) for h in KNOWN_API_HOSTS)


def http_method(args, joined):
    for i, a in enumerate(args):
        if a in ("-X", "--request") and i + 1 < len(args):
            return args[i + 1].upper()
        if a.startswith("--request="):
            return a.split("=", 1)[1].upper()
    if any(a in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
                 "-F", "--form", "-T", "--upload-file", "--json") or
           a.startswith(("--data", "--form", "--json")) for a in args):
        return "POST"
    if "--head" in args or "-I" in args:
        return "HEAD"
    return "GET"


def dry_run_mode(env, args, joined):
    """→ 'dry' | 'apply' | None (не указано)."""
    if re.search(r"--dry[-_]?run|--no-apply|--check\b", joined, re.I):
        return "dry"
    if re.search(r"--apply\b|--send\b|--confirm\b|--yes\b|--force\b", joined, re.I):
        return "apply"
    for k, v in env.items():
        if re.search(r"APPLY|SEND|WRITE|PUSH", k, re.I):
            return "apply" if v not in ("0", "", "false", "no") else "dry"
        if re.search(r"DRY[_-]?RUN", k, re.I):
            return "dry" if v not in ("0", "", "false", "no") else "apply"
    return None


def is_mass(joined):
    if re.search(r"--all\b|--full\b|--batch\b|\*\.csv|\.csv\b|--from-file", joined, re.I):
        return True
    m = re.search(r"--limit[= ](\d+)", joined)
    return bool(m and int(m.group(1)) > 50)


# ─────────────────────────── детекторы ───────────────────────────

def d_secret_read(env, cmd, args, joined, tool):
    """Чтение/печать значений секретов."""
    secret_args = [a for a in args if SECRET_PATH_RE.search(a)]
    if secret_args:
        if cmd in READERS:
            return Verdict(DENY, "secret_read",
                           f"чтение значения секрета в контекст ({cmd} по {secret_args[0]}). "
                           "Приложение может пользоваться ключом из окружения, "
                           "но выводить его значение в сессию нельзя.")
        if cmd in COPIERS:
            return Verdict(ASK, "secret_read",
                           f"копирование секретного файла ({cmd} {secret_args[0]}) — "
                           "подтвердите назначение и права.")
        if cmd not in SECRET_SAFE_CMDS and cmd:
            return Verdict(ASK, "secret_read",
                           f"неизвестная операция над секретным файлом: {cmd} {secret_args[0]}")
    # дамп окружения
    if cmd in ("env", "printenv", "export", "set") and not args:
        return Verdict(DENY, "secret_read",
                       "дамп окружения печатает значения всех ключей в контекст.")
    if cmd in ("env", "printenv") and any(SECRET_NAME_RE.search(a) for a in args):
        return Verdict(DENY, "secret_read", "печать значения секретной переменной окружения.")
    if cmd in ("echo", "printf") and SECRET_NAME_RE.search(joined) and "$" in joined:
        return Verdict(DENY, "secret_read", "печать значения секрета через $-подстановку.")
    return None


def d_secret_exfil(command):
    """Передача секрета наружу. Считается по ВСЕЙ команде — переставить аргументы не поможет."""
    if not any(re.search(rf"(^|[\s|;&(]){c}(\s|$)", command) for c in NET_CMDS):
        return None
    hosts = hosts_of(command)
    unknown = [h for h in hosts if not is_known_host(h)]

    # 1) секрет литералом в командной строке — значение попадает в транскрипт
    m = SECRET_LITERAL_RE.search(command)
    if m:
        return Verdict(DENY, "secret_exfil",
                       f"секрет литералом в команде (параметр «{m.group(1)}»). "
                       "Значение осело бы в истории сессии и в permission-правилах. "
                       "Передавайте ключ переменной окружения из .env, а не текстом.")
    # 2) файл секрета скармливается сети
    if re.search(r"[@<]\s*[^\s]*(\.env|secrets/|\.key|\.pem|credentials)", command, re.I):
        return Verdict(DENY, "secret_exfil", "секретный файл передаётся в сетевой запрос.")
    # 3) $SECRET уходит на посторонний хост
    if SECRET_NAME_RE.search(command) and "$" in command and unknown:
        return Verdict(DENY, "secret_exfil",
                       f"секрет из окружения уходит на неизвестный хост: {unknown[0]}")
    return None


def d_bank(env, cmd, args, joined):
    if not BANK_ANY_RE.search(joined):
        return None
    prod = env.get("ALFA_ENV", "").lower() == "prod" or "prod" in joined.lower()
    if BANK_WRITE_RE.search(joined):
        return Verdict(DENY, "bank_prod",
                       "боевое банковское действие (платёжное поручение / ротация секрета). "
                       "Деньги и необратимость — вне автономии сессии.")
    if env.get("ALFA_MS_APPLY") not in (None, "0", "", "false"):
        return Verdict(DENY, "bank_prod",
                       "запись банковских операций в МойСклад (ALFA_MS_APPLY=1) — "
                       "изменяет боевой учёт.")
    if prod:
        return Verdict(ASK, "bank_prod",
                       "обращение к боевому контуру банка (чтение выписки) — подтвердите.")
    return Verdict(LOG, "bank_prod", "банковский скрипт в песочнице/dry-run.")


def d_mp_write(env, cmd, args, joined, command):
    verdicts = []
    # а) наши скрипты-писатели
    if MP_WRITE_SCRIPT_RE.search(joined):
        mode = dry_run_mode(env, args, joined)
        if mode == "dry":
            verdicts.append(Verdict(LOG, "mp_write", "dry-run записи на площадку — выполняется без отправки."))
        elif mode == "apply" and is_mass(joined):
            verdicts.append(Verdict(DENY, "mp_write",
                                    "МАССОВАЯ запись на маркетплейс (цены/ставки/карточки). "
                                    "Необратимо для витрины и кошелька; только по отдельной команде человека."))
        else:
            verdicts.append(Verdict(ASK, "mp_write",
                                    "запись на маркетплейс (карточки/габариты/ставки/ответы клиентам). "
                                    "CLAUDE.md: отправка изменений — только по прямой команде."))
    # б) прямые HTTP-запросы
    if cmd in ("curl", "wget", "http"):
        method = http_method(args, joined)
        for h in hosts_of(joined):
            if not any(h.endswith(m) for m in MP_HOSTS):
                continue
            if method not in HTTP_WRITE_METHODS:
                continue
            if MP_WRITE_PATH_RE.search(joined):
                tier = DENY if is_mass(joined) else ASK
                verdicts.append(Verdict(tier, "mp_write",
                                        f"{method} по пишущему пути {h} — изменение витрины/ставок/карточек."))
            else:
                # у Ozon читающие ручки тоже POST — это не запись
                verdicts.append(Verdict(LOG, "mp_write",
                                        f"{method} к {h} по читающему пути — считаем чтением."))
    return worst(verdicts)


def d_db(env, cmd, args, joined, command):
    # Нужен КОНТЕКСТ ИСПОЛНЕНИЯ SQL, а не просто слово в тексте: иначе сообщение коммита
    # или документация со словом TRUNCATE блокировали бы работу (ложное срабатывание).
    runner = re.search(r"\b(psql|pg_dump|pg_restore|dropdb|createdb|sqlite3|"
                       r"\.execute\(|cursor|sqlalchemy|alembic|DATABASE_URL)\b", command)
    destructive_kw = re.search(
        r"\b(DROP|TRUNCATE|DELETE\s+FROM|UPDATE\s+\w+\s+SET|ALTER\s+TABLE)\b", command, re.I)
    if not runner:
        return None
    if not destructive_kw and not re.search(r"\b(psql|dropdb|sqlite3)\b", command):
        return None
    sql = command
    prod = bool(PROD_DB_RE.search(command)) or not bool(DEV_DB_RE.search(command))
    if DEV_DB_RE.search(command):
        prod = False
    if not prod:
        return Verdict(LOG, "db_destructive", "операция над не-боевой базой.")
    if re.search(r"\bDROP\s+(DATABASE|SCHEMA)\b|\bdropdb\b", sql, re.I):
        return Verdict(DENY, "db_destructive", "снос боевой базы/схемы — невосстановимо.")
    if re.search(r"\bTRUNCATE\b", sql, re.I):
        return Verdict(DENY, "db_destructive", "TRUNCATE боевой таблицы — данные не вернуть.")
    if re.search(r"\bDROP\s+TABLE\b", sql, re.I):
        return Verdict(DENY, "db_destructive", "DROP TABLE в боевой базе.")
    if re.search(r"\b(DELETE\s+FROM|UPDATE)\b", sql, re.I):
        if re.search(r"\bWHERE\b", sql, re.I):
            return Verdict(ASK, "db_destructive",
                           "изменение/удаление строк в боевой базе (есть WHERE) — подтвердите.")
        return Verdict(DENY, "db_destructive",
                       "DELETE/UPDATE без WHERE в боевой базе — снесёт таблицу целиком.")
    if re.search(r"\bALTER\s+TABLE\b", sql, re.I):
        return Verdict(ASK, "db_destructive", "изменение схемы боевой базы.")
    return Verdict(LOG, "db_destructive", "SQL к боевой базе (чтение).")


def d_git(env, cmd, args, joined, cwd):
    if cmd != "git":
        return None
    # сабкоманду ищем, пропуская глобальные флаги git И ИХ ЗНАЧЕНИЯ
    # (иначе `git -C /path add .env` даёт sub='/path' и правило не срабатывает)
    GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    sub, target_dir, i = "", cwd, 0
    while i < len(args):
        a = args[i]
        if a in GLOBAL_WITH_VALUE:
            if a == "-C" and i + 1 < len(args):
                target_dir = args[i + 1]
            i += 2
            continue
        if a.startswith("-"):
            if a.startswith("--git-dir=") or a.startswith("--work-tree="):
                i += 1
                continue
            i += 1
            continue
        sub = a
        break
    shared = os.path.realpath(target_dir or "") == REPO

    if sub == "push" and any(a in ("-f", "--force") or a.startswith("--force")
                             or a.startswith("+") for a in args):
        return Verdict(DENY, "git_dangerous", "force-push переписывает опубликованную историю.")
    if sub in ("filter-branch", "filter-repo") or "--amend" in args and "push" in args:
        return Verdict(DENY, "git_dangerous", "переписывание истории репозитория.")
    if sub == "push":
        return Verdict(ASK, "git_dangerous", "публикация во внешний репозиторий (GitHub).")
    if sub == "reset" and "--hard" in args:
        return Verdict(ASK, "git_dangerous",
                       "reset --hard теряет незакоммиченную работу (в проекте принят --keep).")
    if sub == "clean" and any("f" in a for a in args if a.startswith("-")):
        return Verdict(ASK, "git_dangerous", "git clean -f удаляет неотслеживаемые файлы.")
    if sub == "branch" and "-D" in args:
        return Verdict(ASK, "git_dangerous", "принудительное удаление ветки.")
    if sub in ("checkout", "switch"):
        is_path_op = "--" in args or any(a in ("-p", "--patch") for a in args)
        if shared and not is_path_op:
            return Verdict(DENY, "git_dangerous",
                           "смена ветки в ОБЩЕМ чекауте /opt/mp-analytics (CLAUDE.md правило 14): "
                           "HEAD уедет под всеми параллельными сессиями. Работайте в worktree.")
    if sub == "add":
        secret_args = [a for a in args if SECRET_PATH_RE.search(a)]
        if secret_args:
            return Verdict(DENY, "git_dangerous",
                           f"попытка закоммитить секрет ({secret_args[0]}).")
        if any(a in ("-A", "--all", ".", "-u") for a in args):
            return Verdict(ASK, "git_dangerous",
                           "git add -A/. захватывает всё дерево (в проекте коммитим только целевые файлы).")
    return None


def d_fs(env, cmd, args, joined):
    if cmd == "rm":
        recursive = any(re.match(r"^-[a-zA-Z]*[rRf]", a) for a in args)
        targets = [a for a in args if not a.startswith("-")]
        for t in targets:
            if PROTECTED_FS_RE.search(t):
                return Verdict(DENY, "fs_destructive",
                               f"удаление защищённого пути ({t}): боевые данные/секреты/репозиторий.")
        if recursive:
            if all(SANDBOX_RE.search(t) for t in targets) and targets:
                return Verdict(LOG, "fs_destructive", "рекурсивное удаление в песочнице.")
            return Verdict(ASK, "fs_destructive",
                           "рекурсивное удаление вне песочницы — подтвердите цель.")
        return None
    if cmd == "find" and "-delete" in args:
        return Verdict(ASK, "fs_destructive", "find -delete: массовое удаление по маске.")
    if cmd in ("shred", "mkfs", "fdisk", "wipefs"):
        return Verdict(DENY, "fs_destructive", f"{cmd}: необратимое разрушение данных/ФС.")
    if cmd == "dd" and any(a.startswith("of=") for a in args):
        return Verdict(DENY, "fs_destructive", "dd of=: перезапись устройства/файла напрямую.")
    if cmd == "chmod" and "-R" in args and any(a in ("777", "-R") for a in args):
        targets = [a for a in args if a.startswith("/")]
        if any(PROTECTED_FS_RE.search(t) for t in targets):
            return Verdict(ASK, "fs_destructive", "рекурсивная смена прав на защищённом пути.")
    return None


def d_service(env, cmd, args, joined):
    if cmd == "systemctl":
        action = args[0] if args else ""
        if action in ("restart", "stop", "disable", "mask", "kill") and PROD_UNITS_RE.search(joined):
            return Verdict(ASK, "service_prod", f"{action} боевого сервиса — простой для пользователей.")
        return None
    if cmd == "docker":
        action = args[0] if args else ""
        if action in ("rm", "stop", "kill", "down", "prune") and re.search(r"mp-postgres|postgres", joined, re.I):
            return Verdict(DENY, "service_prod",
                           "останов/снос контейнера боевой БД mp-postgres.")
        if action in ("rm", "prune", "system") and "-f" in args:
            return Verdict(ASK, "service_prod", "принудительная очистка docker-ресурсов.")
    if cmd in ("pkill", "killall"):
        if PROD_UNITS_RE.search(joined) or "uvicorn" in joined:
            return Verdict(ASK, "service_prod", "убийство процессов боевого сервиса.")
    return None


def d_obfuscation(command, degraded):
    if re.search(r"(curl|wget)[^|;]*\|\s*(sudo\s+)?(ba)?sh\b", command):
        return Verdict(DENY, "obfuscation", "исполнение скачанного скрипта напрямую (curl | sh).")
    if re.search(r"base64\s+(-d|--decode)[^|;]*\|\s*(ba)?sh\b", command):
        return Verdict(DENY, "obfuscation", "исполнение base64-декодированного кода.")
    if re.search(r"\beval\b", command) and re.search(r"\$\(|`", command):
        return Verdict(ASK, "obfuscation", "eval с подстановкой — команда неанализируема заранее.")
    return None


# ─────────────────────────── маршрутизация ───────────────────────────

def classify_bash(command, cwd=REPO):
    verdicts = []
    verdicts.append(d_secret_exfil(command))          # по всей команде: переупорядочивание не спасает
    segments, _ = split_segments(command)
    degraded_any = False
    for seg in segments:
        env, cmd, args, degraded = parse(seg)
        degraded_any = degraded_any or degraded
        joined = seg
        verdicts += [
            d_secret_read(env, cmd, args, joined, "Bash"),
            d_bank(env, cmd, args, joined),
            d_mp_write(env, cmd, args, joined, command),
            d_db(env, cmd, args, joined, command),
            d_git(env, cmd, args, joined, cwd),
            d_fs(env, cmd, args, joined),
            d_service(env, cmd, args, joined),
        ]
    verdicts.append(d_obfuscation(command, degraded_any))
    v = worst(verdicts)
    # fail closed: распознали high-risk, но команду разобрать не смогли — поднимаем планку
    if degraded_any and v is not None and v.tier in (LOG, ASK):
        risky = BANK_WRITE_RE.search(command) or MP_WRITE_SCRIPT_RE.search(command) \
            or SECRET_PATH_RE.search(command)
        if risky:
            return Verdict(DENY, v.cls, v.reason + " [команда не разобрана однозначно — fail closed]")
    return v


def classify_file_tool(tool, tool_input):
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if path and SECRET_PATH_RE.search(path):
        if tool == "Read":
            return Verdict(DENY, "secret_read",
                           f"чтение секретного файла ({os.path.basename(path)}) в контекст сессии.")
        return Verdict(DENY, "secret_read",
                       f"правка секретного файла ({os.path.basename(path)}) — только руками человека.")
    return None


def classify_webfetch(tool_input):
    url = tool_input.get("url", "")
    m = SECRET_LITERAL_RE.search(url)
    if m:
        return Verdict(DENY, "secret_exfil",
                       f"в URL передаётся секрет (параметр «{m.group(1)}»).")
    return None


def classify(payload):
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input", {}) or {}
    cwd = payload.get("cwd") or REPO
    if tool == "Bash":
        return classify_bash(ti.get("command", "") or "", cwd)
    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        return classify_file_tool(tool, ti)
    if tool == "WebFetch":
        return classify_webfetch(ti)
    return None


# ─────────────────────────── вывод ───────────────────────────

def write_log(tool, verdict, raw):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "tier": verdict.tier,
            "class": verdict.cls,
            "reason": verdict.reason,
            "cmd": redact(raw),          # значения секретов вымараны
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass                              # журнал не имеет права ломать работу


def emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))
    sys.stdout.flush()


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)                       # не разобрали вход — не мешаем
    try:
        verdict = classify(payload)
        if verdict is None or verdict.tier == ALLOW:
            sys.exit(0)
        raw = (payload.get("tool_input") or {}).get("command") \
            or (payload.get("tool_input") or {}).get("file_path") \
            or (payload.get("tool_input") or {}).get("url") or ""
        write_log(payload.get("tool_name", ""), verdict, raw)
        if verdict.tier == LOG:
            sys.exit(0)
        prefix = f"[guard:{verdict.cls}] "
        if verdict.tier == ASK:
            emit("ask", prefix + verdict.reason)
            sys.exit(0)
        emit("deny", prefix + verdict.reason)
        sys.exit(2)
    except Exception:
        sys.exit(0)                       # баг хука не должен блокировать работу потоков
    sys.exit(0)


if __name__ == "__main__":
    main()
