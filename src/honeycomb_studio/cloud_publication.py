"""Bundle tiny field-reduction files, hard-link large outputs without duplicating ODB."""

import os
import tarfile
from pathlib import Path


def prepare(source: Path, target: Path):
    # Lazy import avoids a cycle when ReleaseArchive prepares its own publication view.
    from .cloud_archive import ALLOWED, checkpoint_paths

    native_paths = checkpoint_paths(source)
    has_reductions = any(source.glob("**/reduced-fields/*.json")) or any(source.glob("**/remote-fields/*.json"))
    reductions = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if relative.as_posix() in native_paths:
            if path.is_symlink() or not path.is_file():
                raise ValueError('Invalid native checkpoint file')
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(path, destination)
            continue
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in ALLOWED:
            continue
        if path.suffix.lower() == ".tar" and path.name not in ("field-reductions.tar", "state.tar"):
            continue
        if relative.as_posix() == "RESTORE-README.txt" or (
            has_reductions and relative.as_posix() == "field-reductions.tar"
        ):
            continue
        if any(p in ("cache", "spool", "credentials", "backup-manifests", ".git", ".cloud-stream") for p in relative.parts):
            continue
        if path.name in ("session.json", "engine-settings.json", "publication-pending.json", "publication-result.json"):
            continue
        if "reduced-fields" in relative.parts or "remote-fields" in relative.parts:
            reductions.append((path, relative))
            continue
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.link(path, dest)
    if reductions:
        with tarfile.open(target / "field-reductions.tar", "w") as bundle:
            for path, relative in reductions:
                bundle.add(path, arcname=relative.as_posix(), recursive=False)
    (target / "RESTORE-README.txt").write_text(
        "Restore using: uv run python scripts/restore_cloud_archive.py "
        "artifact-manifest.json restored-results --unpack-fields. The flag safely "
        "unpacks field-reductions.tar and streamed-fields/*.tar, verifies original "
        "Parquet checksums and links the immutable payloads back to their original "
        "field paths, including historical extraction generations. Metadata alone "
        "does not replace the full-field volumes. Original CAE/INP/ODB and numerical "
        "exports are unchanged. Existing different files are never overwritten. "
        "Attempt artifact-manifest.json files "
        "refer to the fully restored tree. Research quality remains separate from "
        "transport completeness.\n"
    )
