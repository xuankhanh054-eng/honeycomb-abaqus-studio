"""Select one archived solver attempt without proxying multi-GiB ODB files."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from .cloud_github import REPOSITORY
from .cloud_restore import public_asset, validate_manifest

SOLVER_PATH = re.compile(
    r"^(?P<folder>[^/]+)/04_solver_archive/run-(?P<index>\d+)/"
    r"source-(?P<source>[^/]+)/(?:attempts/(?P<attempt>\d+)/)?raw/(?P<name>[^/]+)$"
)
TYPES = {"cae", "odb", "inp"}


def solver_choices(collection, job):
    groups: dict[str, dict] = {}
    for file in collection.get("files", []):
        match = SOLVER_PATH.fullmatch(file["path"])
        path = PurePosixPath(file["path"])
        kind = path.suffix.lower().lstrip(".")
        if not match or kind not in TYPES or path.stem.endswith("_check"):
            continue
        key = str(path.with_suffix(""))
        if key not in groups:
            index = int(match["index"])
            source = match["source"]
            shard = next((s for s in job.get("shards", []) if s["id"] == source), None)
            if shard is None:
                previous = [p for s in job.get("shards", []) for p in s.get("previous_attempts", [])]
                shard = next((s for s in previous if s["id"] == source), None)
            model = (shard or {}).get("spec", {}).get("model", {})
            parameters = []
            for axis in job.get("spec", {}).get("axes", []):
                value = model
                for name in axis["parameter"].split("."):
                    value = value.get(name) if isinstance(value, dict) else None
                if value is not None:
                    parameters.append({"name": axis["parameter"], "value": value})
            groups[key] = {
                "id": hashlib.sha256(key.encode()).hexdigest()[:24],
                "index": index, "source": source, "attempt": int(match["attempt"] or 1),
                "model_name": path.stem, "parameters": parameters, "files": [],
            }
        groups[key]["files"].append({"path": file["path"], "name": path.name, "type": kind,
                                     "bytes": file["bytes"], "sha256": file["sha256"]})
    return sorted(groups.values(), key=lambda g: (g["index"], g["source"], g["attempt"], g["model_name"]))


def selected_files(collection, job, selection, types):
    requested = set(types.split(","))
    if not requested or not requested <= TYPES:
        raise ValueError("Chọn loại tệp CAE, ODB hoặc INP.")
    choice = next((g for g in solver_choices(collection, job) if g["id"] == selection), None)
    if choice is None:
        raise ValueError("Lượt chạy không còn trong bản lưu này. Làm mới danh sách rồi chọn lại.")
    files = [f for f in choice["files"] if f["type"] in requested]
    if {f["type"] for f in files} != requested:
        raise ValueError("Lượt đã chọn chưa lưu đủ các loại tệp yêu cầu.")
    return choice, files


def fetch_manifest(github, collection):
    """Read only a bounded manifest asset identified by the server catalog."""
    url = public_asset(collection["full_manifest_url"])
    tag, name = urlsplit(url).path.split("/releases/download/", 1)[1].split("/", 1)
    release = github.repo("GET", "/releases/tags/" + quote(unquote(tag), safe=""))
    asset = next((a for a in release.get("assets", []) if a["name"] == unquote(name)), None)
    limit = 24 * 1024**2
    if asset is None or type(asset.get("id")) is not int or not 0 < asset.get("size", 0) <= limit:
        raise ValueError("Chưa đọc được danh sách nguồn. Thử lại hoặc mở kho dữ liệu job.")
    headers = {"Accept": "application/octet-stream"}
    token = github.access_token()
    if token:
        headers["Authorization"] = "Bearer " + token
    content = bytearray()
    with github.client.stream(
        "GET", f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset['id']}",
        headers=headers, timeout=30,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(65536):
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError("Danh sách nguồn vượt giới hạn tải. Mở kho dữ liệu job để lấy bản gốc.")
    if len(content) != asset["size"]:
        raise ValueError("Danh sách nguồn tải chưa đủ. Thử lại.")
    if asset.get("digest") and asset["digest"] != "sha256:" + hashlib.sha256(content).hexdigest():
        raise ValueError("Checksum danh sách nguồn không khớp. Thử lại.")
    manifest = json.loads(content)
    validate_manifest(manifest, allow_incomplete=True)
    return manifest


def download_package(manifest, choice, files):
    by_path = {f["path"]: f for f in manifest["files"]}
    selected = []
    folder = "solver-run-" + str(choice["index"]).zfill(4) + "--" + choice["id"]
    for file in files:
        original = by_path.get(file["path"])
        if original is None or any(original[k] != file[k] for k in ("bytes", "sha256")):
            raise ValueError("Danh sách tệp và nguồn không khớp. Làm mới job trước khi tải.")
        selected.append({**original, "path": folder + "/" + file["name"]})
    package = {"schema_version": "1", "kind": "honeycomb-release-archive", "complete": True,
               "folder": folder, "readable_prefix": folder + "/00_dataset_versions/selected",
               "storage_mode": "direct-source-parts", "files": selected,
               "source_complete": manifest["complete"], "selection": choice["id"]}
    validate_manifest(package)
    return package


def download_script(package):
    source = Path(__file__).with_name("standalone_download.py").read_text(encoding="utf-8-sig")
    source = source.replace("REPOSITORY = 'xuankhanh24/honeycomb-abaqus-studio'  # replaced in generated download_full.py",
                            'REPOSITORY = ' + repr(REPOSITORY))
    return source.replace('MANIFEST = json.loads("{}")  # replaced in generated download_full.py',
                          "MANIFEST = json.loads(" + repr(json.dumps(package, ensure_ascii=True)) + ")")
