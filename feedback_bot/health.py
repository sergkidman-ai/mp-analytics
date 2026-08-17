# поток: rev
"""feedback_bot/health.py — контроль работоспособности движка ответов и алерты в Telegram.

Зачем. 13.08.2026 кончился баланс Anthropic, и ветка ВОПРОСОВ встала на четыре дня: каждый вопрос
уходил в `ПРОПУСК без черновика`, а цикл при этом рапортовал `OK` и молчал. Отзывы шли на DeepSeek
как ни в чём не бывало, поэтому со стороны движок выглядел живым. Простой заметили только когда
Сергей вручную посмотрел неотвеченные вопросы на Ozon.

Правило Сергея 17.08.2026: **обязательно проверять и сообщать, что сообщения не уходят и по какой
причине; баланс DeepSeek и Claude — самое основное.**

Два независимых контура:

1. ПРЕДПОЛЁТ (`preflight`) — до генерации. Для каждого провайдера, который реально используется
   в этом прогоне (MODEL для отзывов, QUESTION_MODEL для вопросов, WEB_MODEL для веб-проверок),
   проверяем доступность. У DeepSeek есть честный эндпоинт баланса (`/user/balance`) — читаем
   остаток и ругаемся заранее, когда он ниже порога. У Anthropic эндпоинта баланса НЕТ, поэтому
   пробуем минимальный вызов (max_tokens=1, ~8 входных токенов ≈ $0.00005 за прогон): при пустом
   балансе он падает 400 `Your credit balance is too low` ДО тарификации, то есть бесплатно.

2. ПОСТФАКТУМ (`report_cycle`) — после цикла. Считаем, сколько ответов не создано и не отправлено,
   и по какой причине; молчаливый провал невозможен.

Дедуп. Одна и та же беда повторяется каждые 2 часа, поэтому одинаковый алерт не шлём чаще
FEEDBACK_ALERT_REPEAT_HOURS (12 ч). Когда проблема уходит — обязательно шлём «восстановлено»,
иначе «тихо» неотличимо от «сломано».

Настройки (.env):
    FEEDBACK_DS_MIN_USD=2          # порог предупреждения по остатку DeepSeek, $
    FEEDBACK_ALERT_REPEAT_HOURS=12 # как часто повторять один и тот же алерт
    FEEDBACK_HEALTH_PROBE=1        # 0 = не делать пробный вызов Anthropic (тогда только постфактум)
"""
import os
import sys
import json
import time
import pathlib
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))          # чтобы модуль работал и при прямом запуске
from dotenv import load_dotenv             # noqa: E402
load_dotenv(BASE_DIR / ".env")             # пороги ниже читаются из .env

STATE = BASE_DIR / "reports" / "data" / "feedback_health_state.json"

DS_MIN_USD = float(os.environ.get("FEEDBACK_DS_MIN_USD", "2"))
REPEAT_H = float(os.environ.get("FEEDBACK_ALERT_REPEAT_HOURS", "12"))
PROBE = os.environ.get("FEEDBACK_HEALTH_PROBE", "1") != "0"


def _log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] feedback_health: {msg}", flush=True)


# ─────────────────────────────── состояние (дедуп алертов) ───────────────────────────────

def _state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        _log(f"не смог сохранить состояние: {e}")


# ─────────────────────────────── отправка в Telegram ───────────────────────────────

def notify(text, key=None, repeat_hours=None):
    """Отправить алерт всем адресатам модерации. key — идентификатор проблемы для дедупа.

    Возвращает True, если сообщение реально ушло. Сам факт отправки пишем в состояние, чтобы
    одна и та же беда не долбила каждые 2 часа.
    """
    st = _state()
    now = time.time()
    if key:
        last = (st.get("alerts") or {}).get(key, 0)
        window = (repeat_hours if repeat_hours is not None else REPEAT_H) * 3600
        if now - last < window:
            _log(f"алерт «{key}» уже отправлен {int((now - last) / 60)} мин назад — молчу")
            return False
    try:
        from feedback_bot import tg_moderation as tg
    except Exception as e:                                   # noqa: BLE001
        _log(f"НЕ ОТПРАВЛЕН алерт (нет tg_moderation): {e}\n{text}")
        return False
    if not tg.TOKEN or not tg.NOTIFY_IDS:
        _log(f"НЕ ОТПРАВЛЕН алерт (нет токена/адресатов):\n{text}")
        return False
    ok = False
    for cid in tg.NOTIFY_IDS:
        ok = bool(tg.send(cid, text)) or ok
    if ok and key:
        st.setdefault("alerts", {})[key] = now
        _save(st)
    _log(("отправлен алерт: " if ok else "НЕ УШЁЛ алерт: ") + text.split("\n")[0])
    return ok


