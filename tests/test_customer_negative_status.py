# поток: fin
from fastapi import HTTPException
import pytest

from web.customer_guard_api import negative_filter


def test_negative_group_filters_cover_cancel_and_return_without_other_statuses():
    fbs, fbo = negative_filter('all')
    assert "cancelled" in fbs[0] and "returned_defective" in fbo[0]
    assert fbs[1] == fbo[1] == []
    fbs, fbo = negative_filter('returned')
    assert "cancelled" not in fbs[0] and "returned" in fbo[0]


def test_specific_status_is_scoped_to_one_scheme_and_uses_bound_values():
    fbs, fbo = negative_filter('fbs:wb:complete:defect')
    assert fbs[1] == ['wb', 'complete:defect']
    assert fbo == ('false', [])
    fbs, fbo = negative_filter('fbo:wb:sales_return')
    assert fbs == ('false', [])
    assert fbo[1] == ['wb', 'sales_return']


@pytest.mark.parametrize('value', ['fbs:wb:', 'fbs:other:cancelled',
                                   'fbo:wb:' + 'x' * 121, 'cancelled;DROP TABLE orders'])
def test_unknown_or_unsafe_filter_is_rejected(value):
    with pytest.raises(HTTPException) as error:
        negative_filter(value)
    assert error.value.status_code == 422
