# поток: rev
"""Кэш утверждённых ответов по артикулу (`reports/answer_cache.py`, блок G брифа 08.09.2026).

Тест держит три обещания брифа, которые легко потерять при доработках:
  * ключ вопроса строится ТОЛЬКО для кэшируемых классов, и «претензия»/«прочее» в кэш не попадают;
  * один артикул с разными классами обращения — это разные ключи, а не перезапись одного ответа;
  * подстановка из кэша не отменяет ни одной проверки публикации: устаревшая запись обязана
    получить запрет гейта, а стоп-лист A1.2 обязан сработать и по тексту, пришедшему из кэша.

`./venv/bin/python -m unittest tests.test_answer_cache -v` — в БД и в сеть не ходит.
"""
import unittest

from reports import answer_cache as ac
from reports import publish_gate as gate

FILL = "Каким тонером их заправлять?"
COMPAT = "Подойдёт ли к принтеру MFC-L2715DW?"
CHIP = "В этом картридже есть чип?"


class QuestionKeyTest(unittest.TestCase):
    def test_класс_константа(self):
        self.assertEqual(ac.question_key(FILL, "заправка")[0], "заправка")
        self.assertEqual(ac.question_key("Как установить?", "инструкция")[0], "инструкция")

    def test_совместимость_по_модели(self):
        key, why = ac.question_key(COMPAT, "совместимость")
        self.assertTrue(key.startswith("модель:"), key)
        self.assertIn("l2715dw", key)
        self.assertIn("l2715dw", why)

    def test_совместимость_без_модели_не_ключуется(self):
        key, why = ac.question_key("А к моему принтеру подойдёт?", "совместимость")
        self.assertIsNone(key)
        self.assertIn("без распознанной модели", why)

    def test_характеристика_по_атрибуту(self):
        self.assertEqual(ac.question_key(CHIP, "характеристики")[0], "атрибут:чип")
        self.assertEqual(ac.question_key("Картридж с чипом или без?", "характеристики")[0],
                         "атрибут:чип")
        self.assertEqual(ac.question_key("Сколько страниц хватит?", "ресурс")[0], "атрибут:ресурс")

    def test_чип_важнее_оригинальности(self):
        # «оригинальный ли чип» — вопрос про чип; порядок ATTR_RX значим.
        self.assertEqual(ac.question_key("Чип оригинальный?", "характеристики")[0], "атрибут:чип")

    def test_некэшируемые_классы(self):
        for cls in ("претензия", "прочее", "наличие", "доставка"):
            key, why = ac.question_key(FILL, cls)
            self.assertIsNone(key, cls)
            self.assertIn("не кэшируется", why)

    def test_пустой_текст(self):
        self.assertIsNone(ac.question_key("   ", "заправка")[0])

    def test_тот_же_артикул_другой_класс_разные_ключи(self):
        """Требование брифа: один товар, разные вопросы — разные записи кэша."""
        keys = {ac.question_key(FILL, "заправка")[0],
                ac.question_key(CHIP, "характеристики")[0],
                ac.question_key(COMPAT, "совместимость")[0],
                ac.question_key("Как установить?", "инструкция")[0]}
        self.assertEqual(len(keys), 4, keys)


class SignatureTest(unittest.TestCase):
    def test_нет_карточки_нет_отпечатка(self):
        # Молчание карточки не должно объявлять кэш устаревшим — сравнивать не с чем.
        self.assertIsNone(ac.card_signature(None, "заправка"))
        self.assertIsNone(ac.card_signature({}, "заправка"))

    def test_совместимость_по_списку_моделей(self):
        a = ac.card_signature({"models": ["HL-L2371DN", "DCP-L2551DN"]}, "модель:l2371dn")
        b = ac.card_signature({"models": ["DCP-L2551dn", "hl l2371dn"]}, "модель:l2371dn")
        c = ac.card_signature({"models": ["HL-L2371DN"]}, "модель:l2371dn")
        self.assertEqual(a, b)                      # порядок и регистр не меняют отпечаток
        self.assertNotEqual(a, c)                   # ушедшая из карточки модель — меняет

    def test_атрибут_по_своему_полю(self):
        a = ac.card_signature({"chip": True, "resource": 1500}, "атрибут:чип")
        b = ac.card_signature({"chip": True, "resource": 9000}, "атрибут:чип")
        c = ac.card_signature({"chip": False, "resource": 1500}, "атрибут:чип")
        self.assertEqual(a, b)                      # чужое поле ключа не касается
        self.assertNotEqual(a, c)

    def test_неизвестный_атрибут_отпечатка_не_даёт(self):
        self.assertIsNone(ac.card_signature({"chip": True}, "атрибут:цвет"))


