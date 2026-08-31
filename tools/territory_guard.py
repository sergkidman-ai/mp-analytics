#!/usr/bin/env python3
"""territory_guard.py — ФЛАГ захода на чужую территорию между потоками работы.

Потоки (домены) делят проект по файлам (см. docs/BRIEF_FIN.md / docs/BRIEF_MKT.md). Этот
страж смотрит, какие файлы трогает текущая сессия, и ФЛАГует, если домен сессии лезет в файлы
ЧУЖОГО домена. Домен сессии: файл `.workstream` в корне — ЯВНАЯ личность сессии и имеет
приоритет; имя ветки (`fin/...`, `mkt/...`) — только подсказка, когда `.workstream` нет.
Явное и ветка противоречат друг другу → КОНФЛИКТ: не угадываем, а закрываемся на запись.
Манифест DOMAINS легко расширить под будущие домены.

Никаких зависимостей (stdlib) — работает в любом worktree. Граница «маржа = собственность
Финансов»: margin_by_sku.py в OWNED_FIN, поэтому правка из ветки mkt/* → флаг.

Здесь же живёт ЕДИНСТВЕННЫЙ разрешатель владельца (`resolve_ownership`) и единственный
манифест защищённых ресурсов (`RESOURCES`). Второй потребитель — PreToolUse-страж
`tools/hooks/guard_side_effects.py`; логика владения НЕ дублируется, он импортирует отсюда.

Использование:
  python3 tools/territory_guard.py --staged    # проверка staged-файлов (git pre-commit hook)
  python3 tools/territory_guard.py --status     # текущий домен + классификация изменений в рабочей копии
  python3 tools/territory_guard.py --runtime '<команда>'   # что скажет runtime-страж
Обход флага (осознанно): WORKSTREAM_OVERRIDE=1 git commit ...   ИЛИ   git commit --no-verify
"""
import os
import re
import sys
import subprocess

# Манифест: домен → список regex путей (от корня репо). Первое совпадение = владелец.
# Не перечисленное здесь — «общее» (core/db.py, CLAUDE.md, web/, docs/, разовые скрипты): НЕ флагуем.
# Миграции делятся по БЛОКУ номера: 0xx = fin, 1xx = mkt, 2xx = inv, 3xx = ret, 4xx = prc,
# 5xx = ev, 6xx = card
# (резерв против коллизий).
DOMAINS = {
    "fin": [
        r"^collectors/(moysklad|ms_products|ms_demand_cogs|wb|ozon|ozon_postings|"
        r"ozon_products|ozon_fbo_stock|yandex|yandex_monthly|supplier_purchases|"
        r"suppliers|set_cost)\.py$",
        r"^reports/(margin_by_sku|margin_ozon_sku|ozon_expenses)\.py$",
        r"^run_daily\.py$",
        r"^(rebuild_validate_cogs|phase1_cogs|cogs_compare)\.py$",
        r"^migrations/0\d\d_.*\.sql$",
        r"^docs/BRIEF_FIN\.md$",
    ],
    "mkt": [
        r"^collectors/(wb_jam|wb_funnel|wb_ads|ozon_ads|ozon_bids|ozon_reviews|"
        r"ozon_search_queries|ozon_price_index)\.py$",
        r"^run_marketing\.py$",
        r"^analyze_jam\.py$",
        r"^reports/(abc|funnel|visibility|search|ozon_red_zone|ozon_buyer_price"
        r"|ozon_margin_control)"
        r".*\.py$",   # маркетинг-витрины
        r"^migrations/1\d\d_.*\.sql$",
        r"^docs/BRIEF_MKT.*\.md$",     # BRIEF_MKT.md и подзадачи (BRIEF_MKT_OZON.md)
        r"^docs/MKT_.*\.md$",          # разборы и планы потока (MKT_OZON_PLAN.md)
    ],
    # ret = ВОЗВРАТЫ ТОВАРА (что физически забрать с ПВЗ). Не путать с rev = отзывы.
    "ret": [
        r"^returns_bot/",
        r"^migrations/3\d\d_.*\.sql$",
        r"^docs/BRIEF_RET\.md$",
    ],
    # ev = ДНЕВНИК СОБЫТИЙ: наши решения, изменения площадок, сторож остатков поставщика.
    # web/app.py и home.html НЕ забираем в домен: это общая витрина, дневник живёт в своём
    # роутере (web/diary_api.py) и подключается к app.py одной строкой.
    "ev": [
        r"^ops/(biz_diary|stock_watch|mp_news|news_digest)\.py$",
        r"^web/diary_api\.py$",
        r"^migrations/5\d\d_.*\.sql$",
        r"^docs/BRIEF_EV\.md$",
    ],
    # prc = ПРАЙСЫ ПОСТАВЩИКОВ: почта -> Оприходование в МС -> список новинок.
    # core/ms_api.py пока пишет только prc; появится второй потребитель — вынести в «общее».
    "prc": [
        r"^prices/",
        r"^core/ms_api\.py$",
        r"^migrations/4\d\d_.*\.sql$",
        r"^docs/BRIEF_PRC\.md$",
    ],
    # card = ЗДОРОВЬЕ КАРТОЧЕК на площадках: статусы модерации, «Ошибки»/«На доработку»,
    # дожим упавших апдейтов ТК. Контент карточек не наш — его пишет ТК.
    "card": [
        r"^collectors/ozon_card_status\.py$",
        r"^tools/(ozon_card_|card_)",
        r"^migrations/6\d\d_.*\.sql$",
        r"^docs/BRIEF_CARD\.md$",
    ],
}


