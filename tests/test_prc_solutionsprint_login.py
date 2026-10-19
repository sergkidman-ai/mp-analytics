# поток: prc
from unittest.mock import Mock

import pytest
import requests

from prices import solutionsprint_source as source


FORM = b'<form action="/personal/"><input type="hidden" name="sessid" value="test"><input name="USER_PASSWORD"></form>'
PERSONAL = b'<html><a href="/?logout=yes">Exit</a></html>'


def response(status, body, url="https://cartridge.ru/personal/"):
    result = requests.Response()
    result.status_code, result._content, result.url = status, body, url
    return result


def session(monkeypatch, post_status, post_body, personal_body=PERSONAL, personal_status=200):
    monkeypatch.setenv("SOLUTIONSPRINT_LOGIN", "test")
    monkeypatch.setenv("SOLUTIONSPRINT_PASSWORD", "test")
    client = Mock()
    client.request.side_effect = [response(200, FORM), response(post_status, post_body),
                                  response(personal_status, personal_body)]
    return client


def test_login_500_requires_confirmed_session_without_repeating_password(monkeypatch):
    client = session(monkeypatch, 500, b"")
    source.login(client)
    assert [c.args[0] for c in client.request.call_args_list] == ["GET", "POST", "GET"]
    assert "data" not in client.request.call_args_list[-1].kwargs


@pytest.mark.parametrize("status,body", [(500, b""), (200, FORM)])
@pytest.mark.parametrize("personal", [FORM, PERSONAL + FORM, b"<html>Public page</html>"])
def test_unconfirmed_session_stops_login(monkeypatch, status, body, personal):
    client = session(monkeypatch, status, body, personal)
    with pytest.raises(RuntimeError, match="authorization not confirmed"):
        source.login(client)
    assert client.request.call_count == 3


def test_normal_success_needs_no_followup(monkeypatch):
    client = session(monkeypatch, 200, PERSONAL)
    source.login(client)
    assert client.request.call_count == 2


def test_unconfirmed_post_200_can_be_verified_by_get(monkeypatch):
    client = session(monkeypatch, 200, FORM)
    source.login(client)
    assert client.request.call_count == 3


@pytest.mark.parametrize("status", [401, 403, 429, 502, 503])
def test_other_login_errors_are_not_ignored(monkeypatch, status):
    client = session(monkeypatch, status, b"Error")
    with pytest.raises(RuntimeError, match="request failed"):
        source.login(client)
    assert client.request.call_count == 2


def test_followup_500_is_rejected(monkeypatch):
    client = session(monkeypatch, 500, b"", personal_status=500)
    with pytest.raises(RuntimeError, match="request failed"):
        source.login(client)


def test_catalogue_500_still_fails():
    client = Mock()
    client.request.return_value = response(500, b"Error")
    with pytest.raises(RuntimeError, match="request failed"):
        source.request(client, "GET", "/catalog/")


def test_login_500_cannot_bypass_supplier_host_check(monkeypatch):
    client = session(monkeypatch, 500, b"")
    client.request.side_effect = [response(200, FORM), response(500, b"", "https://example.com/")]
    with pytest.raises(RuntimeError, match="Unexpected supplier redirect"):
        source.login(client)


def test_transport_failure_is_sanitized_and_not_retried(monkeypatch):
    client = session(monkeypatch, 500, b"")
    client.request.side_effect = [response(200, FORM), requests.Timeout("secret in error")]
    with pytest.raises(RuntimeError, match=r"^Solutions Print request failed \(credentials redacted\)$"):
        source.login(client)
    assert client.request.call_count == 2
