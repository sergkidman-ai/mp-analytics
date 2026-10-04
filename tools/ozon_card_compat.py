"""Поток card: экспериментальное исправление блока совместимости Ozon.

Вызывается флагом --repair-models у ozon_card_push; --models-auto разрешает
плановый запуск с лимитом 20 карточек на кабинет (команда 03.10.2026).
Справочник с совпадающим названием НЕ доказывает совместимость с брендом.
Поэтому новые ссылки берём только из здорового родителя того же товара;
без него допускаются лишь согласование одного бренда с уже сохранёнными
моделями и повтор неизменённого блока. Неполные/неоднозначные списки не удаляем.
"""
import copy
import fcntl
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

BRAND = 22888
MODELS = 22889
COMPLEX = 100007
MODEL_CODES = {"attribute_hierarchy_fail", "warning_attribute_values_out_of_range"}
SELLING = {"Продается", "Готов к продаже"}


class UnsafeCompatibility(ValueError):
    pass


def normal(value):
    # Не убираем суффиксы D/DN/DW и не объединяем серии принтеров.
    return re.sub(r"[\s_-]+", "", str(value).upper())


def block(card):
    out = {}
    for attr in card.get("attributes", []) + card.get("complex_attributes", []):
        if attr.get("id") not in (BRAND, MODELS):
            continue
        if attr["id"] in out or attr.get("complex_id") != COMPLEX:
            raise UnsafeCompatibility("неоднозначная структура комплексного блока")
        out[attr["id"]] = {
            "id": attr["id"], "complex_id": COMPLEX,
            "values": [{k: v for k, v in val.items()
                        if k in ("dictionary_value_id", "value")}
                       for val in attr.get("values", [])],
        }
    return out


def value_key(value):
    return value.get("dictionary_value_id", 0), normal(value.get("value", ""))


def signature(attrs):
    by_id = {a["id"]: a.get("values", []) for a in attrs}
    brands, models = by_id.get(BRAND, []), by_id.get(MODELS, [])
    if models and len(brands) == len(models):
        # Сравниваем пары, а не независимые множества: перестановка брендов
        # относительно моделей меняет совместимость многобрендного товара.
        return sorted((BRAND, MODELS, *value_key(b), *value_key(m))
                      for b, m in zip(brands, models))
    return sorted((a["id"], a.get("complex_id", 0), *value_key(v))
                  for a in attrs for v in a.get("values", []))


def printer_keys(attrs):
    # Родитель может содержать новый id того же принтера. Сохраняем модели,
    # а не устаревшие ссылки; новые id подтверждены живым вердиктом родителя.
    return {normal(v.get("value", "")) for a in attrs if a["id"] == MODELS
            for v in a.get("values", [])}


def model_warning(info):
    return any(e.get("code") in MODEL_CODES and e.get("attribute_id") == MODELS
               for e in info.get("errors", []))


def hard_errors(info):
    return any(e.get("level") == "ERROR_LEVEL_ERROR" for e in info.get("errors", []))


def stable(info):
    st = info.get("statuses", {})
    return (not info.get("is_archived") and not info.get("is_autoarchived")
            and st.get("status_name") in SELLING
            and st.get("status_description") != "Обновляется"
            and st.get("moderate_status") == "approved"
            and not hard_errors(info))


def bundle_parent(offer):
    match = re.match(r"^(\d{4})[XХ×]\d+", offer, re.I)
    return match.group(1) if match else None


def base_offer(offer, parent):
    # Дисквер добавляет случайный хвост. Производные по принтерам начинаются
    # с дополнительной цифры: их нельзя использовать как полную базовую карточку.
    return (offer == parent or
            (bool(re.fullmatch(re.escape(parent) + r"[A-Z][A-Z0-9]{7}", offer, re.I))
             and not bundle_parent(offer)))


def cartridge_model(card):
    for aid in (12141, 9048):
        values = {normal(v.get("value", "")) for a in card.get("attributes", [])
                  if a.get("id") == aid for v in a.get("values", [])}
        values.discard("")
        if len(values) == 1:
            return values.pop()
    return None


def checked_values(attrs, dictionaries):
    for attr in attrs:
        for value in attr.get("values", []):
            current = dictionaries.get(attr["id"], {}).get(value.get("dictionary_value_id"))
            if current is None or normal(current) != normal(value.get("value", "")):
                raise UnsafeCompatibility("ссылка отсутствует/изменилась в справочнике; нужен проверенный родитель")


