# поток: fin
"""reports/fifo_fallback.py — себест/шт для продаж БЕЗ своей отгрузки (FBO и хвост FBS)
по FIFO чужих отгрузок ТОГО ЖЕ ТОВАРА, а не по карточке МС.

Зачем. У FBO-продажи сборочного задания нет, значит нет и своей отгрузки в МС — FIFO конкретного
списания взять неоткуда. Раньше такие штуки падали в цепочку `products.cost_seb` (средняя
себестоимость ТЕКУЩЕГО остатка из карточки) — это не себестоимость списания. Решение Сергея
2026-08-13: единственный источник себестоимости — FIFO, поэтому сначала ищем FIFO того же товара
МС по ближайшей ПРЕДШЕСТВУЮЩЕЙ отгрузке (любая площадка — товар один и тот же), и только если
товар не отгружался ни разу, отдаём управление старой цепочке.

Два источника, в порядке применения:
  1) `tovar_fifo` — товар МС (мост: ведущие цифры артикула МП = `external_code`), последняя
     отгрузка с себестом ДО даты продажи. Если до этой даты товар не отгружался — источника нет
     (правило Сергея 2026-08-14: цена не может быть позже отгрузки — товар сперва поступает
     на склад и только потом уходит покупателю).
  2) `nabor_fifo` — набор: состав из `set_cost.components` (кэш `mix_data` TheCartridge),
     себест = Σ FIFO компонентов на ту же дату. Отдаём ТОЛЬКО при полном покрытии состава:
     частичная сумма занижает себест набора, а занижение себеста завышает прибыль.
  3) `analog_fifo` — связи универсальных моделей (`prc_tc_link`, каталог TheCartridge, поток prc):
     один и тот же картридж продаётся под разными кодами (Q2612A = Canon 703). Покупатель заказал
     5335, а со склада ушёл 0002 — в приёмках и отгрузках МС 5335 не найти, 0002 найдётся.
     Поэтому, если своего FIFO нет, берём FIFO связанного кода на ту же дату. Направление ссылки
     не важно (взаимозаменяемость симметрична), связи читаем в обе стороны. Если FIFO нашёлся
     у нескольких связанных кодов — берём МАКСИМАЛЬНЫЙ: где не знаем точно, ошибаемся в сторону
     расхода, а не завышенной прибыли.

Покрытие хвоста WB 2026 (181 шт без FIFO по nm): товар 152 шт, набор 16 шт, без источника 13 шт.
Связи закрывают ещё ~40 % остатка (219 из 556 шт продаж 2026 без FIFO товара/набора).

Использование:
    from reports import fifo_fallback as FF
    fb = FF.load()
    unit, src = fb.unit(article, day)     # (None, None) — товар МС не нашли / не отгружался
"""
import re
import bisect
import sys
import pathlib
import datetime

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402


def keys(sa):
    """Ключи товара МС по артикулу площадки: полный, 5 ведущих цифр, 4 ведущих цифры.

    Группа МС = ведущие цифры (правило клиента): Цифровой «07772» = 0777 + 2,
    Дисквэр «3212wqfn7m9y» = 3212 + случайный хвост."""
    out = [sa] if sa else []
    m = re.match(r"^(\d{4,6})", sa or "")
    if m:
        d = m.group(1)
        if len(d) >= 5:
            out.append(d[:5])
        out.append(d[:4])
    return out


