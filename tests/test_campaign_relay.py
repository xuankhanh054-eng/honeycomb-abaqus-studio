import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import campaign_controller as controller


@pytest.mark.parametrize('accepted', [False, True])
def test_relay_reconciles_uncertain_timeout_before_retrying_dispatch(monkeypatch, accepted):
    monkeypatch.setattr(controller.time, 'sleep', lambda seconds: None)
    runs = [{'id': 11, 'status': 'in_progress'}]
    calls = []

    def request(method, path, body=None):
        if method == 'GET':
            return {'workflow_runs': runs}
        calls.append(body)
        if len(calls) == 1:
            if accepted:
                runs.append({'id': 12, 'status': 'queued'})
            response = httpx.Response(504, request=httpx.Request('POST', 'https://api.github.com/dispatches'))
            response.raise_for_status()

    store = SimpleNamespace(catalog=SimpleNamespace(get=lambda _: ({'enabled': True, 'phase': 'pilot'}, None)))
    app = SimpleNamespace(repository=lambda _: SimpleNamespace(repo=request))
    result = controller.relay(store, app, 'core-damage-v2-20261107', '11')
    assert len(calls) == (1 if accepted else 2)
    assert result['controller_redispatched'] is (not accepted)
    if accepted:
        assert result['reason'] == 'Another controller is already queued'


@pytest.mark.parametrize('code, expected', [(403, 1), (503, 3)])
def test_relay_does_not_hide_permanent_or_exhausted_errors(monkeypatch, code, expected):
    monkeypatch.setattr(controller.time, 'sleep', lambda seconds: None)
    calls = []

    def request(method, path, body=None):
        if method == 'GET':
            return {'workflow_runs': [{'id': 11, 'status': 'in_progress'}]}
        calls.append(body)
        httpx.Response(code, request=httpx.Request('POST', 'https://api.github.com/dispatches')).raise_for_status()

    store = SimpleNamespace(catalog=SimpleNamespace(get=lambda _: ({'enabled': True, 'phase': 'pilot'}, None)))
    app = SimpleNamespace(repository=lambda _: SimpleNamespace(repo=request))
    with pytest.raises(httpx.HTTPStatusError):
        controller.relay(store, app, 'core-damage-v2-20261107', '11')
    assert len(calls) == expected

