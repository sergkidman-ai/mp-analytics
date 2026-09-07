# поток: rev
"""Гейт A0: отзыв с маркером проблемы не уходит покупателю сам (`reports/feedback_draft_run.py`).

Причина правки — замер 07.09.2026 (`docs/reports/rev_auto_gap_examples_2026-09-07.md`): 11 отзывов
4–5★, в тексте которых покупатель писал о проблеме, закрыты авто-маршрутом, 9 опубликовано.
Классика: «краска хорошая яркая, жаль только что не подошла к принтеру» → «рады, что всё подошло».

Обратная сторона теста не менее важна обычного негатива: обычный позитив ДОЛЖЕН остаться на auto,
иначе правка просто перекладывает 4400 отзывов в год на оператора.

`./venv/bin/python -m unittest tests.test_review_gate -v` — в БД и в сеть не ходит.
"""
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from reports import feedback_draft_run as dr  # noqa: E402
from reports import request_class as rc       # noqa: E402


def row(rating=5, body="", pros="", cons="", platform="wb", ext_id="abc123"):
    return {"rating": rating, "body": body, "pros": pros, "cons": cons,
            "platform": platform, "ext_id": ext_id, "item_id": 1, "payload": {}}


# тексты из реальных 11 кейсов (сокращены) + перечень маркеров из брифа A0
FLAGGED = [
    "краска хорошая яркая жаль только что не подошла к принтеру, а так рекомендую",
    "Обидно, что пришёл другой товар — цвет не совпадает с заказанным",
    "Всё быстро приехало, но картридж не подходит к моей модели",
    "Принтер не видит картридж, выдаёт ошибку",
    "Печатает с полосами, мажет по краям",
    "Пришёл не тот картридж",
    "Бледная печать после установки",
    "Товар с браком, корпус треснут",
    "Подскажите, а этот подойдёт к HP 1102?",
    "Принтер не определяет уровень тонера",
    "Не работает вообще",
]
CLEAN = [
    "Всё отлично, спасибо!", "Печатает хорошо, беру не первый раз", "Быстрая доставка, качество супер",
    "Отличный картридж за свои деньги", "Всё супер", "Рекомендую продавца", "Пришло быстро, упаковано хорошо",
    "Хороший товар", "Спасибо, всё понравилось", "Качество на высоте", "Установила сама, всё просто",
    "Печать чёткая, доволен", "Цена-качество отличное", "Заказываю уже третий раз",
    "Работает как оригинал", "Доставка быстрая", "Всё пришло целым", "Классный картридж",
    "Продавцу спасибо", "Ресурса хватает надолго",
]


class TestFlag(unittest.TestCase):
    def test_flagged(self):
        for t in FLAGGED:
            self.assertTrue(dr.review_flagged(row(body=t)), t)

    def test_clean_not_flagged(self):
        for t in CLEAN:
            self.assertFalse(dr.review_flagged(row(body=t)), t)

    def test_empty_not_flagged(self):
        self.assertFalse(dr.review_flagged(row()))

    def test_marker_in_cons(self):
        """Маркер может стоять в «минусах», а не в теле — WB кладёт их отдельным полем."""
        self.assertTrue(dr.review_flagged(row(body="Хороший", cons="не подошёл к принтеру")))


class TestRoute(unittest.TestCase):
    def setUp(self):
        # card_rating.verdict() ходит в БД за рейтингом карточки — тест обязан оставаться офлайн,
        # поэтому карточка всегда «живая»: ветка мёртвой карточки проверяется отдельно.
        self._v = dr.card_rating.verdict
        dr.card_rating.verdict = lambda *a, **k: {"alive": True, "rating": 4.9, "cnt": 10}

    def tearDown(self):
        dr.card_rating.verdict = self._v

    def test_flagged_five_star_to_operator(self):
        """Ни один отзыв с маркером проблемы не уходит сам — ни ротацией, ни хендоффом."""
        for t in FLAGGED:
            cat, draft, route, conf, tpl = dr.draft_review(row(body=t), "Иван", "картридж")
            self.assertEqual(route, "review", t)

    def test_dead_card_keeps_auto(self):
        """Правило Сергея 24.08.2026: карточку под убой ответом не спасают — DEAD_TEXT уходит сам."""
        dr.card_rating.verdict = lambda *a, **k: {"alive": False, "rating": 2.1, "cnt": 40}
        cat, draft, route, conf, tpl = dr.draft_review(row(rating=1, body="не работает"), None, "к")
        self.assertEqual((route, tpl), ("auto", "dead_card"))

    def test_clean_five_star_stays_auto(self):
        for i, t in enumerate(CLEAN):
            cat, draft, route, conf, tpl = dr.draft_review(row(body=t, ext_id=f"e{i}"), "Иван", "картридж")
            self.assertEqual(route, "auto", t)
            self.assertEqual(conf, 0.8)

    def test_empty_five_star_stays_auto(self):
        cat, draft, route, conf, tpl = dr.draft_review(row(), "Иван", "картридж")
        self.assertEqual((cat, route, conf), ("empty5", "auto", 0.95))

    def test_neutral4_empty_auto_with_text_review(self):
        self.assertEqual(dr.draft_review(row(rating=4), None, "картридж")[2], "auto")
        self.assertEqual(dr.draft_review(row(rating=4, body="норм, но дороговато"), None, "к")[2], "review")

    def test_template_id_is_reproducible(self):
        """id шаблона обязан совпадать с текстом, который реально ушёл, — иначе мерить нечего."""
        r = row(body="Всё отлично", ext_id="zzz9")
        cat, draft, route, conf, tpl = dr.draft_review(r, "Иван", "картридж")
        self.assertEqual(draft, dr.POS_WB[int(tpl.split(":")[1])].format(name="Иван", product="картридж"))


class TestRequestClass(unittest.TestCase):
    def test_claim_first(self):
        for t in ("не работает, верните деньги", "пришёл другой товар", "недостача в комплекте",
                  "картридж не подошёл к принтеру", "течёт тонер"):
            self.assertEqual(rc.classify(t, kind="review", rating=5), "претензия", t)

    def test_low_rating_with_text_is_claim(self):
        self.assertEqual(rc.classify("так себе", kind="review", rating=2), "претензия")

    def test_plain_classes(self):
        self.assertEqual(rc.classify("подойдёт ли к HP 1102?", kind="question"), "совместимость")
        self.assertEqual(rc.classify("на сколько страниц хватает?", kind="question"), "ресурс")
        self.assertEqual(rc.classify("", kind="review", rating=5), "оценка без текста")


if __name__ == "__main__":
    unittest.main()
