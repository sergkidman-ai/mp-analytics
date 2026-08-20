#!/usr/bin/env python3
# поток: infra
"""J0 baseline — измерение стоимости контекста и поведения сессий ДО внедрения KB-архитектуры.

Только чтение `/root/.claude/projects/**/*.jsonl`. Ничего не меняет.

Методика (решения Сергея 19.08):
  * НЕ смешивать оценочный размер текста, raw usage, harness overhead и кэш-поля.
  * Мерить raw-поля как есть: input_tokens / cache_creation_input_tokens /
    cache_read_input_tokens / output_tokens.
  * Пол пустого проекта меряется отдельно (контролируемый прогон) и вычитается ТОЛЬКО
    внутри одинаковой конфигурации (version+model+effort+entrypoint). Полевые сессии
    интерактивные, полы сняты в --print → вычитание между режимами НЕ производится.
  * Сравнение — парное: поток сам с собой, конфигурация с той же конфигурацией.
"""
import json, os, re, glob, statistics as st
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter

PROJ = '/root/.claude/projects'
OUT = '/opt/mp-analytics/reports/data/kb_baseline_2026-08-19.json'
CORR_DUMP = '/opt/mp-analytics/reports/data/kb_correction_candidates.json'
GS_DUMP = '/opt/mp-analytics/reports/data/kb_goldset_candidates.json'
MEM_DIRS = {'repo': f'{PROJ}/-opt-mp-analytics/memory', 'root': f'{PROJ}/-root/memory'}

CORR = re.compile(
    r'(не так\b|не верно|неверно|неправильно|я же (говорил|просил|сказал)|'
    r'сколько раз|опять ты|опять же|запомни|не надо было|перестань|'
    r'ты (снова|опять)|это не то|я просил не)', re.I)
PATH_RE = re.compile(r'\b((?:tools|ops|collectors|reports|core|web|docs|migrations|tests)/[\w./-]+)')
STOP = set('''который которая чтобы просто нужно надо потому этого этому этой этот эти
только можно нельзя после перед через между сейчас вообще значит должен должна будет было
сделай сделать сделал давай пожалуйста ответь скажи покажи файл файлы строк строки'''.split())


def classify_files():
    """189 файлов = сессии верхнего уровня + транскрипты субагентов."""
    main = sorted(glob.glob(f'{PROJ}/*/*.jsonl'))
    sub = sorted(glob.glob(f'{PROJ}/*/*/subagents/*.jsonl'))
    other = [f for f in glob.glob(f'{PROJ}/**/*.jsonl', recursive=True)
             if f not in set(main) | set(sub)]
    return main, sub, other


def parse(path):
    s = {'first_usage': None, 'first_cfg': None, 'version': None, 'model': None,
         'effort': None, 'permission': None, 'entrypoint': None, 'cwd': None, 'branch': None,
         'ts_first': None, 'ts_last': None, 'n_user': 0, 'n_asst': 0, 'compacts': 0,
         'corr': [], 'mem_reads': 0, 'repo_reads': Counter(), 'questions': [],
         'usage_fields': Counter(), 'lines': 0}
    for line in open(path, encoding='utf-8', errors='replace'):
        s['lines'] += 1
        try:
            o = json.loads(line)
        except Exception:
            continue
        ts = o.get('timestamp')
        if ts:
            s['ts_first'] = s['ts_first'] or ts
            s['ts_last'] = ts
        for k, f in (('version', 'version'), ('cwd', 'cwd'), ('gitBranch', 'branch'),
                     ('effort', 'effort'), ('permissionMode', 'permission'),
                     ('entrypoint', 'entrypoint')):
            if o.get(k) and not s[f]:
                s[f] = o[k]
        m = o.get('message') or {}
        t = o.get('type')
        if t == 'assistant':
            s['n_asst'] += 1
            s['model'] = s['model'] or m.get('model')
            u = m.get('usage') or {}
            for k in u:
                s['usage_fields'][k] += 1
            if u and s['first_usage'] is None:
                s['first_usage'] = {
                    'input_tokens': u.get('input_tokens', 0),
                    'cache_creation_input_tokens': u.get('cache_creation_input_tokens', 0),
                    'cache_read_input_tokens': u.get('cache_read_input_tokens', 0),
                    'output_tokens': u.get('output_tokens', 0),
                    'service_tier': u.get('service_tier')}
                s['first_cfg'] = {'version': o.get('version'), 'model': m.get('model'),
                                  'effort': o.get('effort'), 'entrypoint': o.get('entrypoint'),
                                  'permissionMode': o.get('permissionMode')}
            for blk in (m.get('content') or []):
                if isinstance(blk, dict) and blk.get('type') == 'tool_use':
                    inp = blk.get('input') or {}
                    blob = ' '.join(str(inp.get(k, '')) for k in
                                    ('file_path', 'command', 'pattern', 'path'))
                    if '/memory/' in blob and '.md' in blob:
                        s['mem_reads'] += 1
                    for p in PATH_RE.findall(blob):
                        s['repo_reads'][p] += 1
        elif t == 'user':
            if o.get('isCompactSummary') or o.get('isMeta'):
                s['compacts'] += o.get('isCompactSummary', 0) and 1 or 0
                continue
            c = m.get('content')
            txt = (c if isinstance(c, str) else
                   ' '.join(b.get('text', '') for b in c
                            if isinstance(b, dict) and b.get('type') == 'text')
                   if isinstance(c, list) else '')
            if not txt or txt.lstrip().startswith('<'):
                continue
            s['n_user'] += 1
            if CORR.search(txt):
                s['corr'].append({'ts': ts, 'file': os.path.basename(path),
                                  'text': ' '.join(txt.split())[:300]})
            if '?' in txt and 20 < len(txt) < 220:
                s['questions'].append(' '.join(txt.split())[:200])
    return s


