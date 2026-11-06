import importlib.util
import sys
from pathlib import Path

import pytest

from honeycomb_studio.campaign import one_run_spec, record, reference_model
from honeycomb_studio.models import BatchSpec, expand
from honeycomb_studio.store import Store


def tool():
    scripts = Path(__file__).parents[1] / 'scripts'
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location('campaign_postprocess_test', scripts / 'campaign_postprocess.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path):
    design = record(reference_model(), 'development', 1, 'xuankhanh24', [])
    spec = BatchSpec.model_validate(one_run_spec(design))
    store = Store(tmp_path)
    batch = store.create(spec)
    run = store.list('runs', batch['id'])[0]
    attempt = Path(run['path']) / 'attempts' / '001'
    raw = attempt / 'raw'
    raw.mkdir(parents=True)
    (raw / 'hc.odb').write_bytes(b'unchanged completed native ODB')
    (raw / 'hc.sta').write_text(
        ' 101338 8.000E-03 8.000E-03 00:23:48 5.704E-08 26813 3.013E+02 7.002E+03\n'
        ' THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n')
    run.update(status='interrupted', attempts=[{'path': str(attempt), 'job_name': 'hc', 'process': None}])
    store.save('runs', run)
    return store, design, run, expand(spec)[0]


def test_completed_solver_without_restart_ack_can_resume_only_postprocessing(tmp_path):
    module = tool()
    store, design, run, _ = fixture(tmp_path)
    assert not (Path(run['attempts'][0]['path']) / 'raw' / 'continuation-stop-ack.json').exists()
    actual, batch, evidence = module.prepare_completed_run(store, {'design': design})
    assert actual['id'] == run['id'] and actual['status'] == 'queued'
    assert len(actual['attempts']) == 1 and not actual.get('explicit_recover')
    assert evidence['solver_success'] is True
    assert batch['pause_requested'] is False


@pytest.mark.parametrize('problem', ['short_analysis', 'different_input', 'missing_odb'])
def test_postprocess_rejects_partial_or_different_solver_bytes(tmp_path, problem):
    module = tool()
    store, design, run, _ = fixture(tmp_path)
    raw = Path(run['attempts'][0]['path']) / 'raw'
    if problem == 'short_analysis':
        text = (raw / 'hc.sta').read_text().replace('8.000E-03', '4.000E-03')
        (raw / 'hc.sta').write_text(text)
    elif problem == 'different_input':
        run['input_hash'] = 'wrong'
        store.save('runs', run)
    else:
        (raw / 'hc.odb').unlink()
    with pytest.raises(ValueError):
        module.prepare_completed_run(store, {'design': design})


def test_postprocess_engine_refuses_any_new_fe_process(tmp_path):
    module = tool()
    store, _, _, _ = fixture(tmp_path)
    engine = module.PostprocessOnlyEngine(store, abaqus='never-launch', fake=False)
    for phase in ('building', 'checking', 'solving'):
        with pytest.raises(ValueError, match='FE process'):
            engine._monitor({}, {}, phase, ['forbidden'])


def test_recovery_uses_the_original_attempt_and_odb(tmp_path, monkeypatch):
    module = tool()
    store, design, _, _ = fixture(tmp_path)
    run, batch, _ = module.prepare_completed_run(store, {'design': design})
    engine = module.PostprocessOnlyEngine(store, abaqus='must-not-run-fe', fake=False)
    recovered = []
    monkeypatch.setattr(engine, '_postprocess', lambda r, attempt, b: recovered.append(attempt['path']))
    engine._execute(run, batch)
    assert recovered == [run['attempts'][0]['path']]
    assert len(store.get('runs', run['id'])['attempts']) == 1
    assert (Path(recovered[0]) / 'raw' / 'hc.odb').read_bytes() == b'unchanged completed native ODB'


def test_postprocess_claim_rejects_a_duplicate_or_other_source():
    module = tool()
    work = dict(publication_kind='postprocess', publication_status='ready',
                workflow_run_id=123, source_sha='original')
    claim = module.claim_recovery(work, 456, 123, 'original')
    assert claim['publication_workflow_run_id'] == 456
    with pytest.raises(ValueError, match='exclusive'):
        module.claim_recovery(claim, 789, 123, 'original')
    with pytest.raises(ValueError, match='exclusive'):
        module.claim_recovery(work, 456, 123, 'different')


def test_controller_retries_postprocessing_with_separate_original_and_tool_revisions(monkeypatch):
    tool()
    import campaign_controller
    work = dict(ticket='a' * 32, workflow_run_id=123, source_sha='original-source',
                publication_workflow_run_id=456, publication_attempt=1,
                publication_kind='postprocess', postprocess_source_sha='recovery-tool-source',
                postprocess_artifact_run_id=987)
    calls = []

    class Github:
        def repo(self, method, path, body=None):
            calls.append((method, path, body))
            if 'artifacts?' in path:
                return {'artifacts': [{'id': 100, 'name': 'retained', 'expired': False}]}
            return {'status': 'completed', 'conclusion': 'failure'}

    class Catalog:
        def __init__(self, github):
            pass

        def get(self, key):
            return work, None

        def update(self, key, transform):
            return transform(work)

    monkeypatch.setattr(campaign_controller, 'Catalog', Catalog)
    from types import SimpleNamespace
    app = SimpleNamespace(repository=lambda repo: Github())
    row = dict(repository='owner/repo', publication_ticket='a' * 32, recoverable_artifact='retained')
    campaign_controller.reconcile_publication({'id': 'core-damage-v2-20261107'}, row, app)
    posted = [c for c in calls if c[0] == 'POST']
    assert len(posted) == 1 and posted[0][1].endswith('core-damage-postprocess.yml/dispatches')
    assert posted[0][2]['inputs'] == dict(campaign_id='core-damage-v2-20261107', ticket='a' * 32,
        source_sha='original-source', original_run_id='123',
        processor_source_sha='recovery-tool-source', artifact_run_id='987')


@pytest.mark.parametrize('probe,visible_controller,expected', [(True, False, 0), (False, False, 1), (True, True, 0)])
def test_refill_preserves_controller_slot_even_before_its_job_is_visible(monkeypatch, probe, visible_controller, expected):
    tool()
    from types import SimpleNamespace

    import campaign_controller
    jobs = [dict(status='in_progress', campaign_solver=True) for _ in range(18)]
    if probe:
        jobs.append(dict(status='in_progress', campaign_solver=False, workflow_path='.github/workflows/core-damage-bond-probe.yml'))
    if visible_controller:
        jobs.append(dict(status='in_progress', campaign_solver=False, workflow_path='.github/workflows/core-damage-controller.yml'))
    reserved = []
    monkeypatch.setattr(campaign_controller, 'page_jobs', lambda *args, **kwargs: jobs)
    monkeypatch.setattr(campaign_controller, 'admission', lambda *args: True)
    monkeypatch.setattr(campaign_controller, 'Dispatcher', lambda *args: SimpleNamespace(reserve=lambda m, r: reserved.append(r)))
    rows = [dict(owner='xuankhanh24', status='pending') for _ in range(3)]
    app = SimpleNamespace(repository=lambda repo: object())
    campaign_controller.fill_owner_slots(None, app, {'designs': [], 'pilot_designs': []}, rows, 160, 'xuankhanh24')
    assert len(reserved) == expected
