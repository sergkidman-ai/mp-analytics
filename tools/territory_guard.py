#!/usr/bin/env python3
"""territory_guard.py — ФЛАГ захода на чужую территорию между потоками работы.

Потоки (домены) делят проект по файлам (см. docs/BRIEF_FIN.md / docs/BRIEF_MKT.md). Этот
страж смотрит, какие файлы трогает текущая сессия, и ФЛАГует, если домен сессии лезет в файлы
ЧУЖОГО домена. Домен сессии берётся из имени ветки (`fin/...`, `mkt/...`) или из файла
`.workstream` в корне (`fin`/`mkt`). Манифест DOMAINS легко расширить под будущие домены.

Никаких зависимостей (stdlib) — работает в любом worktree. Граница «маржа = собственность
Финансов»: margin_by_sku.py в OWNED_FIN, поэтому правка из ветки mkt/* → флаг.

Использование:
  python3 tools/territory_guard.py --staged    # проверка staged-файлов (git pre-commit hook)
  python3 tools/territory_guard.py --status     # текущий домен + классификация изменений в рабочей копии
Обход флага (осознанно): WORKSTREAM_OVERRIDE=1 git commit ...   ИЛИ   git commit --no-verify
"""
import os
import re
import sys
import subprocess

# Манифест: домен → список regex путей (от корня репо). Первое совпадение = владелец.
# Не перечисленное здесь — «общее» (core/db.py, CLAUDE.md, web/, docs/, разовые скрипты): НЕ флагуем.
# Миграции делятся по БЛОКУ номера: 0xx = fin, 1xx = mkt (резерв номеров против коллизий).
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
        r"^collectors/(wb_jam|wb_funnel|wb_ads|ozon_ads|ozon_bids|ozon_reviews)\.py$",
        r"^run_marketing\.py$",
        r"^analyze_jam\.py$",
        r"^reports/(abc|funnel|visibility|search).*\.py$",   # будущие маркетинг-витрины
        r"^migrations/[1-9]\d\d_.*\.sql$",
        r"^docs/BRIEF_MKT\.md$",
    ],
}


def classify(path):
    """Домен-владелец файла или None («общий»/ничей)."""
    for dom, pats in DOMAINS.items():
        if any(re.search(p, path) for p in pats):
            return dom
    return None


def current_domain():
    """Домен сессии: ветка fin/* mkt/* → fin/mkt; иначе файл .workstream; иначе None."""
    try:
        br = subprocess.check_output(["git", "branch", "--show-current"],
                                     text=True).strip()
    except Exception:
        br = ""
    pref = br.split("/", 1)[0] if "/" in br else ""
    if pref in DOMAINS:
        return pref, f"ветка {br}"
    root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    wf = os.path.join(root, ".workstream")
    if os.path.exists(wf):
        d = open(wf).read().strip()
        if d in DOMAINS:
            return d, ".workstream"
    return None, br or "(нет ветки)"


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
    staged = mode == "--staged"
    dom, src = current_domain()
    files = changed_files(staged)

    if mode == "--status":
        print(f"Домен сессии: {dom or '— НЕ ЗАДАН —'}  ({src})")
        if dom is None:
            print("  ⚠ домен не определён: работай в ветке fin/* или mkt/* (или создай файл .workstream).")
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
    if dom is None:
        print("⚠ [territory] домен сессии не определён (ветка не fin/* и не mkt/*, нет .workstream).")
        print("  Страж пропускает коммит, но заведи доменную ветку, чтобы флаг работал.")
        return 0

    foreign = [(f, classify(f)) for f in files
               if classify(f) is not None and classify(f) != dom]
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
