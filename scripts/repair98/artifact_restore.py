"""Stream a SHA-256 pinned ZIP into selective verified extraction."""

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import httpx
from stream_unzip import stream_unzip


def stream_extract(chunks, selected, destination, expected_digest, expected_bytes):
    destination.mkdir(parents=True, exist_ok=False)
    names = set()
    wanted = {}
    for member in selected:
        path = Path(member["path"])
        if path.is_absolute() or ".." in path.parts or "\\" in member["path"] or ":" in member["path"]:
            raise ValueError("Unsafe source artifact path")
        if path.name in names:
            raise ValueError("Duplicate restart basename")
        names.add(path.name)
        wanted[member["path"]] = member
    digest, count = hashlib.sha256(), 0

    def counted():
        nonlocal count
        for chunk in chunks:
            count += len(chunk)
            if count > expected_bytes + 1024**2:
                raise ValueError("Artifact exceeded pinned size")
            digest.update(chunk)
            if chunk:
                yield chunk

    source = counted()
    for encoded, _size, blocks in stream_unzip(source):
        name = encoded.decode("utf-8")
        if name not in wanted:
            for _block in blocks:
                pass
            continue
        member = wanted[name]
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with target.open("xb") as stream:
            for block in blocks:
                written += len(block)
                if written > member["bytes"]:
                    raise ValueError("Native member exceeded pinned size")
                stream.write(block)
    for _chunk in source:
        pass
    if "sha256:" + digest.hexdigest() != expected_digest:
        raise ValueError("Artifact SHA-256 verification failed")
    found = []
    for member in selected:
        path = destination / member["path"]
        if not path.is_file() or path.is_symlink() or path.stat().st_size != member["bytes"]:
            raise ValueError("Restart member missing or changed size")
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        found.append({"path": member["path"], "bytes": path.stat().st_size, "sha256": checksum})
    return {
        "artifact_sha256_verified": expected_digest,
        "stream_bytes": count,
        "files": found,
        "whole_zip_stored_on_disk": False,
    }


def restore_artifact(plan, root, github):
    source = plan["restart_source"]
    artifact = github.repo("GET", f"/actions/artifacts/{source['artifact_id']}")
    if artifact["expired"] or artifact.get("digest") != source["artifact_digest"]:
        raise ValueError("Pinned native artifact expired or changed")
    if shutil.disk_usage(root).free < source["selected_unpacked_bytes"] + 20 * 10**9:
        raise ValueError("Insufficient disk for selected native state and 20 GB reserve")
    stage = root / "verified-source"
    for attempt in range(3):
        if stage.exists():
            assert stage.resolve().is_relative_to(root.resolve()) and stage.name == "verified-source"
            shutil.rmtree(stage)
        try:
            url = f"https://api.github.com/repos/{github.repository}/actions/artifacts/{source['artifact_id']}/zip"
            with (
                httpx.Client(timeout=120, follow_redirects=True) as client,
                client.stream("GET", url, headers={"Authorization": "Bearer " + github.token}) as response,
            ):
                if response.status_code != 200:
                    raise ValueError("Native artifact download failed: " + str(response.status_code))
                receipt = stream_extract(
                    response.iter_bytes(4 * 1024**2),
                    source["members"],
                    stage,
                    source["artifact_digest"],
                    source["artifact_bytes"],
                )
            break
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    raw = root / "raw"
    raw.mkdir()
    for member in source["members"]:
        path = stage / member["path"]
        os.link(path, raw / path.name)
    (root / "artifact-source-verification.json").write_text(json.dumps(receipt, indent=2))
    return raw
