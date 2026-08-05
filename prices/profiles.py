# поток: prc
# -*- coding: utf-8 -*-
"""
Профили прайсов поставщиков: где лежит письмо, какие колонки читать, как понимать остаток.

Один профиль = один поставщик (группа в имени оприходования). Разбор прайса не должен
ничего угадывать: колонки ищутся по ЗАГОЛОВКУ, остаток — по явной таблице соответствий.
Незнакомая формулировка остатка не превращается в число молча — строка уходит в отчёт.
"""
import re
from dataclasses import dataclass, field

# Склад и организация, на которые оприходуются остатки поставщиков.
STORE_REMOTE = "04bba1be-cb03-11ea-0a80-01600001ca2c"          # «Удаленный склад»
ORG_DIGITAL = "a5e7ee84-8d63-41f2-9fae-0be2ef90462b"           # ООО «ЦИФРОВОЙ КВАДРАТ»

# Позиций в одном оприходовании. 100 — как в текущей ручной загрузке, менять без нужды не стоит.
POSITIONS_PER_DOC = 100


def norm(text):
    """Схлопнуть пробелы и регистр: 'больше 0 , но меньше 10' == 'Больше 0, но меньше 10'."""
    s = str(text or "").replace("\xa0", " ").strip().lower()
    s = re.sub(r"\s*,\s*", ", ", s)
    return re.sub(r"\s+", " ", s)


@dataclass
class Profile:
    key: str                      # ключ группы: он же префикс имени оприходования
    title: str                    # человеческое имя поставщика
    mail_folder: str              # IMAP-папка с прайсом (разделитель '|')
    columns: dict                 # логическое имя колонки -> заголовок в файле
    stock_map: dict               # нормализованный текст остатка -> количество
    currency: str = "RUB"         # валюта цен в прайсе
    markup: str = "1.00"          # надбавка к курсу ЦБ (Decimal-строкой)
    supplier_ids: list = field(default_factory=list)   # контрагенты группы в МС
    sheet: str = None             # имя листа; None — первый
    header_scan_rows: int = 25    # в скольких верхних строках искать шапку

    def qty(self, raw):
        """Остаток прайса -> число. None, если формулировка незнакома."""
        if raw is None:
            return None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw) if raw > 0 else None
        text = norm(raw)
        if text in self.stock_map:
            return float(self.stock_map[text])
        # чистое число текстом ('12', '12,5')
        plain = text.replace(",", ".")
        if re.fullmatch(r"\d+(\.\d+)?", plain):
            value = float(plain)
            return value if value > 0 else None
        return None


# --- Колортек -------------------------------------------------------------------
# Остаток поставщик отдаёт текстом-диапазоном. Числа ниже не выдуманы: они восстановлены
# из ручной загрузки 05.08.2026 (1108 позиций, соответствие однозначное, исключений нет).
# Легенда в шапке файла (строки 1-4) даёт тем же корзинам короткие подписи — учтены обе формы.
COLORTEK_STOCK = {
    "больше 0, но меньше 10": 6,
    "меньше десяти": 6,
    "больше 10, но меньше 50": 25,
    "меньше пятидесяти": 25,
    "больше 50, но меньше 100": 75,
    "до 100": 75,
    "больше 100": 100,
}

PROFILES = {
    "colortek": Profile(
        key="colortek",
        title="Колортек",
        mail_folder="Прайсы Поставщиков|Колортек",
        columns={
            "article": "Артикул",       # артикул поставщика = поле «Артикул» в МС
            "name": "Номенклатура",
            "stock": "Значение",
            "price": "СПЕЦ",            # цена в долларах
        },
        stock_map=COLORTEK_STOCK,
        currency="USD",
        markup="1.03",                  # курс ЦБ на дату загрузки + 3%
        supplier_ids=["32425f47-88cf-11e7-6b01-4b1d000ab2f4"],   # ООО «КОЛОРТЕК РУС»
        sheet="TDSheet",
    ),
}


def get_profile(key):
    if key not in PROFILES:
        raise KeyError(f"нет профиля поставщика '{key}'; известные: {', '.join(sorted(PROFILES))}")
    return PROFILES[key]
