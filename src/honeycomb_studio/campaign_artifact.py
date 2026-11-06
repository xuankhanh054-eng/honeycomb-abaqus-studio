"""Bounded inspection of immutable failure artifacts before any solver retry."""
import io
import json
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import httpx

from .campaign import one_run_spec
from .models import BatchSpec, digest, expand
from .solver import sta_progress

ROOT = PurePosixPath('/tmp/honeycomb-results')
MAX_MEMBER = 2 * 1024 * 1024


class ArtifactRanges(io.RawIOBase):
    """Never buffer the whole artifact, even if a server ignores Range."""

    def __init__(self, client, url):
        self.client, self.url, self.position, self.downloaded = client, url, 0, 0
        with client.stream('GET', url, headers={'Range': 'bytes=0-0'}) as response:
            if response.status_code != 206:
                raise ValueError('Artifact server did not accept a bounded range')
            self.length = int(response.headers['content-range'].split('/')[-1])

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else (self.position if whence == 1 else self.length) + offset
        if whence not in (0, 1, 2) or position < 0:
            raise ValueError('Invalid artifact seek')
        self.position = position
        return position

    def read(self, size=-1):
        count = self.length - self.position if size < 0 else min(size, self.length - self.position)
        if count <= 0:
            return b''
        if count > 4 * 1024 * 1024 or self.downloaded + count > 16 * 1024 * 1024:
            raise ValueError('Artifact inspection exceeded its bounded byte budget')
        end = self.position + count - 1
        with self.client.stream('GET', self.url, headers={'Range': f'bytes={self.position}-{end}'}) as response:
            expected = f'bytes {self.position}-{end}/{self.length}'
            if response.status_code != 206 or response.headers.get('content-range') != expected:
                raise ValueError('Artifact response does not match the requested bounded range')
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                data.extend(chunk)
                if len(data) > count:
                    raise ValueError('Artifact range response is too large')
        if len(data) != count:
            raise ValueError('Artifact range length differs')
        self.position += count
        self.downloaded += count
        return bytes(data)


def member(archive, name):
    matches = [i for i in archive.infolist() if i.filename == name]
    if len(matches) != 1 or matches[0].file_size > MAX_MEMBER or matches[0].compress_size > MAX_MEMBER:
        raise ValueError('Original artifact metadata is missing, ambiguous, or too large')
    return archive.read(matches[0])  # zipfile checks the member CRC.


def inspect_completed_odb(archive, work):
    matches = [i for i in archive.infolist() if i.filename.endswith('/run.json')]
    if len(matches) != 1:
        raise ValueError('Artifact must identify exactly one original native run')
    run = json.loads(member(archive, matches[0].filename))
    expected = expand(BatchSpec.model_validate(one_run_spec(work['design'])))[0]
    if (run.get('input_hash') != expected['input_hash'] or run.get('resolved') != expected['resolved']
            or digest(run.get('resolved')) != work['design']['physics_hash']):
        raise ValueError('Artifact scientific inputs differ from the frozen design')
    if not run.get('attempts'):
        return None
    attempt = run['attempts'][-1]
    relative = PurePosixPath(attempt['path']).relative_to(ROOT)
    run_path = PurePosixPath(run['path']).relative_to(ROOT)
    if ('..' in relative.parts or '\\' in str(relative)
            or str(run_path / 'run.json') != matches[0].filename
            or relative.parent != run_path / 'attempts'):
        raise ValueError('Artifact attempt is outside its original run')
    job = attempt['job_name']
    if not job or PurePosixPath(job).name != job or '\\' in job:
        raise ValueError('Artifact job name is invalid')
    raw = relative / 'raw'
    sta_name = str(raw / (job + '.sta'))
    if sta_name not in archive.namelist():
        if attempt.get('solver_progress', {}).get('solver_success'):
            raise ValueError('Completed solver lacks its independent STA evidence')
        return None
    with tempfile.TemporaryDirectory(prefix='campaign-sta-') as temporary:
        sta_file = Path(temporary) / 'original.sta'
        sta_file.write_bytes(member(archive, sta_name))
        sta = sta_progress(sta_file, expected['resolved']['duration'])
    if not sta.get('solver_success'):
        return None
    odbs = [i for i in archive.infolist() if i.filename == str(raw / (job + '.odb'))]
    if len(odbs) != 1 or odbs[0].file_size <= 0:
        raise ValueError('Completed solver has no retained original ODB')
    return {**sta, 'run_id': run['id'], 'input_hash': run['input_hash'],
            'odb_bytes': odbs[0].file_size, 'odb_member': odbs[0].filename}


def completed_odb_evidence(github, work):
    workflow = work['workflow_run_id']
    artifacts = github.repo('GET', f'/actions/runs/{workflow}/artifacts?per_page=100')['artifacts']
    matches = [a for a in artifacts if a['name'] == 'core-damage-' + work['ticket'] and not a['expired']]
    if len(matches) != 1:
        raise ValueError('Failed solver output is unavailable for inspection; do not blindly rerun FE')
    artifact = matches[0]
    response = github.client.get(
        f'https://api.github.com/repos/{github.repository}/actions/artifacts/{artifact["id"]}/zip',
        headers={'Authorization': 'Bearer ' + github.access_token()}, follow_redirects=False)
    if response.status_code != 302 or not response.headers.get('location', '').startswith('https://'):
        raise ValueError('Unable to authorize bounded artifact inspection')
    # Signed storage URLs and credentials remain in memory and are never logged.
    with httpx.Client(timeout=30, follow_redirects=True) as transport:
        with zipfile.ZipFile(ArtifactRanges(transport, response.headers['location'])) as archive:
            evidence = inspect_completed_odb(archive, work)
    return {**evidence, 'artifact_id': artifact['id'], 'artifact_digest': artifact.get('digest')} if evidence else None
