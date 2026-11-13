# поток: rev
from unittest.mock import Mock,patch
import requests
from collectors import feedback_send as sender
from ops import rev_retry_approved_ozon as recovery


def test_exhausted_ozon_429_is_definitive_http_rejection():
    response=Mock(status_code=429,headers={'Retry-After':'0'},text='')
    response.raise_for_status.side_effect=requests.HTTPError(response=response)
    with patch.object(sender.requests,'post',return_value=response) as post, \
         patch.object(sender.time,'sleep'),patch.object(sender,'_oz_headers',return_value={}):
        try:sender.send_ozon_question('oz_acc1','123','456','Approved reply')
        except requests.HTTPError as error:
            assert error.response.status_code==429
            assert sender._retriable(error)
        else:raise AssertionError('Four 429 rejections must remain definitive')
    assert post.call_count==4


def test_no_network_never_retries_an_approved_answer():
    with patch.object(recovery.db,'query',return_value=[{'n':4}]), \
         patch.object(recovery,'reachable',return_value=False), \
         patch.object(recovery.bot,'_do_send') as publish:
        assert recovery.run()=={'network':'unreachable','retried':0}
    publish.assert_not_called()