def _resolve(key, text):
    """Проблема ушла — снять отметку и сообщить об этом (один раз)."""
    st = _state()
    if (st.get("alerts") or {}).pop(key, None) is not None:
        _save(st)
        notify(text)


# ─────────────────────────────── балансы провайдеров ───────────────────────────────

def deepseek_balance():
    """(is_available, остаток USD | None, текст ошибки | None). Эндпоинт бесплатный."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return False, None, "DEEPSEEK_API_KEY не задан в .env"
    try:
        req = urllib.request.Request("https://api.deepseek.com/user/balance",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        infos = d.get("balance_infos") or []
        usd = next((float(b["total_balance"]) for b in infos if b.get("currency") == "USD"), None)
        return bool(d.get("is_available")), usd, None
    except Exception as e:                                   # noqa: BLE001
        return False, None, f"{type(e).__name__}: {str(e)[:150]}"


def anthropic_probe():
    """(ok, причина). Баланса у Anthropic в API нет — бьём минимальным вызовом.

    При пустом балансе ответ 400 приходит ДО тарификации, то есть проверка бесплатна; при живом
    балансе стоит ~$0.00005 (8 входных + 1 выходной токен).
    """
    if not PROBE:
        return True, "проба отключена (FEEDBACK_HEALTH_PROBE=0)"
    try:
        from reports.llm_client import client_for
        c = client_for("claude-opus-5")
        c.messages.create(model=os.environ.get("FEEDBACK_QUESTION_MODEL", "claude-opus-5"),
                          max_tokens=1, messages=[{"role": "user", "content": "hi"}])
        return True, None
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def _why(err):
    """Человеческая причина вместо трейса — чтобы в Telegram было понятно без разбора логов."""
    e = (err or "").lower()
    if "credit balance is too low" in e or "insufficient balance" in e or "402" in e:
        return "КОНЧИЛСЯ БАЛАНС"
    if "authentication" in e or "401" in e or "invalid api key" in e:
        return "ключ не принят (401)"
    if "rate limit" in e or "429" in e:
        return "лимит запросов (429)"
    if "timeout" in e or "timed out" in e or "connection" in e:
        return "нет связи с API (таймаут/relay)"
    if "not_found" in e or "404" in e:
        return "модель недоступна (404)"
    return (err or "неизвестная причина")[:120]


# ─────────────────────────────── предполёт ───────────────────────────────

def preflight():
    """Проверить провайдеров ДО генерации. Возвращает dict со сводкой; алерты шлёт сам.

    Цикл не роняем: если умер один провайдер, второй должен доработать свою часть очереди.
    """
    from reports.feedback_llm import MODEL
    from reports.feedback_today import QUESTION_MODEL
    from reports.feedback_web import WEB_MODEL
    from reports.llm_client import is_deepseek

    used = {"отзывы": MODEL, "вопросы": QUESTION_MODEL, "веб-проверки": WEB_MODEL}
    need_ds = any(is_deepseek(m) for m in used.values())
    need_an = any(not is_deepseek(m) for m in used.values())
    res = {"models": used, "deepseek": None, "anthropic": None, "problems": []}

    def roles(pred):
        return ", ".join(r for r, m in used.items() if pred(m))

    if need_ds:
        ok, usd, err = deepseek_balance()
        res["deepseek"] = {"ok": ok, "usd": usd, "err": err}
        who = roles(is_deepseek)
        if not ok:
            res["problems"].append(f"DeepSeek недоступен ({_why(err)}) — не уйдут: {who}")
            notify(f"🔴 <b>Ответы не уходят</b>\nПровайдер: DeepSeek\nПричина: <b>{_why(err)}</b>\n"
                   f"Встало: {who}\nОстаток: {'—' if usd is None else f'${usd:.2f}'}\n\n"
                   f"Пополнить: platform.deepseek.com → Top up", key="deepseek_down")
        else:
            _resolve("deepseek_down", f"🟢 DeepSeek снова отвечает (остаток ${usd:.2f}) — {who} пошли")
            if usd is not None and usd < DS_MIN_USD:
                res["problems"].append(f"DeepSeek на исходе: ${usd:.2f}")
                notify(f"🟡 <b>Баланс DeepSeek на исходе</b>\nОстаток: <b>${usd:.2f}</b> "
                       f"(порог ${DS_MIN_USD:.2f})\nНа нём: {who}\n\nПополнить заранее, иначе ответы встанут.",
                       key="deepseek_low", repeat_hours=24)
            else:
                _resolve("deepseek_low", f"🟢 Баланс DeepSeek пополнен: ${usd:.2f}")

    if need_an:
        ok, err = anthropic_probe()
        res["anthropic"] = {"ok": ok, "err": err}
        who = roles(lambda m: not is_deepseek(m))
        if not ok:
            res["problems"].append(f"Anthropic недоступен ({_why(err)}) — не уйдут: {who}")
            notify(f"🔴 <b>Ответы не уходят</b>\nПровайдер: Anthropic (Claude)\n"
                   f"Причина: <b>{_why(err)}</b>\nВстало: {who}\n\n"
                   f"Пополнить: console.anthropic.com → Plans &amp; Billing", key="anthropic_down")
        else:
            _resolve("anthropic_down", f"🟢 Anthropic снова отвечает — {who} пошли")

    _log("предполёт: " + ("; ".join(res["problems"]) if res["problems"] else "провайдеры в порядке"))
    return res


# ─────────────────────────────── постфактум ───────────────────────────────

def report_cycle(fails=None, drafts=0, autosend=None, pending=None):
    """Сообщить, если по итогам цикла что-то НЕ ушло, с разбивкой по причинам.

    fails    — список dict из feedback_today.LAST_RUN: platform/kind/ext_id/err;
    drafts   — сколько черновиков всё же создано;
    autosend — dict из collectors.feedback_autosend.run() ({'sent','fail',…});
    pending  — сколько неотвеченных висит на площадках (для контекста в сообщении).
    """
    fails = fails or []
    lines, key_parts = [], []

    if fails:
        by = {}
        for f in fails:
            by.setdefault(_why(f.get("err")), []).append(f)
        for why, items in sorted(by.items(), key=lambda x: -len(x[1])):
            chans = {}
            for it in items:
                chans[f"{it.get('platform')}/{it.get('kind')}"] = chans.get(f"{it.get('platform')}/{it.get('kind')}", 0) + 1
            detail = ", ".join(f"{k} × {v}" for k, v in sorted(chans.items(), key=lambda x: -x[1]))
            lines.append(f"• <b>{len(items)}</b> без ответа — {why}\n   {detail}")
            key_parts.append(why)

    if autosend and autosend.get("fail"):
        lines.append(f"• <b>{autosend['fail']}</b> не отправлено на площадки (ошибка публикации)")
        key_parts.append("autosend")

    if not lines:
        _resolve("cycle_fail", "🟢 Движок ответов снова работает штатно — всё уходит")
        return False

    tail = f"\n\nСоздано черновиков: {drafts}"
    if pending is not None:
        tail += f" · висит неотвеченных: {pending}"
    notify("🔴 <b>Сообщения покупателям не ушли</b>\n\n" + "\n".join(lines) + tail,
           key="cycle_fail|" + "|".join(sorted(set(key_parts))))
    return True


def cycle_failed():
    """Цикл не доработал (падение/OOM) — сообщить с причиной. Зовётся из systemd OnFailure=.

    Отдельный контур, потому что report_cycle() живёт ВНУТРИ цикла: если процесс убит OOM-killer'ом
    или упал на середине, отчитаться изнутри уже некому. 17.08.2026 так и вышло — цикл 13:03 убили
    на генерации черновиков, и без этого юнита провал снова остался бы незамеченным.
    """
    import subprocess
    why, detail = "цикл упал", ""
    try:
        out = subprocess.run(["journalctl", "-u", "feedback-cycle.service", "-n", "40", "--no-pager"],
                             capture_output=True, text=True, timeout=60).stdout
        if "OOM killer" in out or "oom-kill" in out:
            why = "НЕ ХВАТИЛО ПАМЯТИ (OOM killer)"
            try:
                mem = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=20).stdout
                detail = "\n".join(mem.strip().split("\n")[:2])
            except Exception:                                # noqa: BLE001
                pass
        else:
            err = [ln for ln in out.strip().split("\n") if "FAIL" in ln or "Error" in ln or "Traceback" in ln]
            detail = "\n".join(err[-3:])[:400]
    except Exception as e:                                   # noqa: BLE001
        detail = f"(журнал не прочитан: {type(e).__name__})"
    notify(f"🔴 <b>Цикл ответов не доработал</b>\nПричина: <b>{why}</b>\n"
           f"{('<pre>' + detail + '</pre>') if detail else ''}\n"
           f"Ответы этого цикла не созданы и не отправлены — уйдут следующим, если причина устранена.",
           key="cycle_crash", repeat_hours=3)
    return why


if __name__ == "__main__":
    if "--cycle-failed" in sys.argv:                          # режим для systemd OnFailure=
        print(cycle_failed())
    else:
        r = preflight()
        print(json.dumps({k: v for k, v in r.items() if k != "models"}, ensure_ascii=False, indent=1))
