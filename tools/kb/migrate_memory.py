#!/usr/bin/env python3
# поток: infra
"""Миграция карточек памяти в канонический стор /opt/mp-knowledge (MVP-1).

Копирует, НИЧЕГО не удаляя из источников. На каждую карточку — строка в
journal/migration_2026-08-20.jsonl: откуда → класс → куда → сигналы → причина.

Классы: KEEP (в memory/), MERGE_REVIEW (в inbox/merge_review/, решает человек),
STALE (в archive/stale/), UNCERTAIN (в quarantine/), OUT_OF_SCOPE (не копируем).

Запуск: ./venv/bin/python tools/kb/migrate_memory.py [--dry]
"""
import json, re, shutil, sys, time
from pathlib import Path

K = Path('/opt/mp-knowledge')
SRC = {'repo': Path('/root/.claude/projects/-opt-mp-analytics/memory'),
       'root': Path('/root/.claude/projects/-root/memory')}
JOURNAL = K / 'journal/migration_2026-08-20.jsonl'
DRY = '--dry' in sys.argv

# --- решения классификации (принимаются здесь явно, а не выводятся эвристикой) ---
MERGE_REVIEW = {
    'repo:feedback_32mb_context_deaths':
        ('feedback_session_32mb_deaths', 'орфан вне индекса, но БОГАЧЕ индексированного близнеца '
         '(документированные случаи mp-mkt/review2) — склейка требует построчного решения человека'),
    'repo:feedback_session_desync_stale_ref':
        ('feedback_worktree_docs_sync', 'орфан вне индекса, содержит разбор эталона 3909 vs 3741.77, '
         'которого нет у индексированного близнеца — склейка за человеком'),
    'root:gab-no-estimated-dims':
        ('repo:gab-no-estimated-dims', 'одноимённая карточка в двух скоупах, тела расходятся на 30 строк; '
         'версия репозитория богаче (4080 vs 3389 б), но уникальные куски root не проверены'),
}
STALE = {
    'root:reports-mp-deploy-pending':
        'состояние снято: ветки deploy/reports-mp-web больше нет, «Отчёты МП» влиты в main '
        '(5509213 merge fin/ozon-expense-detail). Карточка описывает ожидание, которого нет',
}
UNCERTAIN = {
    'root:fin-domain-handoff': 'снимок передачи потока: путь .claude/worktrees/fin-night/HANDOFF_FIN.md мёртв '
                               '(дерева нет), содержимое смешанное — часть выводов может быть жива, часть нет',
    'root:mkt-domain-handoff': 'снимок передачи потока mkt, актуальность не проверяется дёшево',
    'root:main-consolidation-2026-07': 'утверждает «продовый cutover ещё не сделан» — состояние на июль, '
                                       'проверить нечем без ручного разбора',
}
OUT_OF_SCOPE = {
    'root:sokol-server-project': 'знание о ДРУГОМ проекте (/opt/sokol-server) — в периметр mp не входит',
    'root:deliver-results-as-artifact': 'общая рабочая привычка, не знание mp-analytics',
    'root:limit-warning-statusline': 'глобальная настройка Claude Code, не знание проекта',
    'root:claude-code-tmux-resume': 'глобальный приём работы с Claude Code, не знание проекта',
}

STREAM_RULES = [
    ('gab', r'габарит|короб|dims|supplier_dims|carton'),
    ('fin', r'cogs|себест|маржа|margin_by_sku|сторно|realization|отчёт.?мп|финанс|p&l'),
    ('mkt', r'реклам|ставк|джем|jam|abc|поиск|показ|воронк|cpc|дрр'),
    ('rev', r'отзыв|автоответ|feedback-moderation|вопрос'),
    ('inv', r'банк|платёж|выписк|alfa|sber|paymentin|paymentout|приёмк'),
    ('ret', r'возврат.*fbs|штрихкод|бот возвратов'),
    ('infra', r'сессии|контекст|токен|worktree|memory|git|tmux|claude code|codex'),
]


def slugify(name):
    s = name.lower()
    s = re.sub(r'^(project_mp_|feedback_|project_|mp_)', '', s)
    s = s.replace('_', '-')
    s = re.sub(r'[^a-z0-9-]', '-', s)
    return re.sub(r'-+', '-', s).strip('-')