def plan(card, info, parents=(), dictionaries=None):
    """Чистый планировщик: parents — пары (свежие атрибуты, свежий статус)."""
    result = {"offer_id": card["offer_id"], "product_id": card["id"],
              "strategy": "skip", "reason": "", "attributes": []}
    if not model_warning(info):
        result["reason"] = "предупреждения совместимости нет"
        return result
    if not stable(info):
        result["reason"] = "карточка не одобрена, обновляется или не готова к продаже"
        return result
    try:
        current = block(card)
        parent_code = bundle_parent(card["offer_id"])
        donors = []
        for parent, parent_info in parents:
            if not parent_code or not base_offer(parent["offer_id"], parent_code):
                continue
            if not stable(parent_info) or model_warning(parent_info):
                continue
            if ((card.get("description_category_id"), card.get("type_id")) !=
                    (parent.get("description_category_id"), parent.get("type_id"))):
                continue
            target_model = cartridge_model(card)
            if not target_model or cartridge_model(parent) != target_model:
                continue
            donor = block(parent)
            brands = donor.get(BRAND, {}).get("values", [])
            models = donor.get(MODELS, {}).get("values", [])
            if not models or len(brands) != len(models):
                continue
            attrs = [donor[BRAND], donor[MODELS]]
            if not printer_keys(list(current.values())) <= printer_keys(attrs):
                continue  # Не теряем ни одну исходную модель.
            donors.append((parent["offer_id"], attrs))
        if donors:
            if len({tuple(signature(attrs)) for _, attrs in donors}) != 1:
                raise UnsafeCompatibility("здоровые родители содержат разные списки")
            result.update(strategy="healthy_parent", source_offer=donors[0][0],
                          attributes=copy.deepcopy(donors[0][1]),
                          reason="проверенный непустой блок родителя того же картриджа")
            return result
        brands = current.get(BRAND, {}).get("values", [])
        models = current.get(MODELS, {}).get("values", [])
        if not brands:
            raise UnsafeCompatibility("нет бренда и проверенного родителя")
        attrs = list(current.values())
        checked_values(attrs, dictionaries or {})
        if not models:
            if MODELS in current:
                raise UnsafeCompatibility("пустой атрибут моделей требует ручной проверки")
            # Как 2618: повтор бренда может снять старый вердикт, но НЕ
            # заполняет модели. Это явно отражается в плане и отчёте.
            result.update(strategy="repeat_brand_only", attributes=attrs,
                          reason="моделей нет; повтор текущего бренда, список не восстанавливается")
        elif len(brands) != len(models):
            if len({value_key(v) for v in brands}) != 1:
                raise UnsafeCompatibility("несколько брендов и несовпадающее число моделей")
            current[BRAND]["values"] = [copy.deepcopy(brands[0]) for _ in models]
            result.update(strategy="balance_existing", attributes=[current[BRAND], current[MODELS]],
                          reason="один бренд; сохраняем все исходные ссылки моделей")
        else:
            result.update(strategy="repeat_existing", attributes=attrs,
                          reason="блок согласован; повтор текущих ссылок без подбора по похожим именам")
        return result
    except UnsafeCompatibility as exc:
        result["reason"] = str(exc)
        return result


class Client:
    def __init__(self, headers):
        self.headers = headers
        self.cache = {}

    def post(self, path, body):
        for n in range(4):
            response = requests.post("https://api-seller.ozon.ru" + path,
                                     headers=self.headers, json=body, timeout=60)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(3 * (n + 1))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Ozon: повторные ошибки {response.status_code} на {path}")

    def attrs(self, offers):
        return self.post("/v4/product/info/attributes", {
            "filter": {"offer_id": offers, "visibility": "ALL"},
            "limit": 1000, "sort_dir": "ASC"})["result"]

    def info(self, offers):
        return self.post("/v3/product/info/list", {"offer_id": offers})["items"]

    def dictionary(self, aid, card):
        key = (aid, card["description_category_id"], card["type_id"])
        if key not in self.cache:
            out, last, seen = {}, 0, set()
            for _ in range(150):
                data = self.post("/v1/description-category/attribute/values", {
                    "attribute_id": aid, "description_category_id": key[1],
                    "type_id": key[2], "language": "DEFAULT", "limit": 1000,
                    "last_value_id": last})
                values = data.get("result", [])
                out.update({x["id"]: x["value"] for x in values})
                if not data.get("has_next"):
                    break
                last = values[-1]["id"] if values else last
                if last in seen or not values:
                    raise RuntimeError("повтор страницы справочника")
                seen.add(last)
            else:
                raise RuntimeError("превышен предел страниц справочника")
            self.cache[key] = out
        return self.cache[key]


