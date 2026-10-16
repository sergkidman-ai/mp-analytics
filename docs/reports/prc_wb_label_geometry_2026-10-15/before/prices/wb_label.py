# поток: prc
"""Shared, deterministic WB label shortening. Never truncates product identifiers.

Character limits are enforced here. Actual two-line layout is a separate release gate
until the original MS print template has been checked.
"""
from dataclasses import dataclass, replace

TARGET_CHARS = 50
MAX_CHARS = 55
RULES_VERSION = '2026-10-15.1'

COLOR_SHORT = {'черный': 'черн.', 'голубой': 'голуб.',
               'пурпурный': 'пурп.', 'желтый': 'желт.'}
TYPE_SHORT = {'Картридж': 'Картр.', 'Набор картриджей': 'Набор картр.',
              'Фотобарабан': 'Фотобараб.', 'Набор фотобарабанов': 'Набор фотобараб.'}


@dataclass(frozen=True)
class Parts:
    prefix: tuple[str, ...]
    extra: str = ''
    colors: tuple[str, ...] = ()
    chip: str = ''
    xl: str = ''
    content: str = ''

    def text(self):
        return ' '.join(x for x in (*self.prefix, self.extra, '/'.join(self.colors),
                                   self.chip, self.xl, self.content) if x)


@dataclass(frozen=True)
class Result:
    name: str = ''
    reason: str = ''
    full_name: str = ''
    suggestion: str = ''
    actions: tuple[str, ...] = ()
    issue: str = ''


def shorten(parts):
    """Preserve semantic parts; shorten only the approved optional parts and words."""
    full_name = parts.text()
    actions = []

    def needs_shortening():
        return len(parts.text()) > TARGET_CHARS

    if needs_shortening() and parts.extra:
        parts = replace(parts, extra='')
        actions.append('убрать дополнительное название')
    if needs_shortening() and parts.xl == 'XL ресурс':
        parts = replace(parts, xl='XL')
        actions.append('XL ресурс → XL')
    if needs_shortening():
        colors = tuple(COLOR_SHORT.get(c, c) for c in parts.colors)
        if colors != parts.colors:
            parts = replace(parts, colors=colors)
            actions.append('сократить цвет по словарю')
    if needs_shortening() and parts.prefix and parts.prefix[0] in TYPE_SHORT:
        parts = replace(parts, prefix=(TYPE_SHORT[parts.prefix[0]], *parts.prefix[1:]))
        actions.append('сократить тип по словарю')
    name = parts.text()
    if len(name) > MAX_CHARS:
        return Result(reason=f'название после сокращений: {len(name)} знаков, предел {MAX_CHARS}; '
                             'нужен разбор без обрезки модели и признаков товара',
                      full_name=full_name, suggestion=name, actions=tuple(actions), issue='length')
    return Result(name=name, full_name=full_name, suggestion=name, actions=tuple(actions))


def validate(value, expected, reason=''):
    """Reject stale/manual text before any writes; return a concrete correction."""
    value = (value or '').strip()
    if not expected:
        return 'Название WB не собрано по формуле: ' + (reason or 'нет подтверждённых данных')
    if value != expected:
        return f'Название WB должно соответствовать общей формуле: «{expected}» ({len(expected)} знаков)'
    if len(value) > MAX_CHARS:
        return f'Название WB: {len(value)} знаков, предел {MAX_CHARS}'
    return ''
