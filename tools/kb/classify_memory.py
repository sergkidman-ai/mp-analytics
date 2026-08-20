#!/usr/bin/env python3
# поток: infra
"""Сигналы классификации карточек памяти перед миграцией в канонический стор (MVP-1).

Скрипт НЕ принимает решений: он считает сигналы S1-S8 и пишет CSV предложений.
Класс проставляет человек/оркестратор по политике из плана.

S1 живость путей из тела карточки   S2 возраст (mtime)
S3 попарная близость (шинглы 5 слов) S4 пересечение тегов/тем
S5 нормативные формулировки          S6 упоминание несуществующих сущностей
S7 конфликт с CLAUDE.md (эвристика)  S8 размер тела

Запуск: ./venv/bin/python tools/kb/classify_memory.py
Выход:  reports/data/kb_classification.csv + сводка <=20 строк в stdout
"""
import csv, os, re, subprocess, sys, time
from pathlib import Path

REPO = Path('/opt/mp-analytics')
SCOPES = {
    'repo': Path('/root/.claude/projects/-opt-mp-analytics/memory'),
    'root': Path('/root/.claude/projects/-root/memory'),
}
OUT = REPO / 'reports/data/kb_classification.csv'
NOW = time.time()

NORM_RE = re.compile(r'\b(запрещ|только по|нельзя|обязан|всегда|никогда|не трогать|не менять)', re.I)
PATH_RE = re.compile(r'`([A-Za-z0-9_./-]+\.(?:py|sh|md|sql|json|csv|service|toml|ini))`')
MP_RE = re.compile(r'mp-analytics|margin_by_sku|МойСклад|wb_|oz_acc|Ozon|Wildberries|WB |МС\b|Дисквэр', re.I)


def parse(p):
    txt = p.read_text(encoding='utf-8', errors='replace')
    name = desc = mtype = ''
    body = txt
    if txt.startswith('---'):
        end = txt.find('\n---', 3)
        if end > 0:
            fm, body = txt[3:end], txt[end + 4:]
            for line in fm.splitlines():
                s = line.strip()
                if s.startswith('name:'):
                    name = s[5:].strip()
                elif s.startswith('description:'):
                    desc = s[12:].strip()
                elif s.startswith('type:'):
                    mtype = s[5:].strip()
    return {'file': p, 'slug': name or p.stem, 'desc': desc, 'type': mtype,
            'body': body.strip(), 'raw': txt}


def shingles(text, k=5):
    w = re.findall(r'[a-zа-яё0-9_]+', text.lower())
    return {' '.join(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}


def jac(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def main():
    cards = []
    for scope, d in SCOPES.items():
        if not d.is_dir():
            continue
        for p in sorted(d.glob('*.md')):
            if p.name == 'MEMORY.md':
                continue
            c = parse(p)
            c['scope'] = scope
            cards.append(c)

    # S6: какие сущности реально есть в репозитории
    tracked = set(subprocess.run(['git', 'ls-files'], cwd=REPO, capture_output=True,
                                 text=True).stdout.split())
    tracked_base = {os.path.basename(t) for t in tracked}

    for c in cards:
        b = c['body']
        c['sh'] = shingles(b)
        c['S8_len'] = len(b)
        c['S2_age_d'] = int((NOW - c['file'].stat().st_mtime) / 86400)
        c['S5_norm'] = len(NORM_RE.findall(b))
        c['S7_mp'] = bool(MP_RE.search(b) or MP_RE.search(c['desc']))
        paths = sorted(set(PATH_RE.findall(b)))
        alive, dead = [], []
        for pth in paths:
            ok = (REPO / pth).exists() or pth in tracked or os.path.basename(pth) in tracked_base \
                 or Path(pth).exists()
            (alive if ok else dead).append(pth)
        c['S1_paths'] = len(paths)
        c['S1_dead'] = dead
        c['S6_ghost'] = len(dead)

    # S4: словарь редких (значимых) терминов — темы, а не стиль
    from collections import Counter
    df = Counter()
    for c in cards:
        c['terms'] = {w for w in re.findall(r'[a-zA-Zа-яёА-ЯЁ0-9_./]{4,}', c['body'].lower())}
        df.update(c['terms'])
    lim = max(2, int(len(cards) * 0.20))
    for c in cards:
        c['salient'] = {w for w in c['terms'] if df[w] <= lim}

    # S3/S4 попарно
    for c in cards:
        c['S3_best'] = 0.0
        c['S3_with'] = ''
        c['S4_best'] = 0.0
        c['S4_with'] = ''
    for i in range(len(cards)):
        for j in range(i + 1, len(cards)):
            s3 = jac(cards[i]['sh'], cards[j]['sh'])
            s4 = jac(cards[i]['salient'], cards[j]['salient'])
            if cards[i]['slug'] == cards[j]['slug']:
                s4 = max(s4, 0.99)
            for a, b in ((i, j), (j, i)):
                if s3 > cards[a]['S3_best']:
                    cards[a]['S3_best'] = s3
                    cards[a]['S3_with'] = f"{cards[b]['scope']}:{cards[b]['slug']}"
                if s4 > cards[a]['S4_best']:
                    cards[a]['S4_best'] = s4
                    cards[a]['S4_with'] = f"{cards[b]['scope']}:{cards[b]['slug']}"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['scope', 'slug', 'type', 'len', 'age_d', 'paths', 'dead_paths',
                    'norm_hits', 'sim_best', 'sim_with', 'topic_best', 'topic_with', 'is_mp', 'dead_list', 'desc'])
        for c in sorted(cards, key=lambda x: (-x['S4_best'], x['scope'], x['slug'])):
            w.writerow([c['scope'], c['slug'], c['type'], c['S8_len'], c['S2_age_d'],
                        c['S1_paths'], c['S6_ghost'], c['S5_norm'], f"{c['S3_best']:.2f}",
                        c['S3_with'], f"{c['S4_best']:.2f}", c['S4_with'], int(c['S7_mp']), ';'.join(c['S1_dead'])[:200],
                        c['desc'][:160]])

    n = len(cards)
    print(f'карточек: {n} (repo {sum(1 for c in cards if c["scope"]=="repo")}, '
          f'root {sum(1 for c in cards if c["scope"]=="root")})')
    print(f'mp-тематика: {sum(1 for c in cards if c["S7_mp"])}')
    print(f'S3>=0.85 (дубли): {sum(1 for c in cards if c["S3_best"]>=0.85)}')
    print(f'S3 0.60-0.85: {sum(1 for c in cards if 0.6<=c["S3_best"]<0.85)}')
    print(f'S4 тем.близость >=0.30 (кандидаты MERGE): {sum(1 for c in cards if c["S4_best"]>=0.30)}')
    print(f'S4 >=0.20: {sum(1 for c in cards if c["S4_best"]>=0.20)}')
    print(f'с битыми путями: {sum(1 for c in cards if c["S6_ghost"])} '
          f'(битых ссылок всего {sum(c["S6_ghost"] for c in cards)}, путей всего {sum(c["S1_paths"] for c in cards)})')
    print(f'нормативных (S5>0): {sum(1 for c in cards if c["S5_norm"])}')
    print(f'старше 90 дней: {sum(1 for c in cards if c["S2_age_d"]>90)}')
    print(f'тело >1400 симв: {sum(1 for c in cards if c["S8_len"]>1400)}')
    print(f'CSV: {OUT}')


if __name__ == '__main__':
    main()