# Манифест ЗАЩИЩЁННЫХ РЕСУРСОВ: владение действует не только на файлы в коммите, но и на
# исполнение в рантайме. Ключ — имя ресурса, каким его называют люди и код.
#   owner       — домен-владелец записи;
#   entrypoints — реальные точки входа, которые ЗАПИСЫВАЮТ ресурс (regex по команде);
#   tokens      — как ресурс упоминается в SQL/командах (таблица, витрина).
# Правило добавления: ресурс сюда попадает, только если его запись реально выполняется
# перечисленными entrypoints. Строки gold-set сюда не попадают никогда.
RESOURCES = {
    "margin_by_sku": {
        "owner": "fin",
        "entrypoints": [
            r"(^|[/\s])run_daily\.py(\s|$)",
            r"(^|[/\s])reports/margin_by_sku\.py(\s|$)",
            r"(^|[/\s])rebuild_validate_cogs\.py(\s|$)",
            r"(^|[/\s])phase1_cogs\.py(\s|$)",
            r"\breports\.margin_by_sku\b",          # python -m / import
        ],
        "tokens": [r"\bmargin_by_sku\b"],
    },
}

# Глаголы записи в SQL и обычные разрушители — если рядом с токеном ресурса, это запись.
_WRITE_SQL_RE = re.compile(
    r"\b(insert\s+into|update|delete\s+from|truncate|drop\s+(table|view|materialized)|"
    r"alter\s+table|create\s+(or\s+replace\s+)?(table|view|materialized\s+view)|"
    r"refresh\s+materialized\s+view|copy\s+\S+\s+from|\\copy)\b", re.I)
# Явно read-only обращения к ресурсу.
_READ_SQL_RE = re.compile(r"\b(select|count\s*\(|explain|\\d\+?)\b", re.I)
# Инструменты, которые только смотрят (grep по коду, чтение файла, история git).
_READONLY_TOOL_RE = re.compile(
    r"^\s*(sudo\s+)?(grep|rg|ag|egrep|fgrep|cat|head|tail|less|more|wc|sed\s+-n|awk|find|ls|"
    r"git\s+(log|show|diff|grep|blame|status))\b", re.I)
