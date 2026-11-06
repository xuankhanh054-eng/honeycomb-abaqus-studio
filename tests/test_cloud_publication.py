import hashlib
import json
import os
import tarfile

import httpx
import pytest

from honeycomb_studio.cloud_archive import ReleaseArchive
from honeycomb_studio.cloud_github import REPOSITORY, Github
from honeycomb_studio.cloud_publication import prepare


def test_many_reductions_bundle_losslessly_and_large_files_are_not_copied(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "model.odb").write_bytes(b"binary transport fixture")
    (source / "RESTORE-README.txt").write_bytes(b"original source instructions")
    (source / "field-reductions.tar").write_bytes(b"prior source bundle")
    (source / "credentials").mkdir()
    (source / "credentials/oauth.json").write_text("private test fixture")
    records = source / "extracted/reduced-fields"
    records.mkdir(parents=True)
    for i in range(1010):
        (records / f"{i}.json").write_bytes(json.dumps({"value": i}).encode())
    prepare(source, target)
    assert (source / "RESTORE-README.txt").read_bytes() == b"original source instructions"
    assert (source / "field-reductions.tar").read_bytes() == b"prior source bundle"
    assert os.path.samefile(source / "model.odb", target / "model.odb")
    assert not (target / "credentials").exists()
    assert len(list(target.iterdir())) == 3
    with tarfile.open(target / "field-reductions.tar") as bundle:
        assert len(bundle.getmembers()) == 1010
        for member in bundle:
            assert bundle.extractfile(member).read() == (source / member.name).read_bytes()


def test_upload_retries_after_lost_response_and_reuses_verified_asset(tmp_path, monkeypatch):
    payload = b"transport fixture"
    path = tmp_path / "data"
    path.write_bytes(payload)
    calls = []
    asset = {"id": 1, "name": "test.part", "size": len(payload),
             "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
             "browser_download_url": "https://github.com/test"}

    class Client:
        def post(self, *args, **kwargs):
            calls.append("post")
            raise httpx.ReadTimeout("Accepted on server, response lost")

    class Remote:
        client = Client()

        def access_token(self):
            return "test-fixture"

        def repo(self, *args):
            return [asset] if calls else []

    monkeypatch.setattr("honeycomb_studio.cloud_archive.time.sleep", lambda _: None)
    uploaded = ReleaseArchive(Remote(), 1).upload(path, "test.part")
    assert calls == ["post"]
    assert uploaded["asset_id"] == 1
    tagged = ReleaseArchive(Remote(), 1, tag="job-data-stable").upload(path, "test.part")
    assert tagged["url"] == f"https://github.com/{REPOSITORY}/releases/download/job-data-stable/test.part"
    assert tagged["digest"] == uploaded["digest"]
    asset["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="digest"):
        ReleaseArchive(Remote(), 1).upload(path, "test.part")


@pytest.mark.parametrize("existing", [False, True])
def test_upload_recovers_incomplete_starter_left_by_github_502(tmp_path, monkeypatch, existing):
    payload = b"complete original solver result"
    path = tmp_path / "result"
    path.write_bytes(payload)
    # Actual failed campaign assets retained a nonzero reported size, but no
    # digest. GitHub marks these unfinished uploads as starter, not uploaded.
    starter = {"id": 7, "name": "result.part", "state": "starter", "size": 203, "digest": None}
    uploaded = {**starter, "id": 8, "state": "uploaded", "size": len(payload),
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "browser_download_url": "https://github.com/fixture/result.part"}
    assets = [starter] if existing else []
    posts, deleted = [], []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=assets if "/assets?" in str(request.url) else assets[0])
        if request.method == "DELETE":
            assert request.url.path.endswith("/releases/assets/7")
            deleted.append(assets.pop())
            return httpx.Response(204)
        posts.append(request.read())
        if not existing and len(posts) == 1:
            assets.append(starter)
            return httpx.Response(502, json={"message": "Error updating policy"})
        assets.append(uploaded)
        return httpx.Response(201, json=uploaded)

    monkeypatch.setattr("honeycomb_studio.cloud_archive.time.sleep", lambda _: None)
    github = Github(token="fixture", client=httpx.Client(transport=httpx.MockTransport(handle)))
    result = ReleaseArchive(github, 1).upload(path, "result.part")
    assert result["asset_id"] == 8
    assert posts == [payload] * (1 if existing else 2)
    assert deleted == [starter]
    assert assets == [uploaded]


def test_starter_that_completed_before_cleanup_is_verified_and_preserved(tmp_path):
    payload = b"result accepted after a lost response"
    path = tmp_path / "result"
    path.write_bytes(payload)
    starter = {"id": 7, "name": "result.part", "state": "starter", "size": 203, "digest": None}
    uploaded = {**starter, "state": "uploaded", "size": len(payload),
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "browser_download_url": "https://github.com/fixture/result.part"}

    def handle(request):
        assert request.method == "GET"  # Never delete or overwrite a completed upload.
        return httpx.Response(200, json=[starter] if "/assets?" in str(request.url) else uploaded)

    github = Github(token="fixture", client=httpx.Client(transport=httpx.MockTransport(handle)))
    result = ReleaseArchive(github, 1).upload(path, "result.part")
    assert result["asset_id"] == 7
    uploaded["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="digest"):
        ReleaseArchive(github, 1).upload(path, "result.part")


@pytest.mark.parametrize('tamper', [False, True])
def test_native_checkpoint_publication_restores_every_inventory_file(tmp_path, monkeypatch, tamper):
    import importlib.util
    from pathlib import Path

    from honeycomb_studio.cloud_restore import restore

    spec = importlib.util.spec_from_file_location('bundle', Path(__file__).parents[1] / 'scripts/continuation_bundle.py')
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('job.abq.1', 'job.pac', 'job.res', 'job.sel', 'catalog.sqlite', 'job.odb'):
        (source / name).write_bytes(('native-' + name).encode())
    sealed = tmp_path / 'sealed'
    bundle.pack(sealed, 'checkpoint', root=source)
    if tamper:
        (source / 'job.abq.1').write_bytes(b'changed')
    payloads = {}

    def upload(self, path, name):
        url = f'https://github.com/{REPOSITORY}/releases/download/test/' + name
        payloads[url] = path.read_bytes()
        return dict(url=url, bytes=path.stat().st_size, digest='sha256:' + hashlib.sha256(path.read_bytes()).hexdigest())

    monkeypatch.setattr(ReleaseArchive, 'upload', upload)
    publisher = ReleaseArchive(None, 1, limit=11)
    if tamper:
        with pytest.raises(ValueError, match='changed after sealing'):
            publisher.publish(sealed, attempt='test', complete=False)
        assert not any(url.endswith('artifact-manifest.json') for url in payloads)
        return
    manifest = publisher.publish(sealed, attempt='test', complete=False)
    restored = tmp_path / 'downloaded'
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payloads[str(request.url)]))) as client:
        restore(manifest, restored, client=client, allow_incomplete=True)
    destination = tmp_path / 'resumed'
    bundle.restore(restored, 1, root=destination)
    assert {p.name: p.read_bytes() for p in source.iterdir()} == {p.name: p.read_bytes() for p in destination.iterdir()}
