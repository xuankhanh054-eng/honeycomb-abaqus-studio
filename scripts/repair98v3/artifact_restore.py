"""Extract pinned ZIP members sequentially using verified central-directory sizes."""

import hashlib
import json
import os
import shutil
import struct
import time
import zlib
from pathlib import Path

import httpx


class ForwardReader:
    def __init__(self, chunks, expected_bytes):
        self.source = iter(chunks)
        self.buffer = memoryview(b"")
        self.position = 0
        self.count = 0
        self.expected_bytes = expected_bytes
        self.digest = hashlib.sha256()

    def fill(self):
        while not self.buffer:
            try:
                chunk = next(self.source)
            except StopIteration:
                raise ValueError("Truncated pinned artifact") from None
            self.count += len(chunk)
            if self.count > self.expected_bytes:
                raise ValueError("Artifact exceeded pinned size")
            self.digest.update(chunk)
            self.buffer = memoryview(chunk)

    def take(self, size):
        while size:
            self.fill()
            length = min(size, len(self.buffer))
            block = self.buffer[:length]
            self.buffer = self.buffer[length:]
            self.position += length
            size -= length
            yield block

    def read(self, size):
        return b"".join(self.take(size))

    def skip_to(self, offset):
        if offset < self.position:
            raise ValueError("Overlapping pinned ZIP members")
        for _block in self.take(offset - self.position):
            pass

    def finish(self):
        self.skip_to(self.expected_bytes)
        for chunk in self.source:
            self.count += len(chunk)
            self.digest.update(chunk)
        if self.count != self.expected_bytes:
            raise ValueError("Artifact size differs from pin")


def stream_extract(chunks, selected, destination, expected_digest, expected_bytes):
    destination.mkdir(parents=True, exist_ok=False)
    names = set()
    for member in selected:
        path = Path(member["path"])
        if path.is_absolute() or ".." in path.parts or "\\" in member["path"] or ":" in member["path"]:
            raise ValueError("Unsafe source artifact path")
        if path.name in names:
            raise ValueError("Duplicate restart basename")
        names.add(path.name)
    reader = ForwardReader(chunks, expected_bytes)
    found = []
    for member in sorted(selected, key=lambda m: m["header_offset"]):
        reader.skip_to(member["header_offset"])
        header = struct.unpack("<4s5H3I2H", reader.read(30))
        signature, _version, flags, compression, _time, _date, crc, packed, size, nlen, xlen = header
        name = reader.read(nlen).decode("utf-8" if flags & 0x800 else "cp437")
        reader.read(xlen)
        if (
            signature != b"PK\x03\x04"
            or name != member["path"]
            or flags != member["flag_bits"]
            or flags & 1
            or compression != member["compression"]
            or compression not in (0, 8)
        ):
            raise ValueError("Local ZIP header differs from pinned index")
        if not flags & 8 and (
            crc != member["crc32"]
            or packed not in (member["compressed_bytes"], 0xFFFFFFFF)
            or size not in (member["bytes"], 0xFFFFFFFF)
        ):
            raise ValueError("Local ZIP sizes or CRC differ from index")
        target = destination / member["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        actual_crc, written = 0, 0
        file_digest = hashlib.sha256()
        decoder = zlib.decompressobj(-15) if compression == 8 else None
        with target.open("xb") as output:

            def emit(block):
                nonlocal actual_crc, written
                written += len(block)
                if written > member["bytes"]:
                    raise ValueError("Native member exceeded pinned size")
                actual_crc = zlib.crc32(block, actual_crc)
                file_digest.update(block)
                output.write(block)

            for block in reader.take(member["compressed_bytes"]):
                if decoder is None:
                    emit(block)
                else:
                    pending = block
                    while pending:
                        emit(decoder.decompress(pending, 4 * 1024**2))
                        pending = decoder.unconsumed_tail
            if decoder is not None and (not decoder.eof or decoder.unused_data):
                raise ValueError("Invalid compressed ZIP member")
        if written != member["bytes"] or actual_crc != member["crc32"]:
            raise ValueError("Native member size or CRC verification failed")
        found.append(
            {
                "path": member["path"],
                "bytes": written,
                "sha256": file_digest.hexdigest(),
                "crc32_verified": actual_crc,
            }
        )
    reader.finish()
    if "sha256:" + reader.digest.hexdigest() != expected_digest:
        raise ValueError("Artifact SHA-256 verification failed")
    return {
        "artifact_sha256_verified": expected_digest,
        "stream_bytes": reader.count,
        "files": found,
        "whole_zip_stored_on_disk": False,
        "zip_size_source": "pinned_central_directory",
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
        os.link(stage / member["path"], raw / Path(member["path"]).name)
    (root / "artifact-source-verification.json").write_text(json.dumps(receipt, indent=2))
    return raw
