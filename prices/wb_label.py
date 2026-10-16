# поток: prc
"""Shared, deterministic WB label shortening. Never truncates product identifiers.

Both character count and conservative two-line geometry are enforced here. Geometry
is calibrated to the supplied PDFs, not a replacement for the original MS template.
"""
from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path

TARGET_CHARS = 50
MAX_CHARS = 55
RULES_VERSION = '2026-10-15.2'
# A fully visible line in the supplied PDFs is 141.12 pt wide at 9 pt Noto Sans.
# Use a smaller width, rather than guessing the full size of the template field.
LINE_WIDTH_PT = 140
FONT_SIZE_PT = 9
MAX_LINES = 2

COLOR_SHORT = {'черный': 'черн.', 'голубой': 'голуб.',
               'пурпурный': 'пурп.', 'желтый': 'желт.'}
TYPE_SHORT = {'Картридж': 'Картр.', 'Набор картриджей': 'Набор картр.',
              'Фотобарабан': 'Фотобараб.', 'Набор фотобарабанов': 'Набор фотобараб.'}


@lru_cache(maxsize=1)
def _metrics():
    return json.loads((Path(__file__).parent / 'data/wb_label_font_metrics.json').read_text())


def text_width(text):
    """Width at the observed font size. Unknown glyphs cannot be approved."""
    font = _metrics()
    try:
        return sum(font['advance_units'][c] for c in text) * FONT_SIZE_PT / font['units_per_em']
    except KeyError:
        return None


def wrap_lines(text):
    """Wrap at spaces only; never split model codes or color qualifiers."""
    lines, line = [], ''
    for word in text.split():
        width = text_width(word)
        if width is None or width > LINE_WIDTH_PT:
            return None
        trial = (line + ' ' + word).strip()
        width = text_width(trial)
        if width is None:
            return None
        if width <= LINE_WIDTH_PT:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return tuple(lines)


def fits_two_lines(text):
    lines = wrap_lines(text)
    return lines is not None and 0 < len(lines) <= MAX_LINES


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
        text = parts.text()
        return len(text) > TARGET_CHARS or not fits_two_lines(text)

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
    if not fits_two_lines(name):
        return Result(reason='название не помещается в две строки по геометрии образцов '
                             '(NotoSans 9 pt, ширина 140 pt); нужен разбор без обрезки модели',
                      full_name=full_name, suggestion=name, actions=tuple(actions), issue='layout')
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
    if not fits_two_lines(value):
        return 'Название WB не помещается в две строки по геометрии образцов'
    return ''
