# поток: rev
"""core/text_norm.py — нормализация омоглифов перед парсингом русских текстов карточек.

Поставщики контента (и наши собственные генераторы описаний) регулярно подмешивают в кириллицу
латинские буквы-двойники. Инцидент 6806 (13.08.2026): в описании карточки ВБ стояло
«оснащен c чипом без счетчика» с ЛАТИНСКОЙ `c` — регексп `с\\s*чипом` по русскому тексту её не
увидел, чип остался неизвестным, движок добрал «без чипа» из чужой Ozon-карточки и пообещал
покупателю переставить чип со старого картриджа.

`fix_homoglyphs()` приводит латинские двойники к кириллице ТОЛЬКО там, где текст русский:

- в токене вперемешку кириллица и латиница, кириллицы не меньше → двойники в кириллицу
  («оснaщен», «Cовместимый»);
- одиночная латинская буква-двойник, ОБА соседних слова русские → в кириллицу
  («оснащен c чипом»).

Чисто латинские слова не трогаем никогда: `HP`, `LJ Pro`, `Black`, артикулы `W1510A`, модели
`C822` должны остаться собой. Одиночную букву рядом с цифрами тоже не трогаем — это обозначение
модели («принтер P 1102»), а не русский текст.
"""
import re

# только визуально неотличимые пары латиница → кириллица
HOMOGLYPHS = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
}

_LAT = re.compile(r"[A-Za-z]")
_CYR = re.compile(r"[А-Яа-яЁё]")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_DIGIT = re.compile(r"\d")


def _swap(word):
    return "".join(HOMOGLYPHS.get(ch, ch) for ch in word)


def fix_homoglyphs(text):
    """Латинские буквы-двойники → кириллица в русских местах текста. Не-строки возвращает как есть."""
    if not isinstance(text, str) or not text or not _CYR.search(text):
        return text
    words = list(_WORD.finditer(text))
    out, cur = [], 0
    for i, m in enumerate(words):
        w = m.group(0)
        n_cyr, n_lat = len(_CYR.findall(w)), len(_LAT.findall(w))
        fixed = w
        if n_cyr and n_lat and n_cyr >= n_lat:
            fixed = _swap(w)                                   # смешанный токен, русский по большинству
        elif n_lat and not n_cyr and len(w) == 1 and w in HOMOGLYPHS:
            prev_w = words[i - 1].group(0) if i else ""
            next_w = words[i + 1].group(0) if i + 1 < len(words) else ""
            # соседние слова русские И вокруг нет цифр (иначе это обозначение модели, не текст)
            gap_before = text[words[i - 1].end():m.start()] if i else ""
            gap_after = text[m.end():words[i + 1].start()] if i + 1 < len(words) else ""
            if (_CYR.search(prev_w) and _CYR.search(next_w)
                    and not _LAT.search(prev_w) and not _LAT.search(next_w)
                    and not _DIGIT.search(gap_before) and not _DIGIT.search(gap_after)):
                fixed = _swap(w)                               # «оснащен c чипом»
        if fixed != w:
            out.append(text[cur:m.start()])
            out.append(fixed)
            cur = m.end()
    if not out:
        return text
    out.append(text[cur:])
    return "".join(out)


def fix_deep(value):
    """То же для вложенных структур: строки внутри dict/list нормализуются рекурсивно."""
    if isinstance(value, str):
        return fix_homoglyphs(value)
    if isinstance(value, list):
        return [fix_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: fix_deep(v) for k, v in value.items()}
    return value


if __name__ == "__main__":
    samples = [
        "Картридж W1510A оснащен c чипом без счетчика, что обеспечивает удобство",   # латинская c
        "Картридж HP LJ Pro 4003 dn, LJ Pro MFP 4103dw черный",                      # трогать нечего
        "Cовместимый картридж для принтеров HP",                                     # латинская C в слове
        "принтер P 1102 подойдёт",                                                   # модель — не трогаем
    ]
    for s in samples:
        print(repr(s), "→", repr(fix_homoglyphs(s)))
