"""Publish solver evidence and verify a checkpoint restored on a different Actions runner."""
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from continuation_bundle import pack
from continuation_bundle import restore as restore_bundle

from honeycomb_studio.cloud_archive import ReleaseArchive
from honeycomb_studio.cloud_github import Catalog, Github
from honeycomb_studio.cloud_restore import restore
from honeycomb_studio.cloud_runtime import execution_runtime
from honeycomb_studio.models import digest
from honeycomb_studio.store import write_json

PROBE = Path('/tmp/honeycomb-recover-probe')


def identity():
    sha = subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
    runtime, errors = execution_runtime()
    if errors:
        raise ValueError('Pinned runtime is unavailable')
    return {'source_sha': sha, 'runtime_hash': digest(runtime), 'workflow_run_id': int(os.environ['GITHUB_RUN_ID']),
            'workflow_run_attempt': int(os.environ['GITHUB_RUN_ATTEMPT'])}


def publish_checkpoint(github, catalog, evidence):
    material = json.loads(Path('output/core-damage-material-validation.json').read_text())
    if material.get('passed') is not True:
        raise ValueError('Material checks failed')
    with tempfile.TemporaryDirectory(prefix='validation-publication-') as temp:
        directory = Path(temp)
        pack(directory, 'checkpoint', root=PROBE)
        write_json(directory / 'material.json', material)
        tag = f"core-damage-validation-{evidence['workflow_run_id']}-{evidence['workflow_run_attempt']}"
        release = github.repo('POST', '/releases', {'tag_name': tag, 'target_commitish': evidence['source_sha'],
                              'name': 'Core damage · cross-runner qualification', 'prerelease': True})
        archive = ReleaseArchive(github, release['id'], tag=tag).publish(directory, attempt='checkpoint', complete=False)
    catalog.update('campaign-validation/pending/' + str(evidence['workflow_run_id']),
                   lambda old: {**evidence, 'archive': archive, 'archive_hash': digest(archive),
                                'material': material, 'producer_job': os.environ['GITHUB_JOB']})


def restore_checkpoint(catalog, evidence):
    pending, _ = catalog.get('campaign-validation/pending/' + str(evidence['workflow_run_id']))
    if (not pending or any(pending.get(k) != evidence[k] for k in evidence)
            or pending['producer_job'] == os.environ['GITHUB_JOB']):
        raise ValueError('Cross-runner validation identity differs')
    if digest(pending['archive']) != pending['archive_hash'] or PROBE.exists():
        raise ValueError('Invalid checkpoint archive or nonempty restore destination')
    with tempfile.TemporaryDirectory(prefix='validation-download-') as temp:
        directory = Path(temp)
        verification = restore(pending['archive'], directory, allow_incomplete=True)
        restore_bundle(directory, 1, root=PROBE)
    write_json(Path('output/campaign-archive-restore.json'), {'passed': True, 'verification': verification,
               'archive_hash': pending['archive_hash'], 'cross_runner': True})


def publish_validation(catalog, evidence):
    pending, _ = catalog.get('campaign-validation/pending/' + str(evidence['workflow_run_id']))
    restart = json.loads(Path('output/explicit-recover-verification.json').read_text())
    archive = json.loads(Path('output/campaign-archive-restore.json').read_text())
    if (not pending or pending['source_sha'] != evidence['source_sha']
            or not all(report.get('passed') is True for report in (pending['material'], restart, archive))
            or restart.get('cross_runner') is not True or restart.get('damage_state_verified') is not True):
        raise ValueError('Qualification is incomplete')
    catalog.update('campaign-validation/latest', lambda old: {**evidence, 'cross_runner': True,
                   'material': pending['material'], 'restart': restart, 'archive_restore': archive})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('publish-checkpoint', 'restore', 'publish-validation', 'infrastructure'))
    args = parser.parse_args()
    github = Github(token=os.environ['GITHUB_TOKEN'])
    catalog = Catalog(github)
    evidence = identity()
    if args.mode == 'publish-checkpoint':
        publish_checkpoint(github, catalog, evidence)
    elif args.mode == 'restore':
        restore_checkpoint(catalog, evidence)
    elif args.mode == 'publish-validation':
        publish_validation(catalog, evidence)
    else:
        material = json.loads(Path('output/core-damage-material-validation.json').read_text())
        if material.get('passed') is not True:
            raise ValueError('Infrastructure checkout/ODB validation failed')
        catalog.update('campaign-infrastructure/latest', lambda old: {**evidence, 'passed': True,
                       'license_checkout_observed': True, 'runtime_installation_verified': True})


if __name__ == '__main__':
    main()
