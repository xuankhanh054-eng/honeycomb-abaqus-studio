"""Bounded 1-GiB release volumes with immutable names and a final completeness manifest."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from .cloud_github import Github, raise_github_error, wait_for_rate_limit

MAX_PART = 1024**3
ALLOWED = {
    ".json",
    ".csv",
    ".parquet",
    ".pdf",
    ".docx",
    ".png",
    ".gif",
    ".inp",
    ".cae",
    ".odb",
    ".sta",
    ".dat",
    ".msg",
    ".log",
    ".txt",
    ".tar",
}


def checkpoint_paths(root: Path):
    """Only an explicit checkpoint receipt admits native files of arbitrary suffix."""
    receipt = root / 'segment.json'
    if not receipt.is_file():
        return set()
    data = json.loads(receipt.read_text(encoding='utf-8'))
    if data.get('format') != 'directory-v1' or data.get('outcome') != 'checkpoint':
        return set()
    files = data.get('files')
    if not isinstance(files, dict) or not files:
        raise ValueError('Invalid native checkpoint inventory')
    paths = set()
    for name in files:
        path = PurePosixPath(name)
        if not name or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe native checkpoint path')
        source = root / 'state' / name
        if source.is_symlink() or not source.is_file():
            raise ValueError('Missing native checkpoint file: ' + name)
        source.resolve().relative_to((root / 'state').resolve())
        paths.add('state/' + name)
    return paths


def parts(path: Path, temporary: Path, limit=MAX_PART):
    """Keep only one bounded part on disk; hash both each part and the original file."""
    if not 1 <= limit <= MAX_PART:
        raise ValueError("Part size must be between 1 byte and 1 GiB")
    whole = hashlib.sha256()
    offset = 0
    index = 0
    with path.open("rb") as source:
        while True:
            digest = hashlib.sha256()
            count = 0
            chunk_path = temporary / "upload.part"
            with chunk_path.open("wb") as dest:
                while count < limit:
                    chunk = source.read(min(4 * 1024**2, limit - count))
                    if not chunk:
                        break
                    dest.write(chunk)
                    digest.update(chunk)
                    whole.update(chunk)
                    count += len(chunk)
            if not count:
                chunk_path.unlink()
                break
            yield {
                "index": index,
                "offset": offset,
                "bytes": count,
                "sha256": digest.hexdigest(),
                "path": chunk_path,
                "file_sha256_so_far": whole.hexdigest(),
            }
            offset += count
            index += 1
            chunk_path.unlink(missing_ok=True)


class ReleaseArchive:
    def __init__(self, github: Github, release_id: int, limit=MAX_PART, *, tag: str | None = None):
        self.github = github
        self.release_id = release_id
        self.limit = limit
        self.tag = tag
        self._assets = None

    def upload(self, path: Path, name: str):
        # A lost upload response may have left a complete asset on GitHub. Each
        # retry inventories and verifies it before attempting another upload.
        for retry in range(8):
            try:
                uploaded = self._upload(path, name)
                if self.tag:
                    # Draft releases return temporary untagged URLs which stop
                    # working after publication. Persist the reserved tag URL.
                    from .cloud_github import REPOSITORY
                    repository = getattr(self.github, "repository", REPOSITORY)
                    uploaded["url"] = f"https://github.com/{repository}/releases/download/{quote(self.tag, safe='')}/{quote(name, safe='')}"
                return uploaded
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                self._assets = None  # Re-read after uncertain writes; never upload blindly.
                if isinstance(exc, httpx.HTTPStatusError) and retry < 7 and wait_for_rate_limit(exc.response, retry):
                    continue
                transient = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
                if not transient or retry >= 2:
                    raise
                time.sleep(2 ** retry)

    def _upload(self, path: Path, name: str):
        from .actions_log import get_actions_log

        logger = get_actions_log()
        if self._assets is None:
            assets = self.github.repo("GET", f"/releases/{self.release_id}/assets?per_page=100")
            page = 2
            while len(assets) == (page - 1) * 100:
                extra = self.github.repo("GET", f"/releases/{self.release_id}/assets?per_page=100&page={page}")
                assets.extend(extra)
                if len(extra) < 100:
                    break
                page += 1
            self._assets = assets
        assets = self._assets
        with path.open("rb") as source:
            expected = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
        for asset in assets:
            if asset["name"] == name:
                if asset.get("state") == "starter" and not asset.get("digest"):
                    # A GitHub 500/502 can leave an unfinished upload occupying
                    # this immutable name. Re-read it before cleanup: a response
                    # may have been lost after the upload actually completed.
                    current = self.github.repo("GET", f"/releases/assets/{asset['id']}", allow_missing=True)
                    if current is not None and (current.get("id") != asset["id"] or current.get("name") != name):
                        raise ValueError("Release asset identity changed during upload recovery")
                    if current is None or (current.get("state") == "starter" and not current.get("digest")):
                        if current is not None:
                            self.github.repo("DELETE", f"/releases/assets/{asset['id']}", allow_missing=True)
                        assets[:] = [a for a in assets if a["id"] != asset["id"]]
                        if logger:
                            logger.emit("RETRY", f"{name} | Đã dọn upload starter chưa hoàn tất; tải lại và kiểm tra SHA-256")
                        break
                    assets[assets.index(asset)] = current
                    asset = current
                if asset.get("digest") != expected or asset["size"] != path.stat().st_size:
                    raise ValueError("An existing release asset failed digest verification")
                if logger and asset["size"] >= 1024**2:
                    logger.emit("CACHE", f"{name} | Đã có trên GitHub, SHA-256/kích thước khớp; không upload lại")
                return {
                    "url": asset["browser_download_url"],
                    "digest": expected,
                    "bytes": asset["size"],
                    "asset_id": asset["id"],
                }
        if len(assets) >= 1000:
            raise ValueError(
                "Release asset limit reached; preserve incomplete manifest and use another release"
            )
        headers = {
            "Authorization": "Bearer " + str(self.github.access_token()),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(path.stat().st_size),
            "Accept": "application/vnd.github+json",
        }
        from .cloud_github import REPOSITORY

        transfer = logger.transfer("Tải lên " + getattr(self, "_upload_label", name), path.stat().st_size) if logger else None
        def chunks(stream):
            for block in iter(lambda: stream.read(4 * 1024**2), b""):
                if transfer:
                    transfer.advance(len(block))
                yield block

        try:
            with path.open("rb") as stream:
                response = self.github.client.post(
                    f"https://uploads.github.com/repos/{getattr(self.github, 'repository', REPOSITORY)}/releases/{self.release_id}/assets",
                    params={"name": name},
                    headers=headers,
                    content=chunks(stream) if transfer else stream,
                    timeout=300,
                )
            raise_github_error(response)
            asset = response.json()
            if asset.get("digest") != expected or asset["size"] != path.stat().st_size:
                raise ValueError("GitHub did not confirm the uploaded asset SHA-256 and size")
            if transfer:
                transfer.verified()
            self._assets.append(asset)
        except Exception as exc:
            if transfer:
                transfer.failed(exc)
            raise
        return {
            "url": asset["browser_download_url"],
            "digest": expected,
            "bytes": asset["size"],
            "asset_id": asset["id"],
        }

    def publish(self, root: Path, *, attempt: str, complete: bool, checkpoint: str | None = None):
        from .cloud_publication import prepare

        # Same filesystem: large solver binaries are hard-linked, never copied.
        # Thousands of tiny reduction records become one lossless tar volume.
        with tempfile.TemporaryDirectory(prefix="honeycomb-publication-", dir=root.resolve().parent) as temp:
            prepared = Path(temp)
            prepare(root.resolve(), prepared)
            return self._publish(prepared, attempt=attempt, complete=complete, checkpoint=checkpoint)

    def _publish(self, root: Path, *, attempt: str, complete: bool, checkpoint: str | None = None):
        files: list[dict[str, Any]] = []
        root = root.resolve()
        native_paths = checkpoint_paths(root)
        native_records = (json.loads((root / 'segment.json').read_text())['files'] if native_paths else {})
        with tempfile.TemporaryDirectory(prefix="honeycomb-upload-") as temp:
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root)
                native = relative.as_posix() in native_paths
                if path.is_symlink() or not path.is_file() or (path.suffix.lower() not in ALLOWED and not native):
                    continue
                if not native and any(
                    p in ("cache", "spool", "credentials", "backup-manifests", ".git") for p in relative.parts
                ):
                    continue
                if path.name in ("session.json", "engine-settings.json"):
                    continue
                before = path.stat()
                record = {
                    "path": relative.as_posix(),
                    "bytes": before.st_size,
                    "parts": [],
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
                for part in parts(path, Path(temp), self.limit):
                    self._upload_label = f"{relative.as_posix()} · part {part['index'] + 1}"
                    uploaded = self.upload(part["path"], f"{attempt}-{part['sha256']}.part")
                    record["parts"].append(
                        {k: part[k] for k in ("index", "offset", "bytes", "sha256")} | uploaded
                    )
                    record["sha256"] = part["file_sha256_so_far"]
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Source changed during archival; no complete manifest will be published")
                if native:
                    expected = native_records[relative.as_posix().removeprefix('state/')]
                    if any(record[key] != expected.get(key) for key in ('bytes', 'sha256')):
                        raise ValueError('Native checkpoint changed after sealing')
                files.append(record)
            if native_paths - {record['path'] for record in files}:
                raise ValueError('Native checkpoint publication is incomplete')
            streamed = root / "stream-index.json"
            if streamed.exists():
                from .cloud_restore import validate_manifest

                inventory = json.loads(streamed.read_text(encoding="utf-8"))
                validate_manifest(inventory, allow_incomplete=True)
                names = {f["path"].casefold() for f in files}
                for entry in inventory["files"]:
                    if entry["path"].casefold() in names:
                        raise ValueError("Duplicate local/remote archive path")
                    names.add(entry["path"].casefold())
                    files.append(entry)
            manifest = {
                "schema_version": "1",
                "kind": "honeycomb-release-archive",
                "attempt": attempt,
                "complete": complete,
                "files": files,
                "part_limit_bytes": self.limit,
            }
            path = Path(temp) / "artifact-manifest.json"
            path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True), encoding="utf-8")
            suffix = f"checkpoint-{checkpoint}-" if checkpoint else ""
            self._upload_label = "artifact-manifest.json"
            self.upload(path, f"{attempt}-{suffix}artifact-manifest.json")
            from .actions_log import get_actions_log

            logger = get_actions_log()
            if logger:
                logger.emit("ARCHIVE", f"Manifest đã xuất | {len(files)} tệp logic | complete={complete} | checksum từng part đã xác minh")
        return manifest
