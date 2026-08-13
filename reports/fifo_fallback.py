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
     отгрузка с себестом ДО даты продажи; если товар до этой даты не отгружался — первая после
     (цена той же закупки, просто в другую сторону по времени).
  2) `nabor_fifo` — набор: состав из `set_cost.components` (кэш `mix_data` TheCartridge),
     себест = Σ FIFO компонентов на ту же дату. Отдаём ТОЛЬКО при полном покрытии состава:
     частичная сумма занижает себест набора, а занижение себеста завышает прибыль.

Покрытие хвоста WB 2026 (181 шт без FIFO по nm): товар 152 шт, набор 16 шт, без источника 13 шт.

Использование:
    from reports import fifo_fallback as FF
    fb = FF.load()
    unit, src = fb.unit(article, day)     # (None, None) — товар МС не нашли / не отгружался
"""
import re
import bisect
import sys
import pathlib

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
    def __init__(self, hist, ec2ms, sets):
        self.hist = hist        # {ms_id: [(дата, себест/шт), …] по возрастанию даты}
        self.ec2ms = ec2ms      # {external_code: [ms_id, …]}
        self.sets = sets        # {external_code: [external_code компонента, …]}

    def _by_ms(self, ms, day):
        """FIFO/шт по ближайшей предшествующей отгрузке товара (или первой после, если раньше
        не отгружался)."""
        h = self.hist.get(ms)
        if not h:
            return None
        i = bisect.bisect_right([x[0] for x in h], day)
        return h[i - 1][1] if i else h[0][1]

    def _tovar(self, kk, day):
        for k in kk:
            for ms in self.ec2ms.get(k, []):
                u = self._by_ms(ms, day)
                if u:
                    return u
        return None

    def _nabor(self, kk, day):
        """Σ FIFO компонентов набора. None, если состав неизвестен или покрыт не полностью."""
        for k in kk:
            comps = self.sets.get(k)
            if not comps:
                continue
            tot, cov = 0.0, 0
            for ec in comps:
                u = self._tovar([str(ec)], day)
                if u:
                    tot += u
                    cov += 1
            if cov and cov == len(comps):
                return tot
        return None

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
        return None, None


def load():
    """Справочники одним заходом (без сети: всё уже в БД после collectors.ms_demand_cogs)."""
    hist = {}
    for r in db.query("""SELECT p.ms_id, c.moment::date d, p.cost / p.qty u
                         FROM ms_demand_pos p JOIN ms_demand_cogs c ON c.demand_id = p.demand_id
                         WHERE p.qty > 0 AND p.cost > 0 ORDER BY p.ms_id, c.moment"""):
        hist.setdefault(r["ms_id"], []).append((r["d"], float(r["u"])))
    ec2ms = {}
    for r in db.query("SELECT external_code, ms_id FROM products WHERE external_code IS NOT NULL"):
        ec2ms.setdefault(r["external_code"], []).append(r["ms_id"])
    sets = {}
    for r in db.query("""SELECT external_code, components FROM set_cost
                         WHERE components IS NOT NULL AND n_components > 0"""):
        c = r["components"]
        if isinstance(c, list) and c:
            sets[r["external_code"]] = [str(x) for x in c]
    return FifoFallback(hist, ec2ms, sets)


if __name__ == "__main__":       # self-check: покрытие справочников
    fb = load()
    print(f"товаров с FIFO-историей: {len(fb.hist)}, external_code→ms: {len(fb.ec2ms)}, "
          f"наборов с составом: {len(fb.sets)}")
