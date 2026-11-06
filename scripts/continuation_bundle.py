"""Seal/restore same-workflow continuation artifacts; reject traversal and symlinks."""
import argparse
import hashlib
import json
import os
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path('/tmp/honeycomb-results')


def safe_path(root, name):
    path = Path(name)
    if not name or PurePosixPath(name).is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        raise ValueError('Unsafe continuation member')
    target = root / path
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError('Unsafe continuation member') from None
    if any(parent.is_symlink() for parent in (target, *target.parents) if parent.is_relative_to(root)):
        raise ValueError('Unsafe continuation symlink')
    return target


def file_record(path):
    with path.open('rb') as stream:
        return {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest(), 'bytes': path.stat().st_size}


def pack(folder, outcome, *, root=None):
    if outcome not in ('completed', 'checkpoint'):
        raise ValueError('A failed solver cannot produce a continuation bundle')
    folder.mkdir(parents=True, exist_ok=True)
    receipt = {'outcome': outcome}
    if outcome == 'checkpoint':
        source = (root or ROOT).resolve()
        receipt.update(format='directory-v1', files={})
        for path in sorted(source.rglob('*')):
            relative = path.relative_to(source)
            if path.is_symlink():
                raise ValueError('Symlink in checkpoint')
            if not path.is_file() or 'credentials' in relative.parts or path.name in (
                    'session.json', 'engine-settings.json', 'publication-pending.json', 'publication-result.json'):
                continue
            destination = safe_path(folder / 'state', relative.as_posix())
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Both directories live on the same runner filesystem. A checkpoint
            # retains the exact bytes without duplicating a potentially 100-GB ODB.
            os.link(path, destination)
            receipt['files'][relative.as_posix()] = file_record(path)
    (folder / 'segment.json').write_text(json.dumps(receipt), encoding='utf-8')


def restore(folder, segment, *, root=None):
    root = root or ROOT
    if segment == 0:
        if root.exists() and any(root.iterdir()):
            raise ValueError('Initial segment must be clean')
        return False
    receipt = json.loads((folder / 'segment.json').read_text())
    if receipt['outcome'] == 'completed':
        return True
    if receipt['outcome'] != 'checkpoint' or root.exists():
        raise ValueError('Invalid continuation outcome or nonempty destination')
    if receipt.get('format') == 'directory-v1':
        files = receipt.get('files')
        if not isinstance(files, dict) or not files:
            raise ValueError('Invalid checkpoint inventory')
        checked = []
        for name, record in files.items():
            source = safe_path(folder / 'state', name)
            destination = safe_path(root, name)
            if source.is_symlink() or not source.is_file() or file_record(source) != record:
                raise ValueError('Corrupt continuation file: ' + name)
            checked.append((source, destination))
        # Verify everything before creating a partially restored workspace.
        root.mkdir()
        for source, destination in checked:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, destination)
        return False
    if receipt.get('format') is not None:
        raise ValueError('Unsupported checkpoint format')
    archive = folder / 'state.tar'
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != receipt['sha256'] or archive.stat().st_size != receipt['bytes']:
            raise ValueError('Corrupt continuation archive')
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if not member.isfile() or path.is_absolute() or '..' in path.parts or '\\' in member.name or ':' in member.name:
                raise ValueError('Unsafe continuation member')
        root.mkdir()
        tar.extractall(root, filter='data')
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['restore', 'pack'])
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--segment', type=int)
    parser.add_argument('--outcome')
    args = parser.parse_args()
    if args.mode == 'pack':
        pack(args.folder, args.outcome)
    else:
        done = restore(args.folder, args.segment)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
            out.write('done=' + str(done).lower() + '\n')


if __name__ == '__main__':
    main()
