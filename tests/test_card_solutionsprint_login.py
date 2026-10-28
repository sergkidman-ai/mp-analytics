import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.card_solutionsprint_scraper import login


FORM = b'<form action="/personal/"><input type="hidden" name="sessid" value="test"><input name="USER_PASSWORD"></form>'
PERSONAL = b'<html><a href="/?logout=yes">Exit</a></html>'


def response(status, content, url='https://cartridge.ru/personal/'):
    return SimpleNamespace(status_code=status, content=content, url=url)


class LoginVerificationTests(unittest.TestCase):
    def run_login(self, post_status, post_body, personal_body=PERSONAL, personal_status=200):
        session = Mock()
        session.get.side_effect = [response(200, FORM), response(personal_status, personal_body)]
        session.post.return_value = response(post_status, post_body)
        with patch.dict('os.environ', {'SOLUTIONSPRINT_LOGIN': 'test', 'SOLUTIONSPRINT_PASSWORD': 'test'}):
            result = login(session)
        session.post.assert_called_once()
        return result, session

    def test_500_can_establish_session_but_requires_personal_verification(self):
        result, session = self.run_login(500, b'')
        self.assertTrue(result['authenticated'])
        self.assertEqual(result['login_post_status'], 500)
        self.assertEqual(session.get.call_count, 2)

    def test_500_without_authenticated_session_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Authenticated session not confirmed'):
            self.run_login(500, b'Error', FORM)

    def test_logout_link_does_not_override_password_form(self):
        with self.assertRaisesRegex(RuntimeError, 'Authenticated session not confirmed'):
            self.run_login(500, b'Error', PERSONAL + FORM)

    def test_failed_followup_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Supplier HTTP 500'):
            self.run_login(500, b'Error', b'Error', personal_status=500)

    def test_normal_success_needs_no_extra_request(self):
        result, session = self.run_login(200, PERSONAL)
        self.assertTrue(result['authenticated'])
        self.assertEqual(session.get.call_count, 1)

    def test_403_does_not_attempt_to_reauthenticate(self):
        with self.assertRaisesRegex(RuntimeError, 'Supplier login HTTP 403'):
            self.run_login(403, b'Forbidden')


if __name__ == '__main__':
    unittest.main()