def fresh(info, hours=2):
    raw = info.get("statuses", {}).get("status_updated_at")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if when.tzinfo is None:
            return True  # Неизвестная зона: не рискуем гонкой обновлений.
        return (datetime.now(timezone.utc) - when).total_seconds() < hours * 3600
    except ValueError:
        return True


def verify_result(proposal, attrs, info, task_status):
    """Успех после завершения обработки, а не по HTTP 200/исчезнувшему warning."""
    if task_status != "imported":
        return False
    if not stable(info) or model_warning(info):
        return False
    return signature(list(block(attrs).values())) == signature(proposal["attributes"])


def run_daily(account, limit, classes):
    """Существующий cron: совместимость перед обычным дожимом, общий lock."""
    from tools import ozon_card_push as push

    with open("/opt/mp-analytics/backups/ozon_card_daily.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        from tools.ozon_card_limit_reminder import send_due
        try:
            send_due()
        except Exception as exc:
            # Ошибка Telegram не останавливает карточки; повтор в следующем запуске.
            print(f"Напоминание о лимите не доставлено: {type(exc).__name__}", flush=True)
        print(f"{account}: плановая проверка совместимости, лимит {min(limit, 20)}", flush=True)
        run_models(account, min(limit, 20), True, auto=True)
        # Исключение выше прерывает кабинет до старого дожимателя.
        push.run(account, limit, True, classes)


def run_models(account, limit, apply_, offers=None, output=None, auto=False):
    from core.db import query
    from tools import ozon_card_push as push

    if limit < 1:
        raise ValueError("лимит должен быть положительным")
    if auto and (offers or limit > 20):
        raise ValueError("--models-auto: без --offers, лимит не более 20")
    if apply_ and not auto and (not offers or len(offers) > 20):
        raise ValueError("тестовый --repair-models --apply требует --offers (не более 20)")
    if not offers:
        # Успешные старые попытки не блокируют новые рецидивы. Неудачный
        # repair_models оставляет needs_human=true: повтор только вручную.
        guard = ("AND NOT needs_human AND (last_attempt_at IS NULL OR "
                 "last_attempt_at < now() - interval '20 hours') ") if auto else ""
        rows = query("SELECT offer_id FROM card_status WHERE platform='ozon' AND account=%s "
                     "AND is_open AND err_class='W' AND "
                     "(err_codes LIKE '%%attribute_hierarchy_fail%%' OR "
                     "err_codes LIKE '%%warning_attribute_values_out_of_range%%') " + guard +
                     "ORDER BY first_seen LIMIT %s", (account, min(limit, 100)))
        offers = [x["offer_id"] for x in rows]
    else:
        offers = list(dict.fromkeys(offers))[:limit]
    if not offers:
        print(f"{account}: предупреждений совместимости нет")
        return []
    if len(offers) > 100:
        raise ValueError("тестовый режим: не более 100 карточек в сухом прогоне")
    client = Client(push._headers(account))
    cards = {x["offer_id"]: x for x in client.attrs(offers)}
    infos = {x["offer_id"]: x for x in client.info(offers)}
    proposals = []
    for offer in offers:
        if offer not in cards or offer not in infos:
            proposals.append({"offer_id": offer, "strategy": "skip", "reason": "карточка не найдена"})
            continue
        card, info = cards[offer], infos[offer]
        parents = []
        parent_code = bundle_parent(offer)
        if model_warning(info) and parent_code:
            candidates = query("SELECT offer_id FROM raw_ozon_attributes WHERE account=%s "
                               "AND offer_id LIKE %s ORDER BY offer_id LIMIT 201",
                               (account, parent_code + "%"))
            if len(candidates) > 200:
                proposals.append({"offer_id": offer, "strategy": "skip", "reason": "слишком много кандидатов родителя"})
                continue
            donor_offers = [x["offer_id"] for x in candidates if base_offer(x["offer_id"], parent_code)]
            if donor_offers:
                donor_info = {x["offer_id"]: x for x in client.info(donor_offers)}
                parents = [(x, donor_info[x["offer_id"]]) for x in client.attrs(donor_offers)
                           if x["offer_id"] in donor_info]
        dictionaries = {}
        if model_warning(info):
            dictionaries = {aid: client.dictionary(aid, card) for aid in (BRAND, MODELS)}
        proposal = plan(card, info, parents, dictionaries)
        if apply_ and proposal["strategy"] != "skip" and fresh(info):
            proposal.update(strategy="skip", attributes=[], reason="свежее обновление (<2 ч); повторить позже")
        proposals.append(proposal)
    if output:
        Path(output).write_text(json.dumps(proposals, ensure_ascii=False, indent=2))
    for proposal in proposals:
        print(f"{proposal['offer_id']}: {proposal['strategy']} — {proposal['reason']}")
    if not apply_:
        print("СУХОЙ ПРОГОН: API записи и журнал БД не изменялись")
        return proposals
    if all(p["strategy"] == "skip" for p in proposals):
        print("Отправок нет: все карточки пропущены")
        return proposals
    audit = (Path("/opt/mp-analytics/backups/ozon_compatibility") /
             datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    audit.mkdir(parents=True)
    (audit / "before.json").write_text(json.dumps(
        {"account": account, "attributes": cards, "info": infos, "plans": proposals},
        ensure_ascii=False, indent=2))
    for number, proposal in enumerate(proposals, 1):
        if proposal["strategy"] == "skip":
            continue
        offer = proposal["offer_id"]
        # За время получения справочников мог прийти апдейт ТК.
        current = client.attrs([offer])[0]
        info = client.info([offer])[0]
        if (signature(list(block(current).values())) != signature(list(block(cards[offer]).values()))
                or not stable(info) or fresh(info) or not model_warning(info)):
            print(f"{offer}: пропуск — состояние изменилось после плана")
            continue
        if proposal["strategy"] == "healthy_parent":
            parent = proposal["source_offer"]
            parent_attrs, parent_info = client.attrs([parent])[0], client.info([parent])[0]
            if (not stable(parent_info) or model_warning(parent_info)
                    or cartridge_model(parent_attrs) != cartridge_model(current)
                    or signature(list(block(parent_attrs).values())) != signature(proposal["attributes"])):
                print(f"{offer}: пропуск — родитель изменился после плана")
                continue
        response = client.post("/v1/product/attributes/update", {
            "items": [{"offer_id": offer, "attributes": proposal["attributes"]}]})
        tid = response.get("task_id")
        if not tid:
            raise RuntimeError(f"{offer}: нет task_id")
        (audit / f"{number}_task.json").write_text(json.dumps(response, ensure_ascii=False))
        push._log({"platform": "ozon", "account": account, "offer_id": offer,
                   "product_id": proposal["product_id"], "rung": 1, "attr_id": MODELS,
                   "http_code": 200, "task_id": tid, "task_status": "pending",
                   "healed": None, "note": "class:W repair_models " + proposal["strategy"]})
        push._mark_attempt(account, offer, needs_human=True)
        done = False
        for attempt in range(33):
            task = client.post("/v1/product/import/info", {"task_id": tid})
            item = next((x for x in task.get("result", {}).get("items", [])
                         if x.get("offer_id") == offer), {})
            status = item.get("status", "pending")
            push.execute("UPDATE card_push_log SET task_status=%s WHERE account=%s AND offer_id=%s AND task_id=%s",
                         (status, account, offer, tid))
            if status in ("failed", "skipped"):
                break
            attrs, info = client.attrs([offer])[0], client.info([offer])[0]
            (audit / f"{number}_after.json").write_text(json.dumps(
                {"attributes": attrs, "info": info, "task": task}, ensure_ascii=False))
            if hard_errors(info) or info.get("statuses", {}).get("moderate_status") == "declined":
                break
            if verify_result(proposal, attrs, info, status):
                done = True
                break
            print(f"{offer}: проверка {attempt + 1}, задача {status}")
            time.sleep(15)
        push.execute("UPDATE card_push_log SET healed=%s WHERE account=%s AND offer_id=%s AND task_id=%s",
                     (done, account, offer, tid))
        if done:
            push._mark_warn_cleared(account, offer)
            print(f"{offer}: проверено, предупреждение снято ({proposal['strategy']})")
        else:
            raise RuntimeError(f"{offer}: исправление не подтверждено; остальные карточки не отправлены")
    return proposals
