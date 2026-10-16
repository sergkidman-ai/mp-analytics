#!/usr/bin/env python3
# поток: infra
"""Реестр платных внешних возможностей проекта — ЕДИНЫЙ источник правды.

Зачем. Инвариант CLAUDE.md: «платные ресурсы — только после явного да Сергея на конкретную
сумму». Текстом это правило держится нестабильно (gold-set B3). Здесь лежат ДАННЫЕ о том,
какие действия создают внешний платный расход; решение по ним принимает
`tools/hooks/guard_side_effects.py` (класс риска `paid_api`, уровень ASK).

Принципы:
* Реестр перечисляет ПРОВАЙДЕРОВ (хосты + имена переменных окружения) и ТОЧКИ ВХОДА
  (файлы репозитория, исполнение которых обращается к провайдеру). Ничего про конкретные
  строки gold-set: класс общий.
* Точка входа попадает сюда только с доказательством (`evidence`), полученным из кода:
  прямой вызов клиента, хост провайдера в исходнике, SDK-клиент или импорт платного модуля.
* Реестр не хранит тарифы. Стоимость заранее не выдумывается: если её нельзя посчитать —
  так и пишется «неизвестна».
* Дрейф ловится: `python3 tools/paid_api_registry.py --audit` пересканирует репозиторий
  теми же правилами и покажет расхождения (появилась новая платная точка / файл исчез).

Смотри также `tools/territory_guard.py` — тот же приём для владения потоками.
"""

from __future__ import annotations

import os
import re
import sys
from collections import namedtuple

REPO = "/opt/mp-analytics"

# ─────────────────────────── провайдеры ───────────────────────────
# hosts    — по каким хостам уходит платный запрос
# env_keys — ИМЕНА переменных окружения (значения секретов здесь не появляются никогда)
# billing  — как считаются деньги; тариф намеренно не фиксируем (меняется у провайдера)
Provider = namedtuple("Provider", "name hosts env_keys billing")

PROVIDERS = (
    Provider("deepseek", ("api.deepseek.com",),
             ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
             "предоплаченный баланс, списание по токенам"),
    Provider("anthropic", ("api.anthropic.com",),
             ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
             "постоплата по токенам; инструмент web_search тарифицируется отдельно за вызов"),
    # В коде проекта не используются, но ключ/хост может появиться в разовой команде —
    # держим в списке, чтобы прямой curl не оказался незамеченным.
    Provider("openai", ("api.openai.com",), ("OPENAI_API_KEY",), "постоплата по токенам"),
    Provider("openrouter", ("openrouter.ai",), ("OPENROUTER_API_KEY",), "предоплаченный баланс"),
)

# ─────────────────────────── точки входа ───────────────────────────
# path     — путь ОТНОСИТЕЛЬНО корня репозитория (совпадение по суффиксу, поэтому правило
#            одинаково работает в worktree и в копии .deploy/)
# evidence — чем доказано, что исполнение тратит деньги
Entrypoint = namedtuple("Entrypoint", "path provider evidence")

