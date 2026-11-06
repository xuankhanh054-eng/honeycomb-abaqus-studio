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


@pytest.mark.parametrize('workers, expected', [(19, 0), (18, 1), (17, 2)])
@pytest.mark.parametrize('controller_visible', [False, True])
def test_publication_recovery_preserves_controller_slot_across_multiple_claims(monkeypatch, workers,
                                                                              expected, controller_visible):
    jobs = [{'status': 'in_progress', 'campaign_solver': True} for _ in range(workers)]
    if controller_visible:
        jobs.append({'status': 'in_progress', 'workflow_path': '.github/workflows/core-damage-controller.yml'})
    inventories, dispatched, updates = [], [], []
    monkeypatch.setattr(controller, 'page_jobs', lambda *a, **kw: inventories.append(True) or jobs)
    works = {ticket: dict(ticket=ticket, workflow_run_id=123, source_sha='original',
        publication_kind='postprocess', publication_status='pending', postprocess_source_sha='tools')
        for ticket in ('a' * 32, 'b' * 32)}

    class Catalog:
        def __init__(self, github):
            pass

        def get(self, key):
            return works[key.rsplit('/', 1)[-1]], None

        def update(self, key, transform):
            ticket = key.rsplit('/', 1)[-1]
            works[ticket] = transform(works[ticket])
            updates.append(ticket)

    def request(method, path, body=None):
        if method == 'POST':
            dispatched.append(body)
        if '/artifacts?' in path:
            return {'artifacts': [{'id': 100, 'name': 'retained', 'expired': False}]}
        return {'status': 'completed'}

    monkeypatch.setattr(controller, 'Catalog', Catalog)
    app = SimpleNamespace(repository=lambda repo: SimpleNamespace(repo=request))
    rows = [dict(owner='xuankhanh24', repository='xuankhanh24/honeycomb-abaqus-studio',
                 publication_ticket=ticket, status='publication_pending', recoverable_artifact='retained')
            for ticket in works]
    manifest = dict(id='core-damage-v2-20261107', designs=rows, pilot_designs=[])
    reservations = {}
    controller.reconcile_owner(None, manifest, app, 'xuankhanh24', reservations)
    assert len(dispatched) == expected
    assert len(updates) == expected
    assert len(inventories) == 1
    assert reservations.get('xuankhanh24', 0) == expected


def test_new_recovery_reservation_prevents_fe_refill_stealing_controller_slot(monkeypatch):
    jobs = [{'status': 'in_progress', 'campaign_solver': True} for _ in range(18)]
    monkeypatch.setattr(controller, 'page_jobs', lambda *a, **kw: jobs)
    monkeypatch.setattr(controller, 'admission', lambda *a: True)
    dispatched = []
    monkeypatch.setattr(controller, 'Dispatcher', lambda *a: SimpleNamespace(
        reserve=lambda manifest, row: dispatched.append(row)))
    app = SimpleNamespace(repository=lambda repo: object())
    rows = [dict(owner='xuankhanh24', status='pending')]
    # The just-dispatched recovery is not yet visible in the jobs API.
    controller.fill_owner_slots(None, app, {'designs': [], 'pilot_designs': []}, rows, 160, 'xuankhanh24',
                                publication_reserved=1)
    assert not dispatched

