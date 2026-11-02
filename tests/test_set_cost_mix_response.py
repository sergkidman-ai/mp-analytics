# поток: prc
from unittest import TestCase
from unittest.mock import Mock, patch

from collectors import set_cost


class MixResponseTests(TestCase):
    def test_component_objects_are_stored_as_external_codes(self):
        response = Mock()
        response.json.return_value = [{"external_code": "2860"}, {"external_code": "2861"}]
        with patch.object(set_cost.SESSION, 'post', return_value=response):
            self.assertEqual(set_cost._mix('4297'), ['2860', '2861'])

    def test_not_mix_remains_empty(self):
        response = Mock()
        response.json.return_value = {"error": "not_mix"}
        with patch.object(set_cost.SESSION, 'post', return_value=response):
            self.assertIsNone(set_cost._mix('0274'))

    def test_other_api_error_is_not_silently_treated_as_not_mix(self):
        response = Mock()
        response.json.return_value = {"error": "database_unavailable"}
        with patch.object(set_cost.SESSION, 'post', return_value=response):
            with self.assertRaises(RuntimeError):
                set_cost._mix('4297')

    def test_rate_limit_waits_before_retry(self):
        limited = Mock(status_code=429)
        accepted = Mock(status_code=200)
        accepted.json.return_value = [{"external_code": "2860"}]
        with (patch.object(set_cost.SESSION, 'post', side_effect=[limited, accepted]),
              patch.object(set_cost.time, 'sleep') as sleep):
            self.assertEqual(set_cost._mix('4297'), ['2860'])
        sleep.assert_called_once_with(65)

    def test_unknown_catalog_code_is_separate_from_transient_error(self):
        response = Mock(status_code=422)
        response.json.return_value = {'errors': {'external_code': ['unknown']}}
        with patch.object(set_cost.SESSION, 'post', return_value=response):
            with self.assertRaises(set_cost.UnknownCode):
                set_cost._mix('3576')