ENTRYPOINTS = (
    Entrypoint("reports/llm_client.py", "deepseek|anthropic",
               "сам клиент: SDK, хост провайдера, create_with_retry()"),
    Entrypoint("reports/feedback_llm.py", "anthropic", "SDK-клиент, генерация ответов на отзывы"),
    Entrypoint("reports/feedback_llm_sync.py", "anthropic", "SDK-клиент + create_with_retry()"),
    Entrypoint("reports/feedback_web.py", "anthropic",
               "create_with_retry() с инструментом web_search (отдельный тариф за вызов)"),
    Entrypoint("reports/feedback_today.py", "deepseek|anthropic", "create_with_retry()"),
    Entrypoint("reports/feedback_sample.py", "deepseek|anthropic",
               "импортирует reports.feedback_today"),
    Entrypoint("ops/news_digest.py", "deepseek|anthropic", "create_with_retry()"),
    Entrypoint("prices/research.py", "deepseek|anthropic", "create_with_retry()"),
    Entrypoint("feedback_bot/health.py", "deepseek|anthropic",
               "create_with_retry() под FEEDBACK_HEALTH_PROBE — расход мал, но он есть"),
    Entrypoint("tools/deepseek_candidate_validator.py", "deepseek",
               "прямой POST на api.deepseek.com, учёт токенов в отчёте"),
    Entrypoint("tools/deepseek_extract.py", "deepseek",
               "импортирует tools.deepseek_candidate_validator"),
    Entrypoint("tools/search_fetch.py", "deepseek", "импортирует tools.deepseek_extract"),
    Entrypoint("feedback_bot/feedback_cycle.py", "deepseek|anthropic",
               "шаг 3/5 цикла модерации — reports.feedback_today.run() генерирует черновики"),
    Entrypoint("feedback_bot/backfill_review_drafts.py", "deepseek|anthropic",
               "гоняет тот же движок reports.feedback_today на исторических отзывах"),
    Entrypoint("feedback_bot/push_questions.py", "deepseek|anthropic",
               "генерирует черновики ответов на вопросы через reports.feedback_today"),
    Entrypoint("ops/mp_news.py", "deepseek|anthropic", "импортирует ops.news_digest"),
)

# Модули, которые ИМПОРТИРУЮТ платный код, но сами по себе расход не создают.
# Держим списком с причиной, чтобы аудит не показывал их как дрейф каждый прогон.
INDIRECT = {
    "web/app.py": "дашборд импортирует prices.research, но дёргает только локальные и "
                  "кэшированные функции; платный запрос возможен лишь по действию человека в UI. "
                  "Перезапуск сервиса и так требует подтверждения (класс service_prod).",
}

# Тесты платных модулей офлайновые (в них моки; проверено прогоном без .env и без venv) —
# иначе любой прогон набора требовал бы подтверждения и правило быстро отключили бы.
# Освобождение снимается, если в команде есть хост провайдера или его ключ.
EXEMPT_PREFIXES = ("tests/",)

_MODULES = {e.path[:-3].replace("/", "."): e for e in ENTRYPOINTS if e.path.endswith(".py")}


# ─────────────────────────── сопоставление ───────────────────────────

def provider_for_host(host: str):
    """Хост (или строка, содержащая хост) → имя провайдера."""
    h = (host or "").lower()
    for p in PROVIDERS:
        for known in p.hosts:
            if h == known or h.endswith("." + known) or known in h:
                return p.name
    return None


def provider_for_env_key(name: str):
    """Имя переменной окружения → провайдер. Значение переменной здесь не участвует."""
    n = (name or "").upper()
    for p in PROVIDERS:
        if n in p.env_keys:
            return p.name
    return None


def entrypoint_for_token(token: str):
    """Токен командной строки (путь или dotted-модуль) → точка входа реестра."""
    if not token:
        return None
    t = token.strip("'\"").strip()
    t = t.split("=", 1)[1] if t.startswith("--") and "=" in t else t
    norm = t.replace("\\", "/").lstrip("./")
    for e in ENTRYPOINTS:
        if norm == e.path or norm.endswith("/" + e.path):
            return e
    mod = t.strip(".")
    if mod in _MODULES:
        return _MODULES[mod]
    return None


def scan_text(text: str):
    """Все платные упоминания в произвольном тексте (тело `python -c`, heredoc, json).
    Возвращает (провайдеры_по_хостам, точки_входа)."""
    if not text:
        return set(), set()
    low = text.lower()
    hosts = {p.name for p in PROVIDERS for h in p.hosts if h in low}
    eps = set()
    for e in ENTRYPOINTS:
        if e.path in text or e.path[:-3].replace("/", ".") in text:
            eps.add(e)
    return hosts, eps


def is_exempt(token: str) -> bool:
    """Путь освобождён от класса: офлайновые тесты (каталог `tests/` и любые `test_*.py`,
    включая наборы самих хуков — в них платных вызовов нет, только строки для классификации)."""
    norm = (token or "").strip("'\"").replace("\\", "/").lstrip("./")
    if os.path.basename(norm).startswith("test_") and norm.endswith(".py"):
        return True
    return any(norm.startswith(pref) or ("/" + pref) in norm for pref in EXEMPT_PREFIXES)


