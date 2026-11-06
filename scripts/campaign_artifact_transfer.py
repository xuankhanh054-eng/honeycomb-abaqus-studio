"""Resume immutable artifact byte ranges, verify SHA-256, then expose complete files atomically."""
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

import httpx

sys.path.insert(0, str(Path.cwd() / 'scripts'))

from honeycomb_studio.cloud_github import Github

CHUNK = 32 * 1024 * 1024
RESERVE = 20 * 10**9


def transfer(client, signed_url, size, expected_digest, destination, *, chunk_size=CHUNK):
    digest = hashlib.sha256()
    offset = destination.stat().st_size if destination.exists() else 0
    if offset > size:
        raise ValueError('Retained transfer prefix exceeds immutable artifact size')
    if offset:
        with destination.open('rb') as previous:
            for chunk in iter(lambda: previous.read(CHUNK), b''):
                digest.update(chunk)
    url = None
    reported = offset
    with destination.open('ab') as output:
        while offset < size:
            end = min(size, offset + chunk_size) - 1
            for attempt in range(12):
                try:
                    url = url or signed_url()
                    with client.stream('GET', url, headers={'Range': f'bytes={offset}-{end}'}) as response:
                        response.raise_for_status()
                        if (response.status_code != 206 or response.headers.get('content-range')
                                != f'bytes {offset}-{end}/{size}'):
                            raise ValueError('Artifact server did not return the exact requested range')
                        data = bytearray()
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            data.extend(chunk)
                            if len(data) > end - offset + 1:
                                raise ValueError('Artifact range exceeded its requested size')
                    if len(data) != end - offset + 1:
                        raise httpx.ReadError('Artifact range ended early')
                    break
                except httpx.HTTPError as exc:
                    # A storage URL can expire or a connection can break. Reuse
                    # all committed chunks, refresh the URL, retry this range.
                    url = None
                    if attempt == 11:
                        raise RuntimeError('Artifact range retries exhausted: ' + type(exc).__name__) from None
                    time.sleep(min(2**attempt, 30))
            output.write(data)
            output.flush()
            digest.update(data)
            offset = end + 1
            if offset == size or offset - reported >= 256 * 1024 * 1024:
                print(json.dumps({'artifact_bytes_verified_in_transfer': offset, 'total_bytes': size}), flush=True)
                reported = offset
    if 'sha256:' + digest.hexdigest() != expected_digest:
        raise ValueError('Immutable artifact SHA-256 mismatch; no files were exposed')


def extract_verified(archive_path, destination):
    destination.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        if shutil.disk_usage(destination).free < sum(i.file_size for i in archive.infolist()) + RESERVE:
            raise RuntimeError('Insufficient space to unpack the immutable artifact with its free-space reserve')
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            if (relative.is_absolute() or '..' in relative.parts or '\\' in info.filename
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError('Artifact contains an unsafe member path')
            target = destination.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            # zipfile verifies each member CRC while reading it to EOF.


def download(github, artifact_run, ticket, target):
    if target.exists():
        raise ValueError('Artifact transfer requires an absent destination; original data will not be overwritten')
    artifacts = github.repo('GET', f'/actions/runs/{artifact_run}/artifacts?per_page=100')['artifacts']
    matches = [a for a in artifacts if a['name'] == 'core-damage-' + ticket and not a['expired']]
    if len(matches) != 1 or not (matches[0].get('digest') or '').startswith('sha256:'):
        raise ValueError('Immutable artifact identity/digest is missing or ambiguous')
    artifact = matches[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(target.parent).free < artifact['size_in_bytes'] + RESERVE:
        raise RuntimeError('Insufficient scratch space for a verified artifact transfer')

    def signed_url():
        response = github.client.get(
            f'https://api.github.com/repos/{github.repository}/actions/artifacts/{artifact["id"]}/zip',
            headers={'Authorization': 'Bearer ' + github.access_token()}, follow_redirects=False)
        if response.status_code >= 400:
            response.raise_for_status()
        if response.status_code != 302 or not response.headers.get('location', '').startswith('https://'):
            raise RuntimeError('Artifact download authorization failed: HTTP ' + str(response.status_code))
        return response.headers['location']  # Signed storage URL remains in memory.

    with tempfile.TemporaryDirectory(prefix='campaign-transfer-', dir=target.parent) as temporary:
        scratch = Path(temporary)
        blob = scratch / 'immutable.zip'
        with httpx.Client(timeout=httpx.Timeout(90, connect=30), follow_redirects=True) as transport:
            transfer(transport, signed_url, artifact['size_in_bytes'], artifact['digest'], blob)
        staging = scratch / 'verified'
        extract_verified(blob, staging)
        proof = {k: artifact[k] for k in ('id', 'name', 'size_in_bytes', 'digest')}
        proof['source_workflow_run_id'] = artifact_run
        (staging / 'artifact-retrieval.json').write_text(json.dumps(proof, indent=2), encoding='utf-8')
        staging.rename(target)
    return proof


if __name__ == '__main__':
    result = download(Github(token=os.environ['GITHUB_TOKEN']), int(os.environ['ARTIFACT_RUN_ID']),
                      os.environ['TICKET'], Path('/tmp/honeycomb-results'))
    print(json.dumps(result), flush=True)
