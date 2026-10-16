# поток: mkt
"""Read-only interpretation of supplier text using the existing price dictionary.

Returns explicit evidence only. Silence never means absence of a chip.
Supplier-specific Blossom W/chip is documented in docs/MS_CARD_FIELDS.md.
"""
import re
from prices import features

LABELS = {'chip': 'с чипом', 'nochip': 'без чипа',
          'chip_free': 'чип без счётчика'}


def recognize(text, supplier=''):
    s = features.norm(text)
    if not s or s in ('не указан', 'неуказан', 'неизвестно', '?', 'none', 'null'):
        return {'classes': [], 'evidence': [], 'rule': 'нет явного указания'}
    # Generic mention of a chip or templated "not needed" is not supply evidence.
    s = re.sub(r'не\s+требуется\s+чип|чип\s+не\s+требуется|не\s+требует\w*\s+чип', '', s)
    s = re.sub(r'чип\s*(?:не\s+указан|неизвестен|не\s+определен)', '', s)
    found = []
    for code, pattern in features.CHIP_RE:
        if code == 'chip':
            # Mask absent/free indications before recognizing ordinary chip mentions.
            continue
        found.extend((m.start(), m.end(), code, m.group()) for m in pattern.finditer(s))
    free_phrase = r'(?:[сc]\s+)?чип\w*\s+(?:безлимит\w*|без\s*счет\w*)|(?:with\s+)?chip\s+(?:unlimited|no\s*count\w*)'
    found.extend((m.start(),m.end(),'chip_free',m.group())
                 for m in re.finditer(free_phrase,s,re.I))
    extra = re.compile(r'(?:необходим|требует\w*|нужен)\s+чип|чип\s+отдельн\w*|chipless', re.I)
    found.extend((m.start(), m.end(), 'nochip', m.group()) for m in extra.finditer(s))
    if re.search(r'blossom|блоссом', supplier, re.I):
        found.extend((m.start(), m.end(), 'nochip', m.group())
                     for m in re.finditer(r'\bw/chip\b', s, re.I))
    spans = [(a,b) for a,b,_,_ in found]
    masked = list(s)
    for a,b in spans: masked[a:b] = ' '*(b-a)
    remaining = ''.join(masked)
    # Shared dictionary handles phrases such as "с многоразовым чипом Longlife".
    generic = features.chip(remaining)
    if generic:
        found.extend((m.start(),m.end(),generic,m.group()) for code,pattern in features.CHIP_RE
                     if code == generic for m in pattern.finditer(remaining))
        # "чипом" is not a whole-word чип: preserve inflected positive wording.
    if re.search(r'\b[сc]\s+(?:\w+\s+){0,3}чип\w*|with\s+chip|оснащен\w*\s+чип\w*', remaining):
        found.append((0,0,'chip','явная положительная формулировка'))
    classes = sorted({LABELS[c] for _,_,c,_ in found})
    return {'classes': classes, 'evidence': sorted({v for _,_,_,v in found}),
            'rule': 'словарь prices.features и документированные дополнения' if found else 'нет явного указания'}