class FifoFallback:
    def __init__(self, hist, ec2ms, sets, links=None, ms2ec=None):
        self.hist = hist        # {ms_id: [(дата, себест/шт), …] по возрастанию даты}
        self.ec2ms = ec2ms      # {external_code: [ms_id, …]}
        self.sets = sets        # {external_code: [external_code компонента, …]}
        self.links = links or {}    # {external_code: [связанный код, …]} — универсальные модели
        self.ms2ec = ms2ec or {}    # {ms_id: external_code} — вход в связи от позиции документа

    def _by_ms(self, ms, day):
        """FIFO/шт по ближайшей ПРЕДШЕСТВУЮЩЕЙ отгрузке товара. Если до этой даты товар не
        отгружался — None: цену из будущего не берём (правило Сергея 2026-08-14, товар сперва
        поступает на склад и только потом отгружается)."""
        h = self.hist.get(ms)
        if not h:
            return None
        if isinstance(day, str):                     # коллекторы держат дату строкой YYYY-MM-DD
            day = datetime.date.fromisoformat(day[:10])
        i = bisect.bisect_right([x[0] for x in h], day)
        return h[i - 1][1] if i else None

    def _tovar(self, kk, day):
        for k in kk:
            for ms in self.ec2ms.get(k, []):
                u = self._by_ms(ms, day)
                if u:
                    return u
        return None

    def _analog(self, kk, day):
        """FIFO связанной универсальной модели (тот же картридж под другим кодом). Из всех
        связей с историей берём МАКСИМАЛЬНУЮ себестоимость — см. шапку модуля."""
        best = None
        for k in kk:
            for rc in self.links.get(k, ()):
                u = self._tovar([rc], day)
                if u and (best is None or u > best):
                    best = u
        return best

    def _tovar_or_analog(self, kk, day):
        return self._tovar(kk, day) or self._analog(kk, day)

    def _nabor(self, kk, day):
        """Σ FIFO компонентов набора. None, если состав неизвестен или покрыт не полностью.
        Компонент без своего FIFO ищется по связям универсальных моделей — иначе набор целиком
        свалился бы в карточку МС из-за одной ненайденной позиции."""
        for k in kk:
            comps = self.sets.get(k)
            if not comps:
                continue
            tot, cov = 0.0, 0
            for ec in comps:
                u = self._tovar_or_analog([str(ec)], day)
                if u:
                    tot += u
                    cov += 1
            if cov and cov == len(comps):
                return tot
        return None

    def unit_ms(self, ms_id, day):
        """FIFO/шт конкретного товара МС на дату (для документов, где ms_id уже известен)."""
        return self._by_ms(ms_id, day)

    def impute(self, pos, day):
        """(себест документа, метод) по позициям [{ms_id, qty}] — FIFO ТЕХ ЖЕ товаров МС на дату
        документа, а где своего FIFO нет — FIFO связанной универсальной модели (тогда метод
        'analog_fifo'). (None, None), если источник нашёлся не по всем позициям: частичная сумма
        занизила бы себест документа, а занижение себеста завышает прибыль."""
        tot, cov, n, used_analog = 0.0, 0, 0, False
        for x in pos or []:
            n += 1
            u = self._by_ms(x["ms_id"], day)
            if not u:
                ec = self.ms2ec.get(x["ms_id"])
                u = self._analog([ec], day) if ec else None
                used_analog = used_analog or bool(u)
            if u:
                tot += u * float(x["qty"] or 0)
                cov += 1
        if n and cov == n and tot > 0:
            return tot, ("analog_fifo" if used_analog else "tovar_fifo")
        return None, None

    def unit(self, sa, day):
        """(себест/шт, источник) или (None, None). day — дата продажи (date)."""
        kk = keys(sa)
        if not kk:
            return None, None
        u = self._tovar(kk, day)
        if u:
            return u, "tovar_fifo"
        u = self._nabor(kk, day)
        if u:
            return u, "nabor_fifo"
        u = self._analog(kk, day)
        if u:
            return u, "analog_fifo"
        return None, None


def load():
    """Справочники одним заходом (без сети: всё уже в БД после collectors.ms_demand_cogs)."""
    hist = {}
    for r in db.query("""SELECT p.ms_id, c.moment::date d, p.cost / p.qty u
                         FROM ms_demand_pos p JOIN ms_demand_cogs c ON c.demand_id = p.demand_id
                         WHERE p.qty > 0 AND p.cost > 0 ORDER BY p.ms_id, c.moment"""):
        hist.setdefault(r["ms_id"], []).append((r["d"], float(r["u"])))
    ec2ms, ms2ec = {}, {}
    for r in db.query("SELECT external_code, ms_id FROM products WHERE external_code IS NOT NULL"):
        ec2ms.setdefault(r["external_code"], []).append(r["ms_id"])
        ms2ec[r["ms_id"]] = r["external_code"]
    sets = {}
    for r in db.query("""SELECT external_code, components FROM set_cost
                         WHERE components IS NOT NULL AND n_components > 0"""):
        c = r["components"]
        if isinstance(c, list) and c:
            sets[r["external_code"]] = [str(x) for x in c]
    links = {}                   # связи универсальных моделей, обе стороны (поток prc, миграция 408)
    if db.query("SELECT to_regclass('public.prc_tc_link') t")[0]["t"]:   # без 408 работаем без связей
        for r in db.query("SELECT external_code, ref_code FROM prc_tc_link"):
            links.setdefault(r["external_code"], []).append(r["ref_code"])
            links.setdefault(r["ref_code"], []).append(r["external_code"])
    return FifoFallback(hist, ec2ms, sets, links, ms2ec)


if __name__ == "__main__":       # self-check: покрытие справочников
    fb = load()
    print(f"товаров с FIFO-историей: {len(fb.hist)}, external_code→ms: {len(fb.ec2ms)}, "
          f"наборов с составом: {len(fb.sets)}, кодов со связями: {len(fb.links)}")