def describe(provider: str) -> str:
    """Строка про биллинг провайдера для карточки подтверждения."""
    names = [n.strip() for n in (provider or "").split("|")]
    bits = [f"{p.name}: {p.billing}" for p in PROVIDERS if p.name in names]
    return "; ".join(bits) or "тарификация провайдера неизвестна"


# ─────────────────────────── аудит дрейфа ───────────────────────────

_PAID_CALL_RE = re.compile(r"create_with_retry\s*\(|\.messages\.create\s*\(|"
                           r"\.chat\.completions\.create\s*\(|client_for\s*\(")
_SDK_RE = re.compile(r"^\s*(from|import)\s+(anthropic|openai)\b", re.M)
_SKIP_DIRS = {"venv", "pgdata", "backups", ".git", "node_modules", "worktrees", "scratchpad",
              "docs", ".deploy"}


def _imports(src: str, path: str) -> bool:
    """Есть ли в исходнике настоящий импорт модуля `path` (а не упоминание пути в тексте)."""
    dotted = re.escape(path[:-3].replace("/", "."))
    leaf = re.escape(os.path.basename(path)[:-3])
    pkg = re.escape(os.path.dirname(path).replace("/", "."))
    pats = [rf"^\s*(?:from\s+{dotted}\s+import|import\s+{dotted}\b)",
            rf"^\s*(?:from\s+{leaf}\s+import|import\s+{leaf}\b)"]
    if pkg:
        # `from reports import feedback_today` — форма, которой пользуется feedback_cycle
        pats.append(rf"^\s*from\s+{pkg}\s+import\s+[^\n#]*\b{leaf}\b")
    return any(re.search(p, src, re.M) for p in pats)


def audit(repo: str = REPO) -> dict:
    """Пересканировать репозиторий теми же правилами и сравнить с реестром."""
    found = {}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(root, f), repo)
            if is_exempt(rel) or rel.startswith("tools/paid_api_registry"):
                continue
            try:
                with open(os.path.join(root, f), encoding="utf-8", errors="ignore") as fh:
                    src = fh.read()
            except Exception:
                continue
            ev = []
            if _PAID_CALL_RE.search(src):
                ev.append("вызов клиента")
            if any(h in src for p in PROVIDERS for h in p.hosts):
                ev.append("хост провайдера")
            if _SDK_RE.search(src):
                ev.append("SDK")
            for e in ENTRYPOINTS:
                # именно ИМПОРТ, а не упоминание пути в строке/комментарии: например
                # tools/gab_hunt_batch.py печатает команду валидатора как подсказку — это не расход
                if e.path != rel and _imports(src, e.path):
                    ev.append(f"импорт {e.path}")
                    break
            if ev:
                found[rel] = ev
    known = {e.path for e in ENTRYPOINTS}
    return {
        "новые": {k: v for k, v in sorted(found.items())
                  if k not in known and k not in INDIRECT},
        "исчезли": sorted(p for p in known if not os.path.exists(os.path.join(repo, p))),
        "подтверждены": sorted(k for k in found if k in known),
    }


def _main(argv):
    if "--audit" in argv:
        res = audit()
        print(f"подтверждено точек входа: {len(res['подтверждены'])}/{len(ENTRYPOINTS)}")
        for k, v in res["новые"].items():
            print(f"  НОВАЯ ПЛАТНАЯ ТОЧКА (нет в реестре): {k} — {', '.join(v)}")
        for k in res["исчезли"]:
            print(f"  В РЕЕСТРЕ, НО ФАЙЛА НЕТ: {k}")
        return 1 if (res["новые"] or res["исчезли"]) else 0
    print(f"провайдеров: {len(PROVIDERS)}, точек входа: {len(ENTRYPOINTS)}")
    for e in ENTRYPOINTS:
        print(f"  {e.path:45s} {e.provider:22s} {e.evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