def parse(p):
    txt = p.read_text(encoding='utf-8', errors='replace')
    fm, body = '', txt
    if txt.startswith('---'):
        e = txt.find('\n---', 3)
        if e > 0:
            fm, body = txt[3:e], txt[e + 4:]
    g = lambda k: (re.search(rf'^\s*{k}:\s*(.+)$', fm, re.M).group(1).strip().strip('"')
                   if re.search(rf'^\s*{k}:\s*(.+)$', fm, re.M) else '')
    return {'name': g('name') or p.stem, 'description': g('description'),
            'type': g('type') or 'project', 'body': body.strip(), 'raw': txt}


def stream_of(c):
    hay = (c['description'] + ' ' + c['body'][:1200]).lower()
    for st, rx in STREAM_RULES:
        if re.search(rx, hay):
            return st
    return 'infra'


def render(c, slug, scope, src_slug, cls, extra=None):
    norm = bool(re.search(r'запрещ|только по|нельзя|обязан|всегда|никогда|не трогать',
                          c['body'], re.I))
    meta = [f'  type: {c["type"]}', f'  stream: {stream_of(c)}', f'  status: {cls.lower()}',
            f'  source_scope: {scope}', f'  source_slug: {src_slug}',
            f'  migrated: 2026-08-20', f'  contains_norm: {str(norm).lower()}']
    if len(c['body']) > 1400:
        meta.append('  oversize: true')
    for k, v in (extra or {}).items():
        meta.append(f'  {k}: {v}')
    desc = c['description'].replace('"', "'")
    return (f'---\nname: {slug}\ndescription: "{desc}"\nmetadata:\n' +
            '\n'.join(meta) + f'\n---\n\n{c["body"]}\n')


def main():
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    (K / 'inbox/merge_review').mkdir(parents=True, exist_ok=True)
    rows, used = [], {}
    counts = {}
    for scope, d in SRC.items():
        idx_links = set()
        mem = d / 'MEMORY.md'
        if mem.exists():
            idx_links = set(re.findall(r'\]\(([^)]+\.md)\)', mem.read_text(encoding='utf-8')))
        for p in sorted(d.glob('*.md')):
            if p.name == 'MEMORY.md':
                continue
            key = f'{scope}:{p.stem}'
            c = parse(p)
            if key in OUT_OF_SCOPE:
                cls, dest_dir, reason = 'OUT_OF_SCOPE', None, OUT_OF_SCOPE[key]
            elif key in MERGE_REVIEW:
                cls, dest_dir, reason = 'MERGE_REVIEW', K / 'inbox/merge_review', MERGE_REVIEW[key][1]
            elif key in STALE:
                cls, dest_dir, reason = 'STALE', K / 'archive/stale', STALE[key]
            elif key in UNCERTAIN:
                cls, dest_dir, reason = 'UNCERTAIN', K / 'quarantine', UNCERTAIN[key]
            else:
                cls, dest_dir, reason = 'KEEP', K / 'memory', 'тема уникальна, конфликтов не найдено'
            counts[cls] = counts.get(cls, 0) + 1
            slug = slugify(c['name'])
            if cls in ('KEEP',):
                if slug in used:
                    slug = f'{slug}-{scope}'
                used[slug] = key
            dest = None
            if dest_dir is not None:
                fname = f'{slug}.md' if cls == 'KEEP' else f'{scope}--{p.stem}.md'
                dest = dest_dir / fname
                if not DRY:
                    if cls == 'KEEP':
                        dest.write_text(render(c, slug, scope, p.stem, cls), encoding='utf-8')
                    else:
                        extra = {'merge_with': MERGE_REVIEW[key][0]} if cls == 'MERGE_REVIEW' else {}
                        dest.write_text(render(c, dest.stem, scope, p.stem, cls, extra),
                                        encoding='utf-8')
            rows.append({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'source': str(p),
                         'source_scope': scope, 'source_slug': p.stem,
                         'in_source_index': p.name in idx_links, 'class': cls,
                         'dest': str(dest) if dest else None, 'new_slug': slug if cls == 'KEEP' else None,
                         'bytes': len(c['body']), 'reason': reason})
    if not DRY:
        with JOURNAL.open('w', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
    for k in sorted(counts):
        print(f'{k:14s} {counts[k]}')
    print(f'ИТОГО источников: {len(rows)} | в memory/: {counts.get("KEEP",0)}')
    print(f'журнал: {JOURNAL}{" (DRY)" if DRY else ""}')


if __name__ == '__main__':
    main()