def stream_of(cwd, scope):
    if not cwd:
        return 'unknown'
    if 'mp-analytics' not in cwd:
        return 'root-scope' if cwd.startswith('/root') else 'вне проекта'
    ws = os.path.join(cwd, '.workstream')
    if os.path.isfile(ws):
        try:
            v = open(ws).read().strip()
            if v:
                return v
        except Exception:
            pass
    m = re.search(r'worktrees/([a-z]+)[-/]', cwd)
    if m:
        return m.group(1) + ' (по имени дерева)'
    return 'main' if cwd.rstrip('/').endswith('mp-analytics') else 'other'


def salient(txt):
    return [w for w in re.findall(r'[а-яёa-z_]{5,}', txt.lower()) if w not in STOP]


def main():
    main_files, sub_files, other_files = classify_files()
    sessions, corr_all, questions = [], [], []
    sub_stats = {'files': len(sub_files), 'with_usage': 0}

    for path in sub_files:
        s = parse(path)
        if s['first_usage']:
            sub_stats['with_usage'] += 1

    field_presence = Counter()
    for path in main_files:
        scope = os.path.basename(os.path.dirname(path))
        s = parse(path)
        fu = s['first_usage']
        for f in ('version', 'model', 'effort', 'permission', 'entrypoint', 'cwd', 'branch'):
            if s[f]:
                field_presence[f] += 1
        if not fu:
            sessions.append({'file': os.path.basename(path), 'scope': scope,
                             'usable': False, 'lines': s['lines']})
            continue
        raw = (fu['input_tokens'] + fu['cache_creation_input_tokens']
               + fu['cache_read_input_tokens'])
        cfg = s['first_cfg'] or {}
        rec = {'file': os.path.basename(path), 'scope': scope, 'usable': True,
               'cwd': s['cwd'], 'branch': s['branch'], 'stream': stream_of(s['cwd'], scope),
               'version': cfg.get('version') or s['version'], 'model': cfg.get('model') or s['model'],
               'effort': cfg.get('effort') or s['effort'],
               'entrypoint': cfg.get('entrypoint') or s['entrypoint'],
               'permission': cfg.get('permissionMode') or s['permission'],
               'ts': s['ts_first'], 'ts_last': s['ts_last'],
               'raw_turn1_total': raw, **fu,
               'n_user': s['n_user'], 'n_asst': s['n_asst'], 'compacts': s['compacts'],
               'corr_count': len(s['corr']), 'mem_reads': s['mem_reads'],
               'rediscovery_proxy': sum(v for k, v in s['repo_reads'].items()),
               'lines': s['lines']}
        rec['config_key'] = f"{rec['version']}|{rec['model']}|{rec['effort']}|{rec['entrypoint']}"
        sessions.append(rec)
        corr_all += s['corr']
        for q in s['questions']:
            questions.append({'scope': scope, 'ts': s['ts_first'], 'q': q})

    usable = [r for r in sessions if r.get('usable') and r['raw_turn1_total'] > 1000]
    mp = [r for r in usable if 'mp-analytics' in r['scope'] or r['scope'] == '-root']

    def agg(rows, key):
        v = sorted(r[key] for r in rows if r.get(key) is not None)
        if not v:
            return None
        return {'n': len(v), 'median': int(st.median(v)),
                'p90': int(v[min(len(v) - 1, int(len(v) * 0.9))]),
                'min': min(v), 'max': max(v)}

    # --- дрейф памяти: где физически лежат карточки и когда писались
    now = datetime.now()
    drift = {}
    for label, d in MEM_DIRS.items():
        files = glob.glob(d + '/*.md')
        mt = [(f, datetime.fromtimestamp(os.path.getmtime(f))) for f in files]
        drift[label] = {
            'cards': len(files),
            'last_7d': sum(1 for _, t in mt if (now - t).days <= 7),
            'last_30d': sum(1 for _, t in mt if (now - t).days <= 30),
            'newest': max((t for _, t in mt), default=None).isoformat() if mt else None}

    # --- рецидив коррекции: значимое слово в корректирующих репликах ≥2 разных сессий за 30 дней
    def dt(x):
        try:
            return datetime.fromisoformat(x.replace('Z', '+00:00'))
        except Exception:
            return None
    hits = defaultdict(list)
    for c in corr_all:
        d = dt(c['ts'] or '')
        if not d:
            continue
        for w in set(salient(c['text'])):
            hits[w].append((d, c['file']))
    recurrent = {}
    for w, hs in hits.items():
        hs.sort()
        for i in range(len(hs)):
            same = {f for d, f in hs if timedelta() <= d - hs[i][0] <= timedelta(days=30)}
            if len(same) >= 2:
                recurrent[w] = len(same)
                break

    by_stream = defaultdict(list)
    for r in mp:
        by_stream[r['stream']].append(r)

    out = {
        'generated': datetime.now(timezone.utc).isoformat(),
        'files': {'total': len(main_files) + len(sub_files) + len(other_files),
                  'sessions': len(main_files), 'subagents': sub_stats['files'],
                  'subagents_with_usage': sub_stats['with_usage'], 'other': len(other_files)},
        'sessions_usable': len(usable), 'sessions_mp': len(mp),
        'sessions_unusable': sum(1 for r in sessions if not r.get('usable')),
        'field_presence': {k: f'{v}/{len(main_files)}' for k, v in field_presence.items()},
        'raw_turn1_total': agg(mp, 'raw_turn1_total'),
        'input_tokens': agg(mp, 'input_tokens'),
        'cache_creation_input_tokens': agg(mp, 'cache_creation_input_tokens'),
        'cache_read_input_tokens': agg(mp, 'cache_read_input_tokens'),
        'output_tokens': agg(mp, 'output_tokens'),
        'by_config': {k: {'n': len(v), 'raw_median': int(st.median([r['raw_turn1_total'] for r in v]))}
                      for k, v in sorted(
                          ((k, [r for r in mp if r['config_key'] == k])
                           for k in {r['config_key'] for r in mp}), key=lambda x: -len(x[1]))},
        'by_stream': {k: {'n': len(v),
                          'raw': agg(v, 'raw_turn1_total'),
                          'mem_reads_median': st.median([r['mem_reads'] for r in v]),
                          'corr_total': sum(r['corr_count'] for r in v),
                          'cwd_examples': sorted({(r['cwd'] or '')[:60] for r in v})[:2]}
                      for k, v in sorted(by_stream.items(), key=lambda x: -len(x[1]))},
        'mem_reads': agg(mp, 'mem_reads'),
        'rediscovery_proxy': agg(mp, 'rediscovery_proxy'),
        'compacts_total': sum(r['compacts'] for r in mp),
        'corrections_total': len(corr_all),
        'corrections_sessions': sum(1 for r in mp if r['corr_count']),
        'recurrent_words': sorted(recurrent.items(), key=lambda x: -x[1])[:20],
        'memory_drift': drift,
        'sessions': sessions,
    }
    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(corr_all, open(CORR_DUMP, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(questions, open(GS_DUMP, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    print('файлов всего:', out['files'], )
    print('сессий пригодных:', out['sessions_usable'], '| mp:', out['sessions_mp'],
          '| без usage:', out['sessions_unusable'])
    print('raw_turn1_total:', out['raw_turn1_total'])
    print('cache_read:', out['cache_read_input_tokens'])
    print('cache_creation:', out['cache_creation_input_tokens'])
    print('input_tokens:', out['input_tokens'])
    print('карточек/сессия:', out['mem_reads'])
    print('коррекций-кандидатов:', out['corrections_total'], '| рецидивных слов:', len(out['recurrent_words']))
    print('дрейф памяти:', drift)
    print('топ-конфигураций:', list(out['by_config'].items())[:3])


if __name__ == '__main__':
    main()
