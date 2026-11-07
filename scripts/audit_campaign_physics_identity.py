"""Read frozen inputs and report their identities on the native worker platform.

This audit starts no FE solver and changes no campaign design or execution state.
"""
import json
import os
import platform
import subprocess

from honeycomb_studio.campaign import model_hashes, one_run_spec
from honeycomb_studio.campaign_queue import CampaignStore
from honeycomb_studio.cloud_github import Catalog, Github
from honeycomb_studio.models import BatchSpec, digest


def audit(manifest):
    rows = []
    for design in manifest['designs'] + manifest['pilot_designs']:
        model = BatchSpec.model_validate(one_run_spec(design)).model
        assert digest(model.model_dump()) == digest(design['model'])
        computed = model_hashes(model)
        row = {'id': design['id'], 'model_sha256': digest(design['model']),
               'registered': {key: design[key] for key in computed}, 'computed': computed}
        if computed['physics_hash'] != design['physics_hash']:
            row['resolved'] = model.resolved()
        rows.append(row)
    return rows


def main():
    github = Github(token=os.environ['GITHUB_TOKEN'])
    manifest = CampaignStore(github).load(os.environ['CAMPAIGN_ID'])
    rows = audit(manifest)
    report = {'campaign_id': manifest['id'], 'platform': platform.platform(),
              'python': platform.python_version(), 'workflow_run_id': int(os.environ['GITHUB_RUN_ID']),
              'source_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
              'models_blob': subprocess.check_output(
                  ['git', 'rev-parse', 'HEAD:src/honeycomb_studio/models.py'], text=True).strip(),
              'rows': rows, 'mismatches': sum('resolved' in row for row in rows),
              'campaign_mutated': False, 'solver_started': False}
    Catalog(github).update('campaign-physics-portability/' + manifest['id'] + '/latest', lambda _old: report)
    print(json.dumps({key: value for key, value in report.items() if key != 'rows'}, indent=2))


if __name__ == '__main__':
    main()
