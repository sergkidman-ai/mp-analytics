# поток: prc
"""Source facts and provenance for reproducible WB names; no alternative formatter."""
import copy
import hashlib
import json

from core import db
SOURCE_VERSION = "2026-10-16.2"
FACT_KEYS = ("kind", "model", "brand", "color", "chip", "capacity", "content")
SOURCE_FIELDS = ("consumable_type", "title", "additional_title", "printer_models",
                 "ink_colors", "chip", "capacity", "volume_ml")


def fact_options():
    from prices.features import BRANDS
    from prices.ms_import import tc_kinds
    from tools.prc.wb_fill import COLOR_OK, CHIP_WORD
    return {"kind": tc_kinds(), "brand": list(BRANDS), "color": sorted(COLOR_OK),
            "chip": list(CHIP_WORD), "capacity": ["Обычная", "Увеличенная", "Эконом"]}


def merge_facts(raw, facts, evidence, kinds):
    """Fill missing source facts only. Never override catalogue/supplier evidence."""
    from prices import features
    from tools.prc.wb_fill import COLOR_OK, CHIP_WORD
    out = copy.deepcopy(raw or {})
    facts = {k: str(v or "").strip() for k, v in (facts or {}).items() if v}
    if set(facts) - set(FACT_KEYS):
        raise ValueError("неизвестные исходные характеристики Названия WB")
    if not facts:
        return out
    if not str(evidence or "").strip():
        raise ValueError("укажите источник подтверждения характеристик Названия WB")
    for key, value in facts.items():
        field = {"kind": "consumable_type", "model": "title", "brand": "printer_models",
                 "color": "ink_colors", "chip": "chip", "capacity": "capacity",
                 "content": "volume_ml"}[key]
        if key == "kind" and value not in kinds:
            raise ValueError("тип расходника вне словаря ТК")
        if key == "model":
            from prices.ms_import import looks_like_model
            if not looks_like_model(value) or any(c in value for c in "\n\r"):
                raise ValueError("модель должна быть кодом расходника из подтверждённого источника")
        if key == "brand":
            brands = features.brand(value)
            if len(brands) != 1 or value.casefold() not in features.BRAND_BY_LOWER:
                raise ValueError("бренд принтера вне словаря")
            supplied = [{"brand": next(iter(brands))}]
        elif key == "color":
            colors = [c.strip().lower() for c in value.split("/")]
            if any(c not in COLOR_OK for c in colors):
                raise ValueError("цвет вне словаря Названия WB")
            supplied = colors
        else:
            supplied = value
        if key == "chip" and value not in CHIP_WORD:
            raise ValueError("чип вне словаря Названия WB")
        if key == "capacity" and value not in ("Обычная", "Увеличенная", "Эконом"):
            raise ValueError("неизвестный ресурс Названия WB")
        current = out.get(field)
        # An ordinary catalogue resource must not be silently promoted to XL.
        if current and current != supplied:
            raise ValueError(f"характеристика «{key}» противоречит исходным данным — сверить источник")
        out[field] = supplied
    return out


def fingerprint(source):
    data = {k: source.get(k) for k in (
        "source_code", "price_name", "price_article", "raw", "facts", "evidence", "rules_version", "source_version")}
    data["raw"] = {k: (source.get("raw") or {}).get(k) for k in SOURCE_FIELDS}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def prepare_record(rec, source):
    """Journal provenance BEFORE POST, so a failed response cannot lose the source."""
    count = db.execute("""INSERT INTO prc_wb_name_source
        (card_code, external_code, source, source_hash)
        VALUES (%s, %s, %s::jsonb, %s)
        ON CONFLICT (card_code) DO UPDATE SET
          external_code = excluded.external_code, source = excluded.source,
          source_hash = excluded.source_hash, updated_at = now()
        WHERE prc_wb_name_source.ms_id IS NULL""",
        (rec["Код"], rec["Внешний код"], json.dumps(source, ensure_ascii=False), source["token"]))
    if count != 1:
        raise ValueError(f"{rec['Код']}: источник уже закреплён за созданной карточкой; сверить МС")


def mark_created(code, ms_id):
    db.execute("UPDATE prc_wb_name_source SET ms_id = %s, updated_at = now() WHERE card_code = %s",
               (ms_id, code))


def for_product(product):
    """Read-only recalculation for existing cards, preserving their original source code.

    This does not add cards to the frozen mass-write plan. Changed source facts are
    rechecked against current TC; the saved final WB string is never an input.
    """
    from prices.ms_import import wb_details
    rows = db.query("SELECT ms_id, external_code, source FROM prc_wb_name_source WHERE card_code = %s",
                    (product.get("code"),))
    if not rows:
        return wb_details(product.get("externalCode") or "", product.get("name") or "",
                          price_article=product.get("article") or "")
    row = rows[0]
    if str(row["ms_id"]) != str(product.get("id")) or row["external_code"] != product.get("externalCode"):
        raise ValueError("сохранённый источник Названия WB не соответствует карточке МС")
    source = row["source"]
    if (product.get("name") or "") != source["card_name"]:
        raise ValueError("наименование карточки изменилось — сверить сохранённый источник Названия WB")
    return wb_details(source["source_code"], source["price_name"],
                      facts=source.get("facts"), evidence=source.get("evidence", ""),
                      price_article=source.get("price_article", ""))
