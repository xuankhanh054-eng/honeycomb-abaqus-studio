"""Readable, deterministic names and full source inventories for one web submission."""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy

from .cloud_github import REPOSITORY
from .cloud_restore import validate_manifest


def job_folder(identity, name):
    ascii_name = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.encode("ascii", "ignore").decode().lower()).strip("-")
    return "job--" + (slug[:48].rstrip("-") or "mo-phong") + "--" + identity[:12]


def run_folder(run, index):
    model = run.get("resolved", {})
    labels = []
    for key, label, unit in (("reentrant_delta_deg", "delta", "deg"), ("energy_J", "energy", "J")):
        value = model.get(key)
        if isinstance(value, (int, float)):
            labels.append(f"{label}-{value:.6g}{unit}")
    return f"run-{index + 1:04d}" + ("--" + "--".join(labels) if labels else "") + "--" + run["id"]


def full_manifest(sources):
    """Reference every original part without altering original IDs, bytes or hashes."""
    files = []
    complete = bool(sources)
    for source in sources:
        archive = source.get("archive")
        if archive is None:
            complete = False
            continue
        validate_manifest(archive, allow_incomplete=True)
        complete = complete and archive["complete"] is True
        prefix = job_folder(source["job_id"], source["job_name"]) + "/04_solver_archive/"
        prefix += f"run-{source.get('index', 0) + 1:04d}/"
        # A recovered shard is a new immutable attempt. Context files from an
        # earlier failed attempt must not collide when reusing a local folder.
        prefix += "source-" + str(source.get("shard_id", "original")) + "/"
        for file in archive["files"]:
            parts = file["path"].split("/")
            if "runs" in parts and len(parts) > parts.index("runs") + 2:
                relative = "/".join(parts[parts.index("runs") + 2:])
            else:
                relative = "context/" + file["path"]
            files.append({**deepcopy(file), "path": prefix + relative})
    value = {"schema_version": "1", "kind": "honeycomb-release-archive", "complete": complete,
             "files": files, "note": "Full inventory of original solver files, including ODB/CAE/INP, logs and reports."}
    validate_manifest(value, allow_incomplete=True)
    return value


def standalone_downloader(bundle_manifest):
    """A portable stdlib script downloads, verifies and unpacks the whole job folder."""
    if bundle_manifest.get("storage_mode") == "direct-source-parts":
        from pathlib import Path

        source = Path(__file__).with_name("standalone_download.py").read_text(encoding="utf-8-sig")
        source = source.replace("REPOSITORY = 'xuankhanh24/honeycomb-abaqus-studio'  # replaced in generated download_full.py",
                                'REPOSITORY = ' + repr(REPOSITORY))
        return source.replace('MANIFEST = json.loads("{}")  # replaced in generated download_full.py',
                              "MANIFEST = json.loads(" + repr(json.dumps(bundle_manifest, ensure_ascii=True)) + ")")
    return '''"""Run: py download_full.py. No packages or license required."""
import hashlib
import json
import os
import shutil
import stat
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

MANIFEST = json.loads(__MANIFEST__)

def main():
    entry = MANIFEST["files"][0]
    archive = Path(entry["path"])
    if archive.name != entry["path"]:
        raise ValueError("Unsafe package name")
    if archive.exists():
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                raise ValueError("Existing ZIP differs; use a clean folder")
    else:
        partial = archive.with_suffix(".partial")
        with partial.open("wb") as output:
            total = 0
            for part in entry["parts"]:
                url = part["url"]
                if not url.startswith("https://github.com/__REPOSITORY__/releases/download/"):
                    raise ValueError("Unexpected download source")
                digest = hashlib.sha256()
                count = 0
                with urllib.request.urlopen(url, timeout=180) as response:
                    while block := response.read(1024 * 1024):
                        count += len(block)
                        if count > part["bytes"]:
                            raise ValueError("Part exceeds declared size")
                        output.write(block)
                        digest.update(block)
                        total += len(block)
                        print("Download: %.1f / %.1f MiB" % (total / 1048576, entry["bytes"] / 1048576), end="\\r", flush=True)
                if count != part["bytes"] or digest.hexdigest() != part["sha256"]:
                    raise ValueError("Part checksum/size mismatch")
        with partial.open("rb") as stream:
            if partial.stat().st_size != entry["bytes"] or hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                raise ValueError("ZIP checksum/size mismatch")
        os.replace(partial, archive)
    target = Path(archive.stem).resolve()
    target.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        for member in members:
            name = PurePosixPath(member.filename)
            dest = target.joinpath(*name.parts)
            if (name.is_absolute() or ".." in name.parts or "\\\\" in member.filename
                    or any(":" in part for part in name.parts)
                    or not dest.resolve().is_relative_to(target)
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError("Unsafe ZIP member")
        for member in members:
            dest = target.joinpath(*PurePosixPath(member.filename).parts)
            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as inp, dest.open("xb") as output:
                shutil.copyfileobj(inp, output)
    print("\\nComplete folder:", target)

if __name__ == "__main__":
    main()
'''.replace("__MANIFEST__", repr(json.dumps(bundle_manifest, ensure_ascii=True))).replace('__REPOSITORY__', REPOSITORY)
