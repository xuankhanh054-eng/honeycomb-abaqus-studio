"""An uncertain dispatch keeps its durable claim without stopping other owners."""
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from honeycomb_studio.campaign import campaign_manifest
from honeycomb_studio.campaign_queue import Dispatcher

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import campaign_controller as controller


def failure(status):
    if status is None:
        return httpx.ReadTimeout('Acknowledgement unavailable')
    response = httpx.Response(status, request=httpx.Request('POST', 'https://api.github.com/dispatches'))
    return httpx.HTTPStatusError('Dispatch unavailable', request=response.request, response=response)


def test_uncertain_dispatch_preserves_the_exact_durable_ticket(monkeypatch):
    manifest = campaign_manifest()
    row = manifest['designs'][0]
    saved = {}
    work = {}

    def update(_campaign, _owner, _design, transform):
        saved.update(transform(saved or row))
        return {'designs': [saved]}

    def request(*_args):
        raise failure(500)

    monkeypatch.setattr('honeycomb_studio.campaign_queue.admission', lambda *_args: True)
    monkeypatch.setattr('honeycomb_studio.campaign_queue.Catalog', lambda _github: SimpleNamespace(
        update=lambda _key, transform: work.update(transform(None))))
    store = SimpleNamespace(update_design=update)
    app = SimpleNamespace(repository=lambda _: SimpleNamespace(repo=request))
    with pytest.raises(httpx.HTTPStatusError):
        Dispatcher(store, app).reserve({**manifest, 'source_sha': 'a' * 40, 'runtime_hash': 'b' * 64}, row)
    assert saved['status'] == 'dispatching' and saved['ticket'] == work['ticket']
    assert work['status'] == 'ready' and 'workflow_run_id' not in work


@pytest.mark.parametrize('status', [500, 502, 503, 504, None])
def test_refill_continues_and_reports_an_uncertain_dispatch(monkeypatch, status):
    manifest = campaign_manifest()
    owner = manifest['designs'][0]['owner']
    rows = [r for r in manifest['designs'] if r['owner'] == owner][:2]
    attempted = []

    def reserve(_manifest, row):
        attempted.append(row['id'])
        if len(attempted) == 1:
            raise failure(status)

    monkeypatch.setattr(controller, 'page_jobs', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(controller, 'admission', lambda *_args: True)
    monkeypatch.setattr(controller, 'Dispatcher', lambda *_args: SimpleNamespace(reserve=reserve))
    _, observation = controller.fill_owner_slots(None, SimpleNamespace(repository=lambda _: object()),
                                                manifest, rows, 160, owner)
    assert attempted == [r['id'] for r in rows]
    assert observation['dispatch_warnings'][0]['design_id'] == rows[0]['id']
    assert observation['dispatch_warnings'][0]['http_status'] == status


def test_permanent_dispatch_permission_error_is_still_reported(monkeypatch):
    manifest = campaign_manifest()
    row = manifest['designs'][0]
    monkeypatch.setattr(controller, 'page_jobs', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(controller, 'admission', lambda *_args: True)

    def reserve(*_args):
        raise failure(403)

    monkeypatch.setattr(controller, 'Dispatcher', lambda *_args: SimpleNamespace(reserve=reserve))
    with pytest.raises(httpx.HTTPStatusError):
        controller.fill_owner_slots(None, SimpleNamespace(repository=lambda _: object()),
                                    manifest, [row], 160, row['owner'])


def test_uncertain_same_ticket_redispatch_does_not_stop_reconciliation(monkeypatch):
    manifest = campaign_manifest()
    row = manifest['designs'][0]
    row.update(status='dispatching', ticket='a' * 32, dispatch_started_at=0.)
    work = {'status': 'ready', 'source_sha': 'b' * 40}
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == 'GET':
            return {'workflow_runs': []}
        raise failure(500)

    monkeypatch.setattr(controller, 'Catalog', lambda _: SimpleNamespace(get=lambda _: (work, None)))
    app = SimpleNamespace(repository=lambda _: SimpleNamespace(repo=request))
    controller.reconcile_owner(None, manifest, app, row['owner'])
    assert row['status'] == 'dispatching' and row['ticket'] == 'a' * 32
    assert row['dispatch_started_at'] > 0.
    assert row['dispatch_warning'].startswith('Dispatch acknowledgement unavailable')
    assert calls[-1][2]['inputs']['ticket'] == row['ticket']
