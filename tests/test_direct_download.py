import hashlib
import io
import runpy
from copy import deepcopy

import pytest

from honeycomb_studio.cloud_github import REPOSITORY
from honeycomb_studio.job_data import standalone_downloader


def package():
    chunks = [b"verified first part", b"second part"]
    content = b"".join(chunks)
    offset = 0
    parts = []
    for index, chunk in enumerate(chunks):
        sha = hashlib.sha256(chunk).hexdigest()
        parts.append({"index": index, "offset": offset, "bytes": len(chunk), "sha256": sha,
                      "digest": "sha256:" + sha,
                      "url": f"https://github.com/{REPOSITORY}/releases/download/test/{index}"})
        offset += len(chunk)
    return {"folder": "job", "readable_prefix": "job/00_dataset_versions/test",
            "storage_mode": "direct-source-parts", "complete": True,
            "files": [{"path": "job/04_solver_archive/test.odb", "bytes": len(content),
                       "sha256": hashlib.sha256(content).hexdigest(), "parts": parts}]}, chunks


def test_resume_preserves_verified_parts_and_adds_only_new_files(tmp_path, monkeypatch):
    manifest, chunks = package()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _: None)
    script = tmp_path / "download_full.py"
    script.write_text(standalone_downloader(manifest), encoding="utf-8")
    downloads = []

    def interrupted(url, **kwargs):
        downloads.append(url)
        if url.endswith("/1"):
            raise OSError("network lost")
        return io.BytesIO(chunks[0])

    monkeypatch.setattr("urllib.request.urlopen", interrupted)
    with pytest.raises(OSError):
        runpy.run_path(str(script), run_name="__main__")
    assert not (tmp_path / manifest["files"][0]["path"]).exists()
    downloads.clear()

    def fetch(url, **kwargs):
        downloads.append(url)
        return io.BytesIO(chunks[int(url.rsplit("/", 1)[1])])

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    runpy.run_path(str(script), run_name="__main__")
    assert downloads == [manifest["files"][0]["parts"][1]["url"]]
    assert (tmp_path / manifest["files"][0]["path"]).read_bytes() == b"".join(chunks)
    downloads.clear()
    extra = deepcopy(manifest["files"][0])
    extra["path"] = "job/04_solver_archive/new-attempt.odb"
    manifest["files"].append(extra)
    script.write_text(standalone_downloader(manifest), encoding="utf-8")
    runpy.run_path(str(script), run_name="__main__")
    assert len(downloads) == 2  # Only the newly added attempt.


@pytest.mark.parametrize("change", ["path", "url", "other_repository", "offset", "duplicate"])
def test_invalid_inventory_fails_before_network(tmp_path, monkeypatch, change):
    manifest, _ = package()
    if change == "path":
        manifest["files"][0]["path"] = "../escape.odb"
    elif change == "url":
        manifest["files"][0]["parts"][0]["url"] = "https://example.com/payload"
    elif change == 'other_repository':
        manifest['files'][0]['parts'][0]['url'] = 'https://github.com/unowned/repository/releases/download/test/0'
    elif change == "offset":
        manifest["files"][0]["parts"][1]["offset"] += 1
    else:
        manifest["files"].append(deepcopy(manifest["files"][0]))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: pytest.fail("Network before validation"))
    script = tmp_path / "download_full.py"
    script.write_text(standalone_downloader(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        runpy.run_path(str(script), run_name="__main__")