# Признак того, что команда что-то ИСПОЛНЯЕТ (а не просто содержит слово).
_EXEC_RE = re.compile(
    r"(^|[|;&]\s*)(sudo\s+)?([^\s|;&]*/)?(python3?|venv/bin/python|psql|docker|"
    r"pg_restore|make|bash|sh|systemctl)\b", re.I)

READ = "read"
WRITE = "write"
UNKNOWN = "unknown"


def classify(path):
    """Домен-владелец файла или None («общий»/ничей)."""
    for dom, pats in DOMAINS.items():
        if any(re.search(p, path) for p in pats):
            return dom
    return None


def _repo_root(cwd=None):
    try:
        return subprocess.check_output(["git", "rev-parse", "--show-toplevel"],
                                       text=True, cwd=cwd,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return cwd or os.getcwd()


def _branch(cwd=None):
    try:
        return subprocess.check_output(["git", "branch", "--show-current"],
                                       text=True, cwd=cwd,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


class Ownership:
    """Кто такая эта сессия. status: explicit | branch | conflict | unknown."""

    __slots__ = ("domain", "status", "source", "detail")

    def __init__(self, domain, status, source, detail=""):
        self.domain = domain
        self.status = status
        self.source = source
        self.detail = detail

    def __repr__(self):                                   # для отладки и тестов
        return f"Ownership({self.domain!r}, {self.status!r}, {self.source!r})"


def resolve_ownership(cwd=None, workstream_file=None, branch=None):
    """ЕДИНСТВЕННЫЙ разрешатель домена сессии. Используют и pre-commit, и PreToolUse.

    Приоритет: `.workstream` — явная личность сессии. Ветка — только фолбэк.
    Явное и ветка расходятся → status='conflict', domain=None (не угадываем).
    Параметры даны для тестов; в проде читается диск.
    """
    root = _repo_root(cwd)
    if workstream_file is None:
        workstream_file = os.path.join(root, ".workstream")
    ws = None
    try:
        if os.path.exists(workstream_file):
            with open(workstream_file) as fh:
                cand = fh.read().strip()
            if cand in DOMAINS:
                ws = cand
    except Exception:
        ws = None

    br = _branch(cwd) if branch is None else branch
    pref = br.split("/", 1)[0] if "/" in br else ""
    brdom = pref if pref in DOMAINS else None

    if ws and brdom and ws != brdom:
        return Ownership(None, "conflict", f".workstream={ws} vs ветка {br}",
                         f"явный поток «{ws}» и ветка «{br}» указывают на разные домены")
    if ws:
        return Ownership(ws, "explicit", ".workstream")
    if brdom:
        return Ownership(brdom, "branch", f"ветка {br}")
    return Ownership(None, "unknown", br or "(нет ветки)")


def current_domain(cwd=None):
    """Совместимость со старыми вызовами: (домен|None, откуда)."""
    own = resolve_ownership(cwd)
    return own.domain, own.source


_MODE_RANK = {READ: 0, UNKNOWN: 1, WRITE: 2}
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_PIPE_TO_SHELL_RE = re.compile(r"\|\s*(sudo\s+)?(ba)?sh\b", re.I)
# Первая команда сегмента, которая что-то ИСПОЛНЯЕТ.
# Собственный диагностический CLI: ничего не исполняет, только печатает вердикт.
_SELF_DIAG_RE = re.compile(r"tools/territory_guard\.py")
_EXECUTOR_RE = re.compile(
    r"^([^\s]*/)?(python3?|psql|docker|make|bash|sh|systemctl|pg_restore)$", re.I)


def _leader(segment):
    """Первая НАСТОЯЩАЯ команда сегмента (пропуская присваивания VAR=... и sudo)."""
    for tok in segment.replace("\t", " ").split():
        if _ENV_ASSIGN_RE.match(tok):
            continue
        if tok == "sudo":
            continue
        return tok
    return ""


def _segments(command):
    """Разбить команду на сегменты по ;  |  &  и переводам строк — УВАЖАЯ кавычки.
    Наивный split ломается на `python -c "import x; x.go('margin_by_sku')"`."""
    out, buf, quote = [], [], None
    for ch in command:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch in ";|&\n":
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return [s for s in out if s.strip()]


def classify_action(command):
    """Что команда делает с защищённым ресурсом.

    → (имя ресурса, READ|WRITE|UNKNOWN) либо None, если защищённых ресурсов не касается.
    Классифицируем ресурс/действие, а не текст запроса пользователя. Ключевое различие:
    точка входа в ПОЗИЦИИ КОМАНДЫ = исполнение; та же строка в аргументе (echo, payload,
    grep-шаблон) = упоминание, и это чтение.
    """
    if not command:
        return None
    for name, spec in RESOURCES.items():
        if not any(re.search(p, command, re.I)
                   for p in spec["entrypoints"] + spec["tokens"]):
            continue
        mode = None
        for seg in _segments(command):
            hit_entry = any(re.search(p, seg, re.I) for p in spec["entrypoints"])
            hit_token = any(re.search(p, seg, re.I) for p in spec["tokens"])
            if not (hit_entry or hit_token):
                continue
            if _SELF_DIAG_RE.search(seg) and not _WRITE_SQL_RE.search(seg):
                seg_mode = READ                    # диагностика самого стража
                if mode is None or _MODE_RANK[seg_mode] > _MODE_RANK[mode]:
                    mode = seg_mode
                continue
            lead = _leader(seg)
            executes = bool(_EXECUTOR_RE.match(lead)) or lead.endswith(".py")
            readonly_tool = bool(_READONLY_TOOL_RE.match(seg.strip()))
            if hit_entry and executes and not readonly_tool:
                seg_mode = WRITE                   # точка входа реально запускается
            elif _WRITE_SQL_RE.search(seg):
                seg_mode = WRITE
            elif readonly_tool or _READ_SQL_RE.search(seg):
                seg_mode = READ                    # grep/cat/git log/SELECT — осмотр разрешён
            elif executes:
                seg_mode = UNKNOWN                 # исполняем, но не разобрали — закрываемся
            else:
                seg_mode = READ                    # упоминание в аргументе, не исполнение
            if mode is None or _MODE_RANK[seg_mode] > _MODE_RANK[mode]:
                mode = seg_mode
        if mode is None:
            continue
        if mode == READ and _PIPE_TO_SHELL_RE.search(command):
            mode = UNKNOWN                         # `... | sh` — что исполнится, не видно
        return name, mode
    return None


def runtime_decision(command, cwd=None, env=None, ownership=None):
    """Решение runtime-стража по одной команде.

    → None, если защищённый ресурс не затронут (страж молчит).
    → ("allow"|"deny", причина) для затронутого ресурса.
    `ownership` подставляется тестами; в проде читается диск.
    """
    hit = classify_action(command)
    if hit is None:
        return None
    name, mode = hit
    spec = RESOURCES[name]
    owner = spec["owner"]
    own = resolve_ownership(cwd) if ownership is None else ownership

    if mode == READ:
        return "allow", (f"{name} принадлежит {owner}; обращение read-only — разрешено.")

    env = os.environ if env is None else env
    who = own.domain or (f"НЕ ОПРЕДЕЛЁН ({own.detail or own.source})"
                         if own.status != "conflict" else f"КОНФЛИКТ ({own.detail})")

    if own.status == "explicit" and own.domain == owner:
        return "allow", f"{name} принадлежит {owner}; текущий поток — {owner}."
    if own.status == "branch" and own.domain == owner:
        return "allow", f"{name} принадлежит {owner}; домен по ветке — {owner}."

    # Осознанный обход — только из окружения сессии (человек запустил с флагом).
    # Инлайновое «WORKSTREAM_OVERRIDE=1 команда» обходом НЕ считается.
    if env.get("WORKSTREAM_OVERRIDE") == "1" and not re.match(
            r"^\s*WORKSTREAM_OVERRIDE\s*=", command or ""):
        return "allow", f"{name}: WORKSTREAM_OVERRIDE=1 в окружении сессии — пропуск осознанный."

    tail = ("read-only allowed, write/rebuild denied."
            if mode == WRITE else
            "действие не классифицировано как чтение → закрыто (fail closed); "
            "read-only allowed, write/rebuild denied.")
    return "deny", (f"[territory] {name} owned by {owner}; "
                    f"current workstream is {who}; {tail}")


def changed_files(staged):
    cmd = (["git", "diff", "--cached", "--name-only"] if staged
           else ["git", "diff", "--name-only"])
    out = subprocess.check_output(cmd, text=True)
    files = [f for f in out.splitlines() if f.strip()]
    if not staged:  # рабочая копия — добавим и неотслеживаемые
        unt = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"], text=True)
        files += [f for f in unt.splitlines() if f.strip()]
    return sorted(set(files))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--staged"

    if mode == "--runtime":                       # ручная проверка: что скажет PreToolUse
        cmd = sys.argv[2] if len(sys.argv) > 2 else ""
        res = runtime_decision(cmd)
        print("нет защищённых ресурсов" if res is None else f"{res[0]}: {res[1]}")
        return 0

    staged = mode == "--staged"
    own = resolve_ownership()
    dom, src = own.domain, own.source
    files = changed_files(staged)

    if mode == "--status":
        print(f"Домен сессии: {dom or '— НЕ ЗАДАН —'}  ({src}, статус {own.status})")
        if own.status == "conflict":
            print(f"  ⛔ конфликт владения: {own.detail}. Правки защищённых файлов закрыты.")
        if dom is None and own.status != "conflict":
            print("  ⚠ домен не определён: работай в ветке <домен>/* (fin, mkt, ret) или создай файл .workstream.")
        print(f"Изменённых файлов: {len(files)}")
        for f in files:
            owner = classify(f)
            own = owner or "общий"
            if dom is None or owner is None or owner == dom:
                mark = "✓"
            else:
                mark = "⛔ ЧУЖОЙ"
            print(f"  [{own:6}] {mark}  {f}")
        return 0

    # режим проверки (hook)
    owned = [(f, classify(f)) for f in files if classify(f) is not None]

    if own.status == "conflict":
        if not owned:
            return 0                              # общие файлы — конфликт не мешает
        print("⛔ [territory] КОНФЛИКТ владения: %s." % own.detail)
        print("  Не угадываю домен; правка файлов с владельцем закрыта:")
        for f, o in owned:
            print(f"     → {f}   (территория «{o}»)")
        if os.getenv("WORKSTREAM_OVERRIDE") == "1":
            print("  WORKSTREAM_OVERRIDE=1 — пропускаю осознанно.")
            return 0
        print("  Приведи .workstream и ветку в согласие либо WORKSTREAM_OVERRIDE=1 git commit ...")
        return 1

    if dom is None:
        print("⚠ [territory] домен сессии не определён (ветка не <домен>/*, нет .workstream).")
        print("  Страж пропускает коммит, но заведи доменную ветку, чтобы флаг работал.")
        return 0

    foreign = [(f, o) for f, o in owned if o != dom]
    if not foreign:
        return 0

    print("⛔ [territory] ФЛАГ: сессия домена «%s» (%s) правит ЧУЖИЕ файлы:" % (dom, src))
    for f, own in foreign:
        print(f"     → {f}   (территория «{own}»)")
    print("  Граница доменов: docs/BRIEF_FIN.md / docs/BRIEF_MKT.md (маржа = собственность Финансов).")
    if os.getenv("WORKSTREAM_OVERRIDE") == "1":
        print("  WORKSTREAM_OVERRIDE=1 — пропускаю осознанно.")
        return 0
    print("  Если это намеренно: WORKSTREAM_OVERRIDE=1 git commit ...  (или git commit --no-verify).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