class RememberGuardTest(unittest.TestCase):
    """Отказы, которые обязаны случиться ДО обращения к БД."""

    def test_претензию_не_пишем(self):
        okk, why = ac.remember(article="5422", cls="претензия", key="заправка", text="т",
                               approved_by="send")
        self.assertFalse(okk)
        self.assertIn("не кэшируется", why)

    def test_без_артикула_не_пишем(self):
        self.assertFalse(ac.remember(article=None, cls="заправка", key="заправка", text="т",
                                     approved_by="send")[0])

    def test_пустой_текст_не_пишем(self):
        self.assertFalse(ac.remember(article="5422", cls="заправка", key="заправка", text="  ",
                                     approved_by="send")[0])

    def test_отзыв_не_кэшируется(self):
        okk, why = ac.remember_sent({"kind": "review", "body": "хорошо"}, "спасибо", "send")
        self.assertFalse(okk)
        self.assertIn("только вопросы", why)


def cached_row(text, stale=False, cls="заправка", **over):
    """Строка raw_feedback после подстановки из кэша — как её пишет feedback_today."""
    g = {"llm": False, "grounded": True, "source": "кэш: утверждено 01.09.2026 на wb",
         "template_id": "cache", "request_class": cls,
         "cache": {"id": 1, "article": "5422", "key": cls, "hits": 3,
                   "stale": stale, "confirm": False}}
    r = {"platform": "wb", "account": "wb_acc1", "kind": "question", "ext_id": "T", "item_id": 1,
         "payload": {}, "draft_route": "review", "draft_text": text, "draft_confidence": 0.9,
         "draft_grounding": g, "body": FILL}
    r.update(over)
    return r


class GateOnCacheTest(unittest.TestCase):
    def test_свежая_подстановка_проходит(self):
        allow, why = gate.verdict(cached_row("Заправлять тонером типа TN-2375."))
        self.assertTrue(allow, why)

    def test_устаревшая_подстановка_помечена(self):
        # 10.09.2026: на ВОПРОСАХ причина ушла в трейс (publish_gate.Q_BLOCKING) — считается
        # и видна оператору, но публикацию не держит. У отзыва по-прежнему блок.
        allow, why, trace = gate.verdict_full(cached_row("Заправлять тонером типа TN-2375.", stale=True))
        self.assertTrue(any("кэш устарел" in w for w in trace), trace)
        r = cached_row("Заправлять тонером типа TN-2375.", stale=True)
        r["kind"] = "review"
        self.assertFalse(gate.verdict(r)[0])

    def test_стоп_лист_A12_работает_и_по_кэшу(self):
        # Ключевое требование брифа: кэш не отменяет проверок. Текст утверждён когда-то давно,
        # но обещание от лица компании в машинном черновике запрещено всегда.
        allow, why = gate.verdict(cached_row("Оформим замену или возврат, напишите в чат."))
        self.assertFalse(allow)
        self.assertTrue(any("обещание" in w for w in why), why)

    def test_кэш_не_включает_мягкие_причины(self):
        # llm=false: у кэша нет ни self-reported grounded, ни осмысленной уверенности модели.
        allow, why = gate.verdict(cached_row("Ответ.", draft_confidence=0.0))
        self.assertTrue(allow, why)


