# поток: prc
"""Product identity, limits, source parity and no-write validation for WB names."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from prices import ms_import
from prices.wb_label import MAX_CHARS, Parts, shorten
from tools.prc.wb_fill import compose, compose_details


def model(**over):
    return {**dict(consumable_type='Картридж', title='071H', additional_title='5646C002',
                   printer_models=[{'brand': 'Canon'}], ink_colors=['черный'], chip='С чипом',
                   capacity='Увеличенная'), **over}


class Formula(unittest.TestCase):
    def test_limit_inclusive_and_no_unapproved_shortening(self):
        self.assertEqual(len(shorten(Parts(('A' * 55,))).name), 55)
        self.assertEqual(shorten(Parts(('A' * 56,))).issue, 'length')

    def test_real_clipped_sample_keeps_model_chip_capacity(self):
        result = compose_details(model())
        self.assertEqual(len(result.full_name), 57)
        self.assertEqual(result.name, 'Картридж 071H для Canon черный с чипом XL ресурс')
        self.assertLessEqual(len(result.name), 50)

    def test_supplier_xl_is_shortened_together_with_other_parts(self):
        raw = model(capacity='Обычная')
        result = compose_details(raw, 'Canon 071H увеличенный ресурс')
        self.assertEqual(result.name, compose(model())[0])
        self.assertIn('XL', result.name)
        self.assertLessEqual(len(result.name), MAX_CHARS)

    def test_short_name_unchanged_and_raw_not_mutated(self):
        raw = model(title='12A', additional_title='', chip='', capacity='Обычная')
        before = copy.deepcopy(raw)
        self.assertEqual(compose(raw)[0], 'Картридж 12A для Canon черный')
        self.assertEqual(raw, before)

    def test_shortening_order_and_rare_color_are_preserved(self):
        p = Parts(('Картридж', 'MODEL-123456', 'для', 'Canon'), extra='ALT-123456',
                  colors=('светло-пурпурный',), chip='без чипа', xl='XL ресурс')
        r = shorten(p)
        self.assertIn('светло-пурпурный', r.suggestion)
        self.assertIn('без чипа', r.suggestion)
        self.assertIn('MODEL-123456', r.suggestion)
        self.assertEqual(r.actions[:2], ('убрать дополнительное название', 'XL ресурс → XL'))

    def test_unfit_name_is_held_not_truncated(self):
        raw = model(title='LONG-MODEL-' * 8)
        r = compose_details(raw)
        self.assertEqual(r.name, '')
        self.assertEqual(r.issue, 'length')
        self.assertIn(raw['title'], r.suggestion)
        self.assertIn('с чипом', r.suggestion)
        self.assertIn('XL', r.suggestion)

    def test_toner_content_not_gross_weight(self):
        raw = model(consumable_type='Тонер', title='TK-1170', additional_title='',
                    printer_models=[{'brand': 'Kyocera'}], volume_ml='300.0', weight='350',
                    capacity='Обычная', chip='')
        self.assertTrue(compose(raw)[0].endswith('300 г'))
        self.assertNotIn('350', compose(raw)[0])

    def test_unknown_chip_or_color_blocks(self):
        for fields, issue in [({'chip': 'неизвестный'}, 'chip'),
                              ({'ink_colors': ['неизвестный']}, 'color')]:
            with self.subTest(issue=issue):
                r = compose_details(model(**fields))
                self.assertFalse(r.name)
                self.assertEqual(r.issue, issue)

    def test_set_colors_not_printed(self):
        raw = model(consumable_type='Комплект картриджей', title='ABC X4',
                    additional_title='', capacity='Обычная', chip='')
        name, why = compose(raw)
        self.assertFalse(why)
        self.assertIn('X4', name)
        self.assertNotIn('черный', name)

    def test_full_color_and_chip_qualifiers_are_never_lost(self):
        raw = model(ink_colors=['черный матовый'], chip='С чипом без счетчика',
                    title='ABCDEF1234')
        r = compose_details(raw)
        self.assertIn('черный матовый', r.suggestion)
        self.assertIn('с чипом без счётчика', r.suggestion)


class SharedPaths(unittest.TestCase):
    def test_supplier_fallback_preserves_explicit_content_volume(self):
        with patch.object(ms_import, 'query', return_value=[]), \
             patch.object(ms_import, 'tc_kinds', return_value=['Тонер', 'Чернила']):
            name, note = ms_import.wb_compose('', 'Тонер Kyocera TK-1170, 300 г, черный')
        self.assertTrue(name.endswith('300 г'), name)
        self.assertIn('поставщика', note)

    def test_pult_and_mass_formula_identical(self):
        raw = model(capacity='Обычная')
        with patch.object(ms_import, 'query', return_value=[{'raw': raw}]):
            actual, why = ms_import.wb_compose('1234', 'Canon 071H XL')
        self.assertEqual(actual, compose(raw, 'Canon 071H XL')[0])
        self.assertIsNone(why)

    def test_length_failure_cannot_switch_to_supplier_fallback(self):
        with patch.object(ms_import, 'query', return_value=[{'raw': model(title='LONG-' * 30)}]), \
             patch.object(ms_import, 'wb_from_price') as fallback:
            actual, why = ms_import.wb_compose('1234', 'Картридж Canon 12A')
        self.assertFalse(actual)
        self.assertIn('предел 55', why)
        fallback.assert_not_called()

    def test_unrecognised_color_cannot_switch_sources(self):
        with patch.object(ms_import, 'query', return_value=[{'raw': model(ink_colors=['???'])}]), \
             patch.object(ms_import, 'wb_from_price') as fallback:
            self.assertFalse(ms_import.wb_compose('1234', 'Картридж Canon 12A')[0])
        fallback.assert_not_called()

    def test_new_code_keeps_original_model_source(self):
        record = {'Код': '9999ep', 'Внешний код': '9999', '_wb_source_code': '1234',
                  'Наименование': 'Canon 071H XL', 'Доп. поле: Название WB': compose(model())[0]}
        with patch.object(ms_import, 'query', return_value=[{'raw': model()}]) as query:
            self.assertEqual(ms_import.wb_record_errors([record]), [])
            self.assertEqual(query.call_args.args[1], ('1234',))

    def test_every_record_checked_before_first_api_access(self):
        good = {'Код': '1234ep', 'Артикул': 'A', 'Внешний код': '1234',
                'Наименование': 'Canon 071H', 'Доп. поле: Название WB': compose(model())[0]}
        bad = dict(good, Код='1235ep', **{'Доп. поле: Название WB': 'старое ручное название'})
        with patch.object(ms_import, 'query', return_value=[{'raw': model()}]), \
             patch.object(ms_import, '_folders') as folders:
            with self.assertRaisesRegex(ValueError, 'общей формуле'):
                ms_import.create_in_ms([good, bad], 'supplier', dry=False)
        folders.assert_not_called()

    def test_stale_dialog_is_rejected_before_folder_or_identity_reads(self):
        # Exercise the real route body without importing unrelated application startup code.
        tree = ast.parse(Path('web/app.py').read_text())
        route = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == 'novelties_ms_create')
        route.decorator_list = []
        route.args.args[0].annotation = None
        namespace = {'MS_CARDS_MAX': 20, 'db': SimpleNamespace(query=lambda *a: [
            {'id': 1, 'supplier_key': 'sakura', 'article': 'A', 'name': 'Canon 071H',
             'ms_code': '1234ep'}])}
        exec(compile(ast.Module(body=[route], type_ignores=[]), 'web/app.py', 'exec'), namespace)
        payload = SimpleNamespace(id=1, cards=[SimpleNamespace(code='9999ep', wb='старое')],
                                  main_index=0, wb_source_code='')
        with patch.object(ms_import, 'query', return_value=[{'raw': model()}]):
            result = namespace['novelties_ms_create'](payload)
        self.assertFalse(result['ok'])
        self.assertIn(compose(model())[0], result['error'])


if __name__ == '__main__':
    unittest.main()
