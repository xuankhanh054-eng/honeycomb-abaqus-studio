"""Portable verified download; generated copies require only Python's standard library."""
import hashlib
import json
import os
import re
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

MANIFEST = json.loads("{}")  # replaced in generated download_full.py
REPOSITORY = 'xuankhanh24/honeycomb-abaqus-studio'  # replaced in generated download_full.py


def destination(relative):
    root = Path.cwd().resolve()
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or '\\' in relative
            or any(p in ('.', '..') or ':' in p or p != p.rstrip(' .')
                   or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?', p)
                   for p in relative.split('/'))):
        raise ValueError('Unsafe download path')
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root) or any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError('Download target escapes folder or follows symlink')
    return target


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate():
    destination(MANIFEST['folder'])
    destination(MANIFEST['readable_prefix'])
    if not MANIFEST['readable_prefix'].startswith(MANIFEST['folder'] + '/00_dataset_versions/'):
        raise ValueError('Unsafe readable snapshot path')
    if len(MANIFEST['files']) > 100000:
        raise ValueError('Too many files')
    seen = set()
    for file in MANIFEST['files']:
        destination(file['path'])
        if not file['path'].startswith(MANIFEST['folder'] + '/'):
            raise ValueError('File outside job folder')
        key = file['path'].casefold()
        if key in seen:
            raise ValueError('Duplicate file path')
        seen.add(key)
        if type(file['bytes']) is not int or file['bytes'] < 0 or not re.fullmatch('[0-9a-f]{64}', file['sha256']):
            raise ValueError('Invalid file size/checksum')
        offset = 0
        for index, part in enumerate(file['parts']):
            url = urlsplit(part['url'])
            if (url.scheme != 'https' or url.netloc != 'github.com' or url.query or url.fragment
                    or not url.path.startswith('/' + REPOSITORY + '/releases/download/')
                    or any(p in ('.', '..') for p in url.path.split('/'))):
                raise ValueError('Unexpected download source')
            if (part['index'] != index or part['offset'] != offset or type(part['bytes']) is not int
                    or not 0 < part['bytes'] <= 1024**3 or not re.fullmatch('[0-9a-f]{64}', part['sha256'])
                    or part.get('digest') != 'sha256:' + part['sha256']):
                raise ValueError('Invalid part inventory')
            offset += part['bytes']
        if offset != file['bytes']:
            raise ValueError('Part sizes do not match file')


def download(file):
    target = destination(file['path'])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size != file['bytes'] or digest_file(target) != file['sha256']:
            raise ValueError('Existing file differs: ' + file['path'])
        print('Reuse verified:', file['path'], flush=True)
        return target
    partial = target.with_name('.' + target.name + '.' + file['sha256'][:12] + '.partial')
    destination(partial.relative_to(Path.cwd().resolve()).as_posix())
    with partial.open('r+b' if partial.exists() else 'w+b') as output:
        for part in file['parts']:
            output.seek(part['offset'])
            digest, count = hashlib.sha256(), 0
            while count < part['bytes']:
                block = output.read(min(1024**2, part['bytes'] - count))
                if not block:
                    break
                digest.update(block)
                count += len(block)
            if count == part['bytes'] and digest.hexdigest() == part['sha256']:
                continue
            for attempt in range(3):
                output.seek(part['offset'])
                output.truncate()
                digest, count = hashlib.sha256(), 0
                started = time.monotonic()
                try:
                    with urllib.request.urlopen(part['url'], timeout=180) as response:
                        while block := response.read(1024**2):
                            count += len(block)
                            if count > part['bytes']:
                                raise ValueError('Part exceeds declared size')
                            output.write(block)
                            digest.update(block)
                    if count != part['bytes'] or digest.hexdigest() != part['sha256']:
                        raise ValueError('Part checksum/size mismatch')
                    output.flush()
                    print('%s: part %d/%d verified, %.1f MiB/s' % (
                        file['path'], part['index'] + 1, len(file['parts']), count / 1048576 / max(.001, time.monotonic() - started)), flush=True)
                    break
                except (OSError, TimeoutError):
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
        output.truncate(file['bytes'])
        output.flush()
    if digest_file(partial) != file['sha256']:
        raise ValueError('Reassembled file checksum mismatch')
    os.link(partial, target)
    partial.unlink()
    return target


def unpack_readable(archive):
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        seen = set()
        for member in members:
            destination(member.filename.rstrip('/'))
            if (not member.filename.startswith(MANIFEST['folder'] + '/')
                    or (member.external_attr >> 16) & 0o170000 == 0o120000
                    or member.filename.casefold() in seen):
                raise ValueError('Unsafe ZIP member')
            seen.add(member.filename.casefold())
        for member in members:
            relative = member.filename.rstrip('/')[len(MANIFEST['folder']) + 1:]
            if not relative:
                continue
            target = destination(MANIFEST['readable_prefix'] + '/' + relative)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists():
                with source.open(member) as stream:
                    digest = hashlib.sha256()
                    while block := stream.read(1024**2):
                        digest.update(block)
                    expected = digest.hexdigest()
                if digest_file(target) != expected:
                    raise ValueError('Existing readable file differs')
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name('.' + target.name + '.extracting')
            destination(partial.relative_to(Path.cwd().resolve()).as_posix())
            with source.open(member) as stream, partial.open('wb') as output:
                while block := stream.read(1024**2):
                    output.write(block)
            os.link(partial, target)
            partial.unlink()


def main():
    validate()
    if not MANIFEST['complete']:
        print('WARNING: source incomplete; retained files only, see audit.')
    for file in MANIFEST['files']:
        target = download(file)
        if file.get('role') == 'readable':
            unpack_readable(target)
    print('Complete download of retained files:', destination(MANIFEST['folder']))


if __name__ == '__main__':
    main()