class FakeDB:
    """Заглушка core.db: запоминает SQL и параметры, ничего никуда не пишет."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.q = []
        self.e = []

    def query(self, sql, params=None):
        self.q.append((" ".join(sql.split()), params))
        r = self.rows.pop(0) if self.rows else []
        return r

    def execute(self, sql, params=None):
        self.e.append((" ".join(sql.split()), params))


class NormalizeTest(unittest.TestCase):
    """Замечание №6 ревью 08.09.2026: артикул печатает человек, и пробел не должен заводить
    вторую запись того же товара, а /cache_drop — «успешно» удалять ноль строк."""

    def setUp(self):
        self.real, ac.db = ac.db, FakeDB()

    def tearDown(self):
        ac.db = self.real

    def test_remember_обрезает_края(self):
        ac.remember(article=" 5422 ", cls="заправка", key=" заправка ", text="т", approved_by="send")
        self.assertEqual(ac.db.e[0][1][:3], ("5422", "заправка", "заправка"))

    def test_lookup_обрезает_края(self):
        ac.lookup(" 5422 ", "заправка", "заправка")
        self.assertEqual(ac.db.q[0][1], ("5422", "заправка", "заправка"))

    def test_drop_без_оглядки_на_регистр(self):
        ac.drop(" 5422 ", " Заправка ")
        sql, params = ac.db.q[0]
        self.assertIn("lower(article)=lower(%s)", sql)
        self.assertEqual(params, ("5422", "Заправка"))

    def test_drop_без_артикула_ничего_не_трогает(self):
        self.assertEqual(ac.drop("  "), 0)
        self.assertEqual(ac.db.q, [])


class StrictArticleTest(unittest.TestCase):
    """Замечание №3 ревью: срез площадочного хвоста — догадка по длине. Для карточки оператора
    догадка допустима, для ключа кэша — нет: чужой ответ ушёл бы покупателю."""

    def setUp(self):
        self.real = ac.db

    def tearDown(self):
        ac.db = self.real

    def test_неподтверждённый_артикул_в_кэш_не_идёт(self):
        ac.db = FakeDB(rows=[[]])                       # ms_product не знает такого кода
        self.assertIsNone(ac.internal_article("wb", "9999ZZZZZZZZ", 1, strict=True))

    def test_для_карточки_оператора_догадка_остаётся(self):
        ac.db = FakeDB(rows=[[]])
        self.assertEqual(ac.internal_article("wb", "9999ZZZZZZZZ", 1), "9999")

    def test_подтверждённый_артикул_проходит_строгий_режим(self):
        ac.db = FakeDB(rows=[[{"external_code": "5422"}]])
        self.assertEqual(ac.internal_article("wb", "5422YHSC5BIR", 1, strict=True), "5422")


HITROW = {"id": 7, "answer_text": "Заправлять тонером TN-2375.", "stale": False,
          "stale_reason": None, "card_sig": "aaaaaaaaaaaaaaaa", "hit_count": 5,
          "approved_at": None, "source_platform": "wb"}


class TryHitTest(unittest.TestCase):
    def setUp(self):
        self.real = ac.db

    def tearDown(self):
        ac.db = self.real

    def _run(self, hit, platform, facts):
        # rows: ms_product (артикул подтверждён) → lookup
        ac.db = FakeDB(rows=[[{"external_code": "5422"}], [dict(hit)]])
        row = {"kind": "question", "platform": platform, "account": "a", "item_id": 1,
               "article": "5422", "body": FILL, "rating": None}
        return ac.try_hit(row, facts, cls="заправка")

    def test_чужая_площадка_не_делает_кэш_устаревшим(self):
        """Замечание №8: артикул один на все площадки, карточки у них разные (у oz_acc2 атрибутов
        нет вовсе) — межплощадочное сравнение отпечатков давало бы вечный ложный stale."""
        res = self._run(HITROW, "ozon", {"refillable": False, "kind": "тонер"})
        self.assertIsNotNone(res)
        self.assertFalse(res[1]["cache"]["stale"])
        self.assertFalse(any("stale" in str(p) for _, p in ac.db.e))

    def test_своя_площадка_с_другой_карточкой_даёт_stale(self):
        res = self._run(HITROW, "wb", {"refillable": False, "kind": "тонер"})
        self.assertTrue(res[1]["cache"]["stale"])
        self.assertIn("кэш устарел", res[1]["note"])

    def test_отпечаток_усыновляется_если_его_не_было(self):
        """Замечание №2: запись, заведённая при непрочитанной карточке, иначе не включит
        авто-инвалидацию никогда."""
        self._run(dict(HITROW, card_sig=None), "wb", {"refillable": False, "kind": "тонер"})
        sets = [p for sql, p in ac.db.e if "card_sig" in sql]
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0][1], 7)

    def test_совпадающая_карточка_остаётся_свежей(self):
        facts = {"refillable": False, "kind": "тонер"}
        sig = ac.card_signature(facts, "заправка")
        res = self._run(dict(HITROW, card_sig=sig), "wb", facts)
        self.assertFalse(res[1]["cache"]["stale"])
        self.assertEqual(res[0], HITROW["answer_text"])


if __name__ == "__main__":
    unittest.main()
