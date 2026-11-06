import base64
import hashlib
import io
import json
import runpy
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from honeycomb_studio.cloud_api import create_app
from honeycomb_studio.cloud_github import REPOSITORY, Github
from honeycomb_studio.cloud_solver_download import (
    download_package,
    download_script,
    selected_files,
    solver_choices,
)

IDENTITY = "a" * 32
SOURCE = "b" * 32


def fixture():
    files = []
    bodies = {}
    for run in (1, 2):
        for extension in ("cae", "odb", "inp"):
            data = f"run {run} {extension}".encode()
            sha = hashlib.sha256(data).hexdigest()
            path = f"job/04_solver_archive/run-{run:04d}/source-{SOURCE}/attempts/001/raw/model{run}.{extension}"
            url = f"https://github.com/{REPOSITORY}/releases/download/test/{run}-{extension}"
            files.append({"path": path, "bytes": len(data), "sha256": sha,
                          "parts": [{"index": 0, "offset": 0, "bytes": len(data), "sha256": sha,
                                     "digest": "sha256:" + sha, "url": url}]})
            bodies[url] = data
    check = deepcopy(files[1])
    check["path"] = check["path"].replace("model1.odb", "model1_check.odb")
    files.append(check)
    manifest = {"schema_version": "1", "kind": "honeycomb-release-archive", "complete": True, "files": files}
    collection = {"files": [{k: v for k, v in f.items() if k != "parts"} for f in files],
                  "full_manifest_url": f"https://github.com/{REPOSITORY}/releases/download/test/full-source-manifest.json"}
    job = {"name": "Delta", "status": "completed", "spec": {"axes": [{"parameter": "reentrant_delta_deg"}]},
           "shards": [{"id": SOURCE, "spec": {"model": {"reentrant_delta_deg": 36}}}]}
    return manifest, collection, job, bodies


def test_download_contains_only_selected_run_and_types(tmp_path, monkeypatch):
    manifest, collection, job, bodies = fixture()
    choices = solver_choices(collection, job)
    assert len(choices) == 2  # Auxiliary _check ODB is not a separate physical run.
    assert choices[0]["parameters"] == [{"name": "reentrant_delta_deg", "value": 36}]
    choice, files = selected_files(collection, job, choices[1]["id"], "cae,inp")
    package = download_package(manifest, choice, files)
    script = tmp_path / "download.py"
    script.write_text(download_script(package), encoding="utf-8")
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return io.BytesIO(bodies[url])

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("urllib.request.urlopen", fetch)
    runpy.run_path(str(script), run_name="__main__")
    assert {url.rsplit("/", 1)[1] for url in calls} == {"2-cae", "2-inp"}
    assert sorted(p.suffix for p in (tmp_path / package["folder"]).iterdir()) == [".cae", ".inp"]
    calls.clear()
    runpy.run_path(str(script), run_name="__main__")
    assert calls == []  # Running again reuses verified files.


def test_attempts_remain_separate_and_mismatched_catalog_is_rejected():
    manifest, collection, job, _ = fixture()
    newer = deepcopy(collection["files"][0])
    newer["path"] = newer["path"].replace("attempts/001", "attempts/002")
    collection["files"].append(newer)
    choices = solver_choices(collection, job)
    assert len(choices) == 3
    assert {c["attempt"] for c in choices} == {1, 2}
    choice, files = selected_files(collection, job, choices[0]["id"], "cae")
    files[0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="khớp"):
        download_package(manifest, choice, files)


def test_parameters_are_not_guessed_from_ambiguous_run_indices():
    _, collection, job, _ = fixture()
    job["shards"] = []
    job["runs"] = [{"index": 0, "resolved": {"reentrant_delta_deg": 25}},
                   {"index": 1, "resolved": {"reentrant_delta_deg": 26}}]
    assert all(choice["parameters"] == [] for choice in solver_choices(collection, job))


def test_public_api_builds_one_helper_and_refuses_unrelated_selections():
    manifest, collection, job, _ = fixture()
    content = json.dumps(manifest).encode()
    downloads = []

    def transport(request):
        path = request.url.path
        if "/contents/catalog/" in path:
            value = collection if "/training/jobs/" in path else job
            return httpx.Response(200, json={"content": base64.b64encode(json.dumps(value).encode()).decode(), "sha": "x"})
        if path.endswith("/releases/tags/test"):
            return httpx.Response(200, json={"assets": [{"id": 7, "name": "full-source-manifest.json", "size": len(content),
                                                       "digest": "sha256:" + hashlib.sha256(content).hexdigest()}]})
        if path.endswith("/releases/assets/7"):
            downloads.append(path)
            return httpx.Response(200, content=content)
        pytest.fail("Unexpected upstream request: " + str(request.url))

    github = Github(client=httpx.Client(transport=httpx.MockTransport(transport)))
    with TestClient(create_app(github)) as client:
        info = client.get(f"/api/jobs/{IDENTITY}/data").json()
        selection = info["solver_runs"][1]["id"]
        url = f"/api/jobs/{IDENTITY}/data/solver-download"
        for params in ({"selection": "wrong"}, {"selection": selection, "types": "exe"}):
            assert client.get(url, params=params).status_code == 422
        assert downloads == []
        response = client.get(url, params={"selection": selection, "types": "odb"})
        assert response.status_code == 200
        assert 'filename="download_run_0002.py"' in response.headers["content-disposition"]
        compile(response.text, "helper.py", "exec")
        response = client.get(url, params={"selection": selection, "types": "cae,inp", "format": "manifest"})
        assert response.status_code == 200
        assert len(response.json()["files"]) == 2
        assert all("model2." in f["path"] for f in response.json()["files"])
