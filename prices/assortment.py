# поток: prc
"""Business assortment gate, before MS matching or novelty ingestion.
Keep finished printer consumables and approved refill/printing media.
Supplier category restrictions remain authoritative; unknown goods are not new cartridges.
"""
import re

VERSION='2026-11-06-print-consumables'

# Compatibility text must never turn a printer/part into a cartridge, or vice versa.
COMPAT=re.compile(r'\bдля\b',re.I)
EQUIPMENT=re.compile(r'\b(?:монитор\w*|monitor|экран\w*|screen|проектор\w*|projector|принтер\w*|printer|мфу|сканер\w*|scanner|плоттер\w*|ламинатор\w*|кресл\w*|стол\w*|сейф\w*|источник\s+бесперебойного)\b',re.I)
PARTS=re.compile(r'\b(?:(?:магнитн\w*|прижимн\w*|тефлонов\w*|резинов\w*)\s+)?(?:вал\w*|ролик\w*|ракел\w*|лезви\w*|шестер\w*|втулк\w*|подшипник\w*)\b',re.I)
OUTSIDE=re.compile(r'\b(?:фильтр\w*|filter|hepa|фотобумаг\w*|бумаг\w*|лампа\w*|креплен\w*|кронштейн\w*|пульт\w*|кабел\w*)\b',re.I)
CHIP=re.compile(r'\b(?:чип\w*|chip)\b',re.I)
REFILLABLE=re.compile(r'\bперезаправляем\w*\s+картридж\w*|^\W*пзк\b',re.I)
CLEANING=re.compile(r'\b(?:промывочн\w*|очищающ\w*)\s+жидкост\w*',re.I)
CONSUMABLE=re.compile(r'\b(?:картридж\w*|тонер\w*|чернил\w*|фотобарабан\w*|снпч|printhead|печатающ\w*\s+головк\w*|головк\w*\s+печат\w*|фьюзерн\w*\s+масл\w*|краск\w*\s+полиграфическ\w*|драм[-\s]?юнит\w*|драм[-\s]?картридж\w*|девелопер\w*|printer\s+(?:cartridge|ribbon|ink|toner)|cartridge|toner|ink|drum|developer|ribbon|риббон\w*|термо[пп]?л[её]нк\w*)\b',re.I)
UNITS=re.compile(r'\b(?:блок|модуль|юнит|узел)\s+(?:проявк\w*|перенос\w*|фотобарабан\w*|термозакреплен\w*|фиксаци\w*)\b|\b(?:термоблок\w*|фьюзер\w*)\b|\bсервисн\w*\s+комплект\w*\b',re.I)
KITS=re.compile(r'\b(?:набор\w*\s+для\s+печат\w*|заправочн\w*\s+(?:набор|комплект)\w*|(?:набор|комплект)\w*\s+для\s+заправк\w*)\b',re.I)
CONTAINER=re.compile(r'\b(?:[eеё]мкост\w*|контейнер\w*|бункер\w*)\b.*\b(?:чернил\w*|тонер\w*|epson\s+(?:c13t\d+|t\d{4})\b)',re.I)
TAPE=re.compile(r'\b(?:лент\w*|пл[её]нк\w*)\b.*(?:термо|красящ|матричн|этикет|накле|принтер|printer|tze|dymo|brother)|\bкрасящ\w*\s+лент\w*\b',re.I)


def name_reason(name,category_confirmed=False):
    text=re.sub(r'\s+',' ',str(name or '')).strip()
    head=COMPAT.split(text,maxsplit=1)[0]
    if not text:return 'не определён вид товара'
    if REFILLABLE.search(head):return 'перезаправляемые картриджи не берём'
    if CLEANING.search(head):return 'промывочную жидкость не берём'
    if OUTSIDE.search(head):return 'оборудование или посторонний расходник'
    if re.search(r'\b(?:видео)?проектор\w*\b|\bprojector\b',text,re.I):return 'оборудование или аксессуар проектора'
    # A finished consumable may mention its printer model before an explicit "для".
    positive=CONSUMABLE.search(head) or UNITS.search(head) or KITS.search(text) or CONTAINER.search(text) or TAPE.search(text)
    part=PARTS.search(head);chip=CHIP.search(head)
    if chip and (not positive or chip.start()<positive.start()):return 'чип — не готовый расходник'
    if part and (not positive or part.start()<positive.start()):return 'ремонтная запчасть'
    equipment=EQUIPMENT.search(head)
    if equipment and (not positive or equipment.start()<positive.start()):return 'оборудование, не расходник'
    if positive:return None
    if category_confirmed:return None
    return 'не подтверждён разрешённый вид расходника'


def row_reason(row,profile=None,check_categories=True):
    restricted=bool(profile and profile.keep_categories)
    if check_categories and restricted and not profile.keep_category(row):return 'категория поставщика вне ассортимента'
    confirmed=bool(restricted and row.get('group') and profile.keep_category(row))
    return name_reason(row.get('name'),category_confirmed=confirmed)


def filter_rows(rows,profile=None):
    keep=[];off=[]
    for row in rows:
        reason=row_reason(row,profile)
        if reason:off.append(dict(row,reason='category_off',assortment_reason=reason))
        else:keep.append(row)
    return keep,off
