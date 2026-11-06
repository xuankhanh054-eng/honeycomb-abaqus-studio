import io
import json
import re
import zipfile

import httpx
import pytest

from honeycomb_studio.campaign import one_run_spec, record, reference_model
from honeycomb_studio.campaign_artifact import ArtifactRanges, inspect_completed_odb
from honeycomb_studio.models import BatchSpec, expand

BASE = 'projects/project/batches/batch/runs/run'


def artifact(problem=None):
    design = record(reference_model(), 'development', 1, 'xuankhanh24', [])
    expected = expand(BatchSpec.model_validate(one_run_spec(design)))[0]
    run = dict(id='run', path='/tmp/honeycomb-results/' + BASE,
               input_hash=expected['input_hash'], resolved=expected['resolved'],
               attempts=[dict(path='/tmp/honeycomb-results/' + BASE + '/attempts/001', job_name='hc')])
    if problem == 'different_input':
        run['input_hash'] = 'wrong'
    if problem == 'outside_attempt':
        run['attempts'][0]['path'] = '/tmp/honeycomb-results/../../outside'
    duration = '4.000E-03' if problem == 'partial_solver' else '8.000E-03'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(BASE + '/run.json', json.dumps(run))
        raw = BASE + '/attempts/001/raw/'
        archive.writestr(raw + 'hc.sta',
            f' 101338 {duration} {duration} 00:23:48 5.704E-08 26813 3.013E+02 7.002E+03\n'
            ' THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n')
        archive.writestr(BASE + '/attempts/001/solver-complete.json', '{"sta":{"solver_success":true}}')
        if problem != 'missing_odb':
            archive.writestr(raw + 'hc.odb', b'original native ODB' * 100000)
        if problem == 'ambiguous_run':
            archive.writestr('other/run.json', json.dumps(run))
    return buffer.getvalue(), {'design': design}


def test_bounded_ranges_read_sta_and_inputs_without_downloading_native_odb():
    blob, work = artifact()
    requested = []

    def transport(request):
        first, last = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', request.headers['range']).groups())
        requested.append(last - first + 1)
        return httpx.Response(206, content=blob[first:last + 1],
                              headers={'Content-Range': f'bytes {first}-{last}/{len(blob)}'})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with zipfile.ZipFile(ArtifactRanges(client, 'https://storage.example/immutable')) as archive:
            evidence = inspect_completed_odb(archive, work)
    assert evidence['solver_success'] and evidence['simulation_time_s'] == .008
    assert evidence['odb_bytes'] == len(b'original native ODB') * 100000
    assert sum(requested) < 30000


def test_partial_solver_is_not_accepted_from_a_stale_complete_marker():
    blob, work = artifact('partial_solver')
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert inspect_completed_odb(archive, work) is None


@pytest.mark.parametrize('problem', ['different_input', 'missing_odb', 'outside_attempt', 'ambiguous_run'])
def test_uncertain_or_mismatched_original_output_is_not_recomputed(problem):
    blob, work = artifact(problem)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive, pytest.raises(ValueError):
        inspect_completed_odb(archive, work)


def test_server_ignoring_range_is_rejected_without_reading_its_body():
    reads = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(True)
            yield b'must not download this potentially huge body'

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))) as client:
        with pytest.raises(ValueError, match='bounded range'):
            ArtifactRanges(client, 'https://storage.example/immutable')
    assert not reads


def test_range_identity_must_match_the_exact_requested_offsets():
    calls = []

    def transport(request):
        calls.append(request.headers['range'])
        return httpx.Response(206, content=b'x', headers={'Content-Range': 'bytes 0-0/100'})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        ranges = ArtifactRanges(client, 'https://storage.example/immutable')
        ranges.seek(50)
        with pytest.raises(ValueError, match='requested bounded range'):
            ranges.read(1)
        with pytest.raises(ValueError, match='byte budget'):
            ranges.length = 10 * 1024 * 1024
            ranges.read(5 * 1024 * 1024)
    assert calls == ['bytes=0-0', 'bytes=50-50']
