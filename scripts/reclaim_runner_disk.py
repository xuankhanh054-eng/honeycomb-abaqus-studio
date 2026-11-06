"""Reclaim unused SDKs on an ephemeral GitHub-hosted Linux runner."""
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    if sys.platform != 'linux' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Disk reclamation requires an ephemeral GitHub-hosted Linux runner')
    protected = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), Path.cwd().resolve()]
    for executable in ('uv', 'python', 'python3', 'node', 'docker'):
        if found := shutil.which(executable):
            protected.append(Path(found).resolve())
    candidates = [*Path('/opt/hostedtoolcache').glob('*'), *Path('/usr/lib').glob('llvm-*')]
    candidates.extend(Path(p) for p in ('/usr/share/swift', '/usr/local/julia', '/usr/local/share/vcpkg',
                                       '/usr/local/share/powershell', '/usr/local/lib/R'))
    allowed = [Path(p) for p in ('/opt/hostedtoolcache', '/usr/lib', '/usr/share', '/usr/local')]
    for path in candidates:
        if not path.is_dir() or path.is_symlink():
            continue
        resolved = path.resolve()
        if not any(resolved.is_relative_to(base) and resolved != base for base in allowed):
            raise ValueError('SDK target is outside the allowed roots')
        if any(item.is_relative_to(resolved) for item in protected):
            print('Preserving active runtime:', resolved, flush=True)
            continue
        print('Reclaiming unused SDK:', resolved, flush=True)
        subprocess.run(['sudo', 'rm', '-rf', '--one-file-system', '--', str(resolved)], check=True)
    print('Free GB after SDK reclamation:', shutil.disk_usage('/tmp').free / 1e9, flush=True)


if __name__ == '__main__':
    main()
