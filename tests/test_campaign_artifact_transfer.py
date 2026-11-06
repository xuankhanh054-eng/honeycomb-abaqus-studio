import hashlib
import importlib.util
import io
import re
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


def tool():
    path = Path(__file__).parents[1] / 'scripts' / 'campaign_artifact_transfer.py'
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('artifact_transfer_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_network_breaks_retry_only_the_uncommitted_range_and_preserve_sha256(tmp_path, monkeypatch):
    module = tool()
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    payload = bytes(range(128))
    requests, refreshed = [], []

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b'partial'
            raise httpx.ReadError('connection reset')

    def transport(request):
        first, last = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', request.headers['range']).groups())
        requests.append(first)
        headers = {'Content-Range': f'bytes {first}-{last}/{len(payload)}'}
        if first == 32 and requests.count(32) <= 6:
            return httpx.Response(206, headers=headers, stream=Interrupted())
        return httpx.Response(206, headers=headers, content=payload[first:last + 1])

    def signed_url():
        refreshed.append(True)
        return 'https://storage.example/immutable'

    target = tmp_path / 'artifact.zip'
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        module.transfer(client, signed_url, len(payload), 'sha256:' + hashlib.sha256(payload).hexdigest(),
                        target, chunk_size=32)
    assert target.read_bytes() == payload
    assert requests == [0] + [32] * 7 + [64, 96]
    assert len(refreshed) == 7


def test_existing_download_prefix_resumes_at_its_exact_byte_offset(tmp_path):
    module = tool()
    payload = bytes(range(128))
    offsets = []

    def transport(request):
        first, last = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', request.headers['range']).groups())
        offsets.append(first)
        return httpx.Response(206, content=payload[first:last + 1],
                              headers={'Content-Range': f'bytes {first}-{last}/{len(payload)}'})

    target = tmp_path / 'artifact.zip'
    target.write_bytes(payload[:64])
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        module.transfer(client, lambda: 'https://storage.example/immutable', len(payload),
                        'sha256:' + hashlib.sha256(payload).hexdigest(), target, chunk_size=32)
    assert offsets == [64, 96] and target.read_bytes() == payload


@pytest.mark.parametrize('problem', ['none', 'bad_digest', 'bad_crc', 'traversal', 'symlink'])
def test_only_whole_verified_safe_archives_become_visible(tmp_path, monkeypatch, problem):
    module = tool()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('projects/run/raw/hc.odb', b'original complete native bytes')
        if problem == 'traversal':
            archive.writestr('../escaped', b'bad')
        if problem == 'symlink':
            link = zipfile.ZipInfo('unsafe-link')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, '/outside')
    payload = buffer.getvalue()
    if problem == 'bad_crc':
        payload = payload.replace(b'original complete native bytes', b'corrupt! complete native bytes')
    digest = 'sha256:' + ('0' * 64 if problem == 'bad_digest' else hashlib.sha256(payload).hexdigest())

    def transport(request):
        first, last = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', request.headers['range']).groups())
        return httpx.Response(206, content=payload[first:last + 1],
                              headers={'Content-Range': f'bytes {first}-{last}/{len(payload)}'})

    client = httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(module.httpx, 'Client', lambda **kwargs: client)
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10**12))
    artifact = dict(id=99, name='core-damage-' + 'a' * 32, expired=False, digest=digest, size_in_bytes=len(payload))
    github = SimpleNamespace(repository='owner/repo', access_token=lambda: 'test',
        repo=lambda *a: {'artifacts': [artifact]}, client=SimpleNamespace(get=lambda *a, **kw:
            httpx.Response(302, headers={'location': 'https://storage.example/immutable'})))
    target = tmp_path / 'results'
    if problem == 'none':
        proof = module.download(github, 123, 'a' * 32, target)
        assert proof['digest'] == digest
        assert (target / 'projects/run/raw/hc.odb').read_bytes() == b'original complete native bytes'
    else:
        with pytest.raises((ValueError, zipfile.BadZipFile)):
            module.download(github, 123, 'a' * 32, target)
        assert not target.exists()
    assert not (tmp_path / 'escaped').exists()
