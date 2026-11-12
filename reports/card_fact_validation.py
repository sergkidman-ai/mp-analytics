# поток: rev
"""Narrow deterministic checks for known card-to-answer semantic errors."""
import hashlib
import json
import re


def signature(facts):
    return hashlib.sha256(json.dumps(facts or {}, ensure_ascii=False,
                                    sort_keys=True, default=str).encode()).hexdigest()


def problems(facts, reply, expert=""):
    facts = facts or {}
    text = (reply or "").lower().replace("ё", "е")
    evidence = " ".join(str(facts.get(k) or "") for k in ("name", "annot", "set_info"))
    evidence = (evidence + " " + (expert or "")).lower().replace("ё", "е")
    out = []
    installed = r"(?:чип.{0,20}(?:установлен|встроен)|(?:установлен|встроен).{0,20}чип)"
    if re.search(installed, text) and not re.search(installed, evidence):
        out.append("установка чипа не подтверждена")
    if re.search(r"(?:пере)?став\w*.{0,20}чип|чип.{0,20}перестав", text) and not re.search(
            r"(?:пере)?став\w*.{0,20}чип|чип.{0,20}перестав", evidence):
        out.append("перестановка чипа не подтверждена")
    if facts.get("chip") in ("none", "not_required") and re.search(r"(?:поставляется|картридж|комплект).{0,20}с чипом", text):
        out.append("наличие чипа противоречит данным карточки")
    level = r"(?:отображ|показыв|контрол|отслежив).{0,35}(?:уров|остат)|(?:уров|остат).{0,35}(?:отображ|показыв|контрол)"
    if re.search(level, text) and not re.search(level, evidence):
        out.append("контроль уровня расходника не подтверждён")
    settings = r"без.{0,25}настро|не треб\w*.{0,25}настро|настро\w*.{0,15}не (?:нуж|треб)"
    if re.search(settings, text) and not re.search(settings, evidence):
        out.append("отсутствие дополнительных настроек не подтверждено")
    if facts.get("set_info") and re.search(r"\bодин\s+(?:картридж|цвет)\b|\bтолько\s+(?:красный|желтый|черный|голубой)\b", text):
        if not re.search(r"\bодин\s+картридж\b|\b1\s*(?:шт|картридж)", evidence):
            out.append("единственный картридж/цвет не подтверждён для комплекта")
    return out
