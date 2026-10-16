# поток: ev
import unittest
from unittest.mock import patch

from ops import mp_news as news


class PultOnlyNewsTests(unittest.TestCase):
    def test_all_platforms_continue_collecting_and_publishing_without_telegram(self):
        for platform, collector in [('wb', 'collect_wb'), ('ozon', 'collect'), ('yandex', 'collect_yandex')]:
            alert = dict(account='test', importance='alert', matched='тарифы', title='Новые тарифы', body='Текст')
            with self.subTest(platform=platform), \
                 patch.object(news, collector, side_effect=lambda *a, **kw: [dict(alert)]) as collect, \
                 patch.object(news, 'to_diary', return_value=1) as publish, \
                 patch.object(news, 'close_pauses'), \
                 patch.object(news, 'annotate_damage'), \
                 patch.object(news, 'tg') as tg:
                self.assertEqual(news.main(['--platform', platform]), 0)
                self.assertTrue(collect.called)
                publish.assert_called_once()
                self.assertEqual(publish.call_args.kwargs['platform'], platform)
                tg.assert_not_called()

    def test_legacy_sender_cannot_make_network_request(self):
        with patch.object(news.requests, 'post') as post:
            self.assertIn('отключён', news.tg('Важные изменения площадок'))
            post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
