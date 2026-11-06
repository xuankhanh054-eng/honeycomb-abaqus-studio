from copy import deepcopy

import pytest

from honeycomb_studio.cloud_checkpoint import completed_checkpoint
from honeycomb_studio.cloud_github import REPOSITORY


def fixture():
    names = ['run.json', 'solver-complete.json', 'quality.json', 'summary.json',
             'complete.json', 'impact_history.csv', 'model.odb']
    files, assets = [], []
    for i, name in enumerate(names):
        url = f'https://github.com/{REPOSITORY}/releases/download/fixture/{i}'
        part = {'index': 0, 'offset': 0, 'bytes': 1, 'sha256': 'a' * 64,
                'digest': 'sha256:' + 'a' * 64, 'asset_id': i, 'url': url}
        files.append({'path': f'projects/p/batches/b/runs/r/attempts/001/{name}',
                      'bytes': 1, 'sha256': 'a' * 64, 'parts': [part]})
        assets.append({'id': i, 'size': 1, 'digest': part['digest'], 'browser_download_url': url})
    checkpoint = {'kind': 'honeycomb-release-archive', 'schema_version': '1', 'complete': False, 'files': files}
    runs = [{'id': 'r', 'status': 'completed', 'quality': {'solver_completed': True, 'ml_eligible': True}}]
    return checkpoint, assets, runs


def test_complete_wave_recovers_without_rewriting_checkpoint():
    checkpoint, assets, runs = fixture()
    original = deepcopy(checkpoint)
    result = completed_checkpoint(checkpoint, assets, runs)
    assert result['complete'] and result['recovery']['solver_rerun'] is False
    assert checkpoint == original and result['files'] == checkpoint['files']


@pytest.mark.parametrize('damage', ['missing_asset', 'digest', 'unfinished', 'missing_odb'])
def test_incomplete_or_changed_checkpoint_is_never_certified(damage):
    checkpoint, assets, runs = fixture()
    if damage == 'missing_asset':
        assets.pop()
    elif damage == 'digest':
        assets[0]['digest'] = 'sha256:' + 'b' * 64
    elif damage == 'unfinished':
        runs[0]['status'] = 'running'
    else:
        checkpoint['files'].pop()
    with pytest.raises(ValueError):
        completed_checkpoint(checkpoint, assets, runs)
