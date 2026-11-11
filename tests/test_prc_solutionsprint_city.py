# поток: prc
from unittest.mock import Mock, patch

import pytest

from prices import solutionsprint_source as source


def page(city="Москва", codes=("0000073738",), authorized=True):
    locations = "".join(f'<li data-code="{code}">Москва</li>' for code in codes)
    body = '<body class="b2b-body">' if authorized else '<body>'
    return (f'<html>{body}<div class="hdr-top__change-city">Ваш город: {city}</div>'
            f'{locations}</body></html>').encode()


def test_moscow_session_is_left_in_place():
    with patch.object(source, "request", return_value=Mock(content=page())) as request:
        source.moscow_catalogue(Mock())
    assert request.call_count == 1
    assert request.call_args.args[1:] == ("GET", "/catalog/")


def test_geoip_region_uses_moscow_code_from_supplier_and_verifies_readback():
    replies = [Mock(content=page("Санкт-Петербург", ("0000001234",))), Mock(),
               Mock(content=page())]
    with patch.object(source, "request", side_effect=replies) as request:
        source.moscow_catalogue(Mock())
    assert [call.args[1] for call in request.call_args_list] == ["GET", "POST", "GET"]
    assert request.call_args_list[1].kwargs["data"] == {"LOCATION": "0000001234"}


def test_successful_post_without_region_change_is_rejected():
    replies = [Mock(content=page("Санкт-Петербург")), Mock(),
               Mock(content=page("Санкт-Петербург"))]
    with patch.object(source, "request", side_effect=replies):
        with pytest.raises(RuntimeError, match="Moscow price region not confirmed"):
            source.moscow_catalogue(Mock())


@pytest.mark.parametrize("codes", [(), ("0000001234", "0000005678"), ("invalid",)])
def test_missing_ambiguous_or_invalid_location_never_posts(codes):
    with patch.object(source, "request", return_value=Mock(content=page("Санкт-Петербург", codes))) as request:
        with pytest.raises(RuntimeError, match="Unique Moscow location code missing"):
            source.moscow_catalogue(Mock())
    assert request.call_count == 1


def test_unauthenticated_catalogue_never_changes_region():
    with patch.object(source, "request", return_value=Mock(content=page(authorized=False))) as request:
        with pytest.raises(RuntimeError, match="Authorized B2B catalogue not confirmed"):
            source.moscow_catalogue(Mock())
    assert request.call_count == 1
