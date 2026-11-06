import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from honeycomb_studio.cloud_github import Catalog


@pytest.mark.parametrize('problem', [None, 'different_sha', 'wrong_size'])
def test_large_catalog_reads_the_exact_immutable_blob_after_contents_omits_payload(problem):
    document = {'designs': [{'summary': 'field data' * 110000}]}
    payload = json.dumps(document).encode()
    sha = hashlib.sha1(b'blob ' + str(len(payload)).encode() + b'\0' + payload).hexdigest()
    requests = []

    def request(method, path, **kwargs):
        requests.append((method, path))
        if path.startswith('/contents/'):
            return {'sha': sha, 'size': len(payload), 'encoding': 'none', 'content': ''}
        assert path == '/git/blobs/' + sha
        return {'sha': 'a' * 40 if problem == 'different_sha' else sha, 'encoding': 'base64',
                'content': base64.b64encode(payload[:-1] if problem == 'wrong_size' else payload).decode()}

    catalog = Catalog(SimpleNamespace(repo=request))
    if problem:
        with pytest.raises(ValueError, match='blob'):
            catalog.get('campaigns/core-damage/owners/example')
    else:
        loaded, revision = catalog.get('campaigns/core-damage/owners/example')
        assert loaded == document and revision == sha
        assert len(requests) == 2


def test_small_catalog_documents_still_use_one_contents_request():
    calls = []
    def request(method, path, **kwargs):
        calls.append(path)
        return {'sha': 'a' * 40, 'content': base64.b64encode(b'{"status":"ready"}').decode()}
    assert Catalog(SimpleNamespace(repo=request)).get('example') == ({'status': 'ready'}, 'a' * 40)
    assert len(calls) == 1
