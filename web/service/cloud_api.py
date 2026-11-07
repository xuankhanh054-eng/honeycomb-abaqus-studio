"""Stateless web control plane: contracts and GitHub API only, never Engine/SQLite."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .cloud_github import JOB_ID, REPOSITORY, Catalog, Github
from .cloud_matrix import TERMINAL, matrix_entries, matrix_plan, recover, workflow_title
from .cloud_quality import longer_research_spec
from .cloud_runtime import execution_runtime, qualification, technical_evidence
from .cloud_solver_download import (
    download_package,
    download_script,
    fetch_manifest,
    selected_files,
    solver_choices,
)
from .models import BatchSpec, ModelSpec, catalogue, digest, expand

OWNER_ID = 273490538
PRODUCTION_WEB_ORIGIN = "https://honeycomb-abaqus-studio.vercel.app"


class Submission(BaseModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    profile: Literal["smoke", "research", "full"] = "research"
    spec: BatchSpec


class TrainingRetry(BaseModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def gates():
    return execution_runtime()[1]


def create_app(github: Github | None = None) -> FastAPI:
    app = FastAPI(title="Honeycomb Cloud", version="0.3.0")
    github = github or Github()
    catalog = Catalog(github)
    public_origin = os.getenv("WEB_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if not public_origin and any(os.getenv(key) for key in ("VERCEL", "VERCEL_ENV", "VERCEL_GIT_COMMIT_SHA")):
        public_origin = PRODUCTION_WEB_ORIGIN
    if public_origin:
        parts = urlsplit(public_origin)
        local_http = (os.getenv("WEB_LOCAL_HTTP") == "1" and parts.scheme == "http"
                      and parts.hostname in ("127.0.0.1", "localhost", "::1"))
        if ((parts.scheme != "https" and not local_http) or not parts.hostname
                or parts.username or parts.password or parts.path or parts.query or parts.fragment):
            raise ValueError("WEB_PUBLIC_ORIGIN phải là origin HTTPS, không chứa đường dẫn hoặc thông tin đăng nhập")

    def oauth_callback_url(request):
        return (public_origin or str(request.base_url).rstrip("/")) + "/auth/callback"

    @app.middleware("http")
    async def bounded_body(request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            chunks = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 262144:
                    return JSONResponse({"detail": "Input vượt giới hạn 256 KiB."}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        return await call_next(request)

    session_secret = os.getenv("WEB_SESSION_SECRET")
    if session_secret:
        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret,
            https_only=os.getenv("WEB_LOCAL_HTTP") != "1",
            same_site="lax",
            max_age=43200,
        )

    def session(request):
        return request.scope.get("session", {})

    def owner(request):
        return session(request).get("user_id") == OWNER_ID

    def require_owner(request):
        if not owner(request):
            raise HTTPException(403, "Chỉ chủ dự án được điều khiển mô phỏng.")
        csrf = request.headers.get("X-Honeycomb-CSRF", "")
        if not csrf or not secrets.compare_digest(csrf, session(request).get("csrf", "")):
            raise HTTPException(403, "Phiên thao tác đã hết hạn. Đăng nhập lại.")
        if not github.configured:
            raise HTTPException(503, "Chưa kết nối GitHub App.")

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request, exc):
        return JSONResponse(
            {"detail": "GitHub chưa phản hồi thành công. Thử lại; bài đã lưu vẫn được giữ."}, status_code=502
        )

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/api/health")
    def health():
        return {
            "service": "honeycomb-cloud",
            "version": "0.3.0",
            "revision": os.getenv("VERCEL_GIT_COMMIT_SHA"),
            "test_solver": False,
            "repository": REPOSITORY,
            "storage": "GitHub Releases",
            "worker_started_in_web": False,
        }

    @app.get('/api/campaigns/{identity}')
    def campaign_status(identity: str):
        if not re.fullmatch(r'core-damage-v2-[0-9]{8}(?:-v6p5)?', identity):
            raise HTTPException(404, 'Không tìm thấy chiến dịch.')
        report, _ = catalog.get('campaigns/' + identity + '/status')
        if report is None:
            raise HTTPException(404, 'Chiến dịch chưa được khởi tạo trên GitHub.')
        return report

    @app.get("/api/session")
    def identity(request: Request):
        return {
            "owner": owner(request),
            "csrf": session(request).get("csrf") if owner(request) else None,
            "login_available": bool(
                session_secret
                and os.getenv("GITHUB_OAUTH_CLIENT_ID")
                and os.getenv("GITHUB_OAUTH_CLIENT_SECRET")
            ),
        }

    @app.get("/api/status")
    def status():
        return {
            "gates": gates(),
            "execution_runtime": execution_runtime()[0],
            "qualification_gates": qualification()[1],
            "technical_evidence": technical_evidence(),
            "github_app_connected": github.configured,
            "license_source": "FlexNet trên máy của chủ dự án, qua mạng riêng",
            "max_runs": 100,
            "cpus": 4,
            "workflow_minutes": 330,
            "qualified_linux_solver": not any(g["status"] == "blocked_environment" for g in qualification()[1]),
            "evidence_url": f"https://github.com/{REPOSITORY}/actions/runs/40168384430",
            "runner": {
                "cpu": 4,
                "ram_gib": 15.61,
                "disk_total_gib": 145.19,
                "disk_free_gib": 85.64,
                "measured_at": "2026-10-25T06:14:23Z",
            },
        }

    @app.get("/api/template")
    def template():
        model = ModelSpec(core_topology="reentrant_0", reentrant_geometry="angle", clamp_mode="perimeter",
                          cpus=4, duration=.012, history_intervals=2400)
        return {
            "spec": BatchSpec(model=model, max_runs=100).model_dump(),
            "parameters": catalogue(),
            "schema": BatchSpec.model_json_schema(),
        }

    @app.get("/api/training")
    def training():
        value, _ = catalog.get("training/latest")
        return {"collection": value}

    @app.post("/api/training/collect")
    def collect_training(request: Request):
        require_owner(request)
        github.repo("POST", "/actions/workflows/collect-training.yml/dispatches", {"ref": "main"})
        return {"status": "dispatching", "workflow_url":
                f"https://github.com/{REPOSITORY}/actions/workflows/collect-training.yml"}

    @app.get("/api/jobs/{identity}/data")
    def job_data(identity: str):
        if not JOB_ID.fullmatch(identity):
            raise HTTPException(404)
        value, _ = catalog.get("jobs/" + identity)
        if value is None:
            raise HTTPException(404, "Không tìm thấy job.")
        data, _ = catalog.get("training/jobs/" + identity)
        return {"job_id": identity, "name": value["name"], "status": value["status"], "collection": data,
                "solver_runs": solver_choices(data, value) if data else [],
                "workflow_url": f"https://github.com/{REPOSITORY}/actions/runs/{value['workflow_run_id']}"
                if value.get("workflow_run_id") else None}

    @app.get("/api/jobs/{identity}/data/solver-download")
    def solver_download(identity: str, selection: str, types: str = "cae,odb,inp", format: str = "script"):
        if not JOB_ID.fullmatch(identity):
            raise HTTPException(404)
        value, _ = catalog.get("jobs/" + identity)
        data, _ = catalog.get("training/jobs/" + identity)
        if value is None or data is None:
            raise HTTPException(404, "Job chưa có bản lưu để tải.")
        if format not in ("script", "manifest"):
            raise HTTPException(422, "Định dạng tải không hợp lệ.")
        choice, files = selected_files(data, value, selection, types)
        package = download_package(fetch_manifest(github, data), choice, files)
        name = "download_run_" + str(choice["index"]).zfill(4)
        content = download_script(package) if format == "script" else json.dumps(package, indent=2)
        return Response(content, media_type="text/x-python" if format == "script" else "application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}.{"py" if format == "script" else "json"}"',
                                 "Cache-Control": "no-store"})

    @app.post("/api/jobs/{identity}/data/collect")
    def collect_job_data(identity: str, request: Request):
        require_owner(request)
        if job_data(identity)["status"] not in TERMINAL:
            raise HTTPException(409, "Đợi toàn bộ lượt trong đợt kết thúc trước khi tổng hợp dữ liệu.")
        github.repo("POST", "/actions/workflows/collect-training.yml/dispatches",
                    {"ref": "main", "inputs": {"job_id": identity}})
        return {"status": "dispatching", "workflow_url":
                f"https://github.com/{REPOSITORY}/actions/workflows/collect-training.yml"}

    @app.post("/api/preview")
    def preview(spec: BatchSpec):
        if spec.sampling == "lhs" and spec.samples > 100:
            raise HTTPException(422, "Cloud cho phép tối đa 100 mẫu LHS.")
        spec = spec.model_copy(update={"max_runs": 100})
        runs = expand(spec)
        if len(runs) > 100:
            raise HTTPException(422, "Cloud cho phép tối đa 100 lượt mỗi batch.")
        return {
            "count": len(runs),
            "execution": matrix_plan(spec.model_dump()),
            "input_hash": digest(spec.model_dump()),
            "runs": [
                {
                    "index": i,
                    "input_hash": r["input_hash"],
                    "design_id": r["design_id"],
                    "resolved": r["resolved"],
                }
                for i, r in enumerate(runs, 1)
            ],
            "gates": gates(),
        }

    @app.post("/api/geometry")
    def geometry(model: ModelSpec):
        import math

        from .adapters.hexagonal import network as hex_network
        from .adapters.reentrant import network

        resolved = model.resolved()
        pitch = (
            min(model.cell_size - resolved["reentrant_indent"], model.reentrant_cell_height)
            if model.core_topology == "reentrant_0"
            else min(
                2 * resolved["hex_l"] * math.sin(math.radians(resolved["hex_theta_deg"])),
                resolved["hex_b"] + resolved["hex_l"] * math.cos(math.radians(resolved["hex_theta_deg"])),
            )
        )
        if (
            max(model.panel, model.core_height) + abs(model.phase_x) + abs(model.phase_y) + abs(model.phase_z)
        ) / pitch > 60:
            raise HTTPException(422, "Mạng ô quá lớn để preview trên web; hãy tăng kích thước ô.")
        if model.core_topology == "reentrant_0":
            walls = network(
                model.panel,
                model.core_height,
                model.cell_size,
                model.reentrant_cell_height,
                resolved["reentrant_indent"],
                model.phase_x,
                model.phase_z,
            )
            return {
                "plane": "XZ",
                "width": model.panel,
                "height": model.core_height,
                "depth": model.panel,
                "walls": walls,
                "reentrant_indent_mm": resolved["reentrant_indent"],
                "delta_deg": resolved["reentrant_delta_deg"],
                "kind": "geometry_preview_not_solver_result",
            }

        def clip(a, b):
            # Liang-Barsky clipping, same finite square as the Abaqus builder.
            dx, dy = b[0] - a[0], b[1] - a[1]
            low, high = 0.0, 1.0
            half = model.panel / 2
            for p, q in ((-dx, a[0] + half), (dx, half - a[0]), (-dy, a[1] + half), (dy, half - a[1])):
                if abs(p) < 1e-12:
                    if q < 0:
                        return None
                else:
                    t = q / p
                    if p < 0:
                        low = max(low, t)
                    else:
                        high = min(high, t)
                    if low > high:
                        return None
            u, v = (a[0] + low * dx, a[1] + low * dy), (a[0] + high * dx, a[1] + high * dy)
            if math.dist(u, v) < model.min_segment_length:
                return None
            return tuple(sorted((tuple(round(x, 7) for x in u), tuple(round(x, 7) for x in v))))

        walls = hex_network(
            model.panel,
            resolved["hex_b"],
            resolved["hex_l"],
            resolved["hex_theta_deg"],
            model.core_angle,
            model.phase_x,
            model.phase_y,
            clip,
        )
        return {
            "plane": "XY",
            "width": model.panel,
            "height": model.panel,
            "depth": model.core_height,
            "walls": [[u, v, abs(u[0] - v[0]) < 1e-5] for u, v in walls],
            "kind": "geometry_preview_not_solver_result",
        }

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": catalog.jobs()}

    @app.get("/api/activity")
    def activity():
        """Read-only observation; a development run never claims a saved draft."""
        workflows = {
            ".github/workflows/collect-training.yml": "Tổng hợp dữ liệu huấn luyện",
            ".github/workflows/linux-impact-7j.yml": "Thử nghiệm native 7 J",
            ".github/workflows/verify-native-parallel.yml": "Thử nghiệm native song song",
            ".github/workflows/abaqus-linux.yml": "Job cloud",
            ".github/workflows/recover-native-artifact.yml": "Khôi phục kết quả native",
            ".github/workflows/verify-impact-release.yml": "Kiểm tra dữ liệu đã lưu",
            ".github/workflows/verify-native-recovery.yml": "Kiểm tra dữ liệu đã khôi phục",
        }
        try:
            runs = []
            for state in ("in_progress", "queued"):
                result = github.repo("GET", f"/actions/runs?status={state}&per_page=30")
                for run in result.get("workflow_runs", []):
                    path = run.get("path", "").split("@", 1)[0]
                    identity = run.get("id")
                    if path not in workflows or type(identity) is not int or identity <= 0:
                        continue
                    runs.append({
                        "id": identity, "name": run.get("display_title") or run.get("name"),
                        "scope": workflows[path], "status": run.get("status"),
                        "started_at": run.get("run_started_at"),
                        "url": f"https://github.com/{REPOSITORY}/actions/runs/{identity}",
                    })
            return {"available": True, "runs": runs}
        except (httpx.HTTPError, ValueError, KeyError):
            return {"available": False, "runs": []}

    @app.get("/api/jobs/{identity}")
    def job(identity: str):
        if not JOB_ID.fullmatch(identity):
            raise HTTPException(404)
        value, _ = catalog.get("jobs/" + identity)
        if value is None:
            raise HTTPException(404, "Không tìm thấy bài.")
        if value.get("workflow_run_id"):
            value["workflow"] = github.repo("GET", "/actions/runs/" + str(value["workflow_run_id"]))
        collection, _ = catalog.get("training/jobs/" + identity)
        value["data_collection"] = ({k: collection.get(k) for k in
                                    ("source_complete", "source_status", "workflow_run_id", "collection_workflow_url")}
                                    if collection else None)
        value["parameter_title"] = workflow_title(value["spec"])
        if value.get("shards"):
            labels = {e["child_id"]: e["display_name"] for e in matrix_entries(value, pending_only=False)["include"]}
            value["shards"] = [{**s, "display_name": labels[s["id"]]} for s in value["shards"]]
            try:
                actions = []
                for page in (1, 2):
                    rows = github.repo("GET", f"/actions/runs/{value['workflow_run_id']}/jobs?per_page=100&page={page}").get("jobs", [])
                    actions.extend(rows)
                    if len(rows) < 100:
                        break
                by_name = {a["name"]: a for a in actions}
                for shard in value["shards"]:
                    # Completed recovery sources belong to their original workflow.
                    if shard.get("workflow_run_id", value["workflow_run_id"]) != value["workflow_run_id"]:
                        continue
                    action = by_name.get(shard["display_name"])
                    if action:
                        shard.update(action_status=action["status"], action_job_id=action["id"])
                value["queue_snapshot_available"] = True
            except (httpx.HTTPError, KeyError, TypeError):
                value["queue_snapshot_available"] = False
        return value

    @app.get("/api/jobs/{identity}/field-view/{run_id}")
    def field_view(identity: str, run_id: str):
        # The caller cannot supply a URL or asset ID. Both come from the trusted catalog.
        if not JOB_ID.fullmatch(identity):
            raise HTTPException(404)
        value, _ = catalog.get("jobs/" + identity)
        record = next((r for r in (value or {}).get("runs", []) if r["id"] == run_id), {})
        asset = record.get("field_view", {})
        asset_id = asset.get("asset_id")
        limit = 4 * 1024**2
        if not isinstance(asset_id, int) or not 0 < asset.get("bytes", 0) <= limit:
            raise HTTPException(404, "Chưa có bản hiển thị đủ điều kiện tải.")
        headers = {"Accept": "application/octet-stream"}
        token = github.access_token()
        if token:
            headers["Authorization"] = "Bearer " + token
        content = bytearray()
        with github.client.stream(
            "GET", f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset_id}", headers=headers
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(65536):
                content.extend(chunk)
                if len(content) > limit:
                    raise HTTPException(
                        502, "Bản hiển thị vượt giới hạn; tải bộ dữ liệu trực tiếp từ Releases."
                    )
        if len(content) != asset["bytes"] or hashlib.sha256(content).hexdigest() != asset.get("sha256"):
            raise HTTPException(502, "Checksum bản hiển thị không khớp; dữ liệu chưa được hiển thị.")
        data = json.loads(content)
        if data.get("kind") != "honeycomb-field-view":
            raise HTTPException(502, "Asset không phải dữ liệu trường.")
        return Response(
            bytes(content), media_type="application/json", headers={"Cache-Control": "public, max-age=300"}
        )

    @app.post("/api/jobs", status_code=201)
    def submit(body: Submission, request: Request):
        require_owner(request)
        preview(body.spec)
        document = {
            "id": body.request_id,
            "name": body.spec.name,
            "profile": body.profile,
            "spec": body.spec.model_dump(),
            "input_hash": digest(body.spec.model_dump()),
            "status": "draft",
            "parameter_title": workflow_title(body.spec),
        }

        def insert(old):
            if old and (old["input_hash"] != document["input_hash"] or old["profile"] != document["profile"]):
                raise HTTPException(409, "Request ID đã được dùng cho input khác.")
            return old or document

        saved = catalog.update("jobs/" + body.request_id, insert)

        def index(old):
            rows = (old or {}).get("jobs", [])
            if not any(r["id"] == body.request_id for r in rows):
                rows.insert(0, {k: saved[k] for k in ("id", "name", "status", "profile", "parameter_title")})
            return {"jobs": rows}

        catalog.update("index", index)
        return saved

    @app.post("/api/jobs/{identity}/dispatch")
    def dispatch(identity: str, request: Request):
        require_owner(request)
        value = job(identity)
        if gates():
            return JSONResponse(
                {"detail": "Bài đã lưu; chưa đủ điều kiện chạy thật.", "gates": gates()}, status_code=409
            )

        def claim(old):
            if old["status"] != "draft" or old.get("cancel_requested"):
                raise HTTPException(409, "Bài đã được đưa vào hàng đợi.")
            return {**old, "status": "dispatching"}

        catalog.update("jobs/" + identity, claim)
        try:
            github.repo(
                "POST",
                "/actions/workflows/abaqus-linux.yml/dispatches",
                {"ref": "main", "inputs": {"job_id": identity, "job_name": workflow_title(value["spec"])}},
            )
        except httpx.HTTPError:
            # Leave dispatching: the server may have accepted the request before a lost response.
            raise
        return {"id": value["id"], "status": "dispatching"}

    @app.post("/api/jobs/{identity}/recover")
    def recover_job(identity: str, request: Request):
        require_owner(request)
        value = job(identity)
        if gates():
            raise HTTPException(409, "Môi trường chưa sẵn sàng; chưa gửi lượt chạy bù.")
        if value.get("workflow", {}).get("status") != "completed":
            raise HTTPException(409, "Đợi workflow hiện tại kết thúc trước khi chạy bù.")
        try:
            recover(value, 0)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

        def reserve(old):
            if old.get("recovery_dispatch_pending") or old.get("workflow_run_id") != value.get("workflow_run_id"):
                raise HTTPException(409, "Lượt chạy bù đã được gửi; mở Actions để theo dõi.")
            recover(old, 0)
            return {**old, "recovery_dispatch_pending": True}

        catalog.update("jobs/" + identity, reserve)
        github.repo("POST", "/actions/workflows/abaqus-linux.yml/dispatches", {
            "ref": "main", "inputs": {"job_id": identity, "job_name": workflow_title(value["spec"]), "recover": True}})
        return {"id": identity, "recovery_dispatch_pending": True}

    @app.post("/api/jobs/{identity}/cancel")
    def cancel(identity: str, request: Request):
        require_owner(request)
        value = job(identity)
        if value["status"] in ("completed", "review", "failed", "archive_failed", "cancelled", "timed_out"):
            raise HTTPException(409, "Bài đã kết thúc; kết quả được giữ nguyên.")
        # The worker observes this flag and stops its owned solver, then archives
        # partial outputs. GitHub's immediate cancel would kill that cleanup.
        catalog.update("jobs/" + identity, lambda old: {**old, "cancel_requested": True})
        return {"cancel_requested": True}

    @app.post("/api/jobs/{identity}/training-retry", status_code=201)
    def training_retry(identity: str, body: TrainingRetry, request: Request):
        require_owner(request)
        parent = job(identity)
        spec = longer_research_spec(parent)
        submit(Submission(request_id=body.request_id, profile=parent["profile"], spec=spec), request)
        return catalog.update("jobs/" + body.request_id, lambda old: {
            **old, "source_job_id": identity,
            "quality_retry": "Thời gian vật lý ×2, giữ mật độ ghi history; không sửa kết quả hoặc ngưỡng chất lượng gốc.",
        })

    @app.get("/auth/login")
    def login(request: Request):
        if not session_secret or not os.getenv("GITHUB_OAUTH_CLIENT_ID"):
            raise HTTPException(503, "Cần cấu hình GitHub OAuth trước khi đăng nhập.")
        if public_origin and str(request.base_url).rstrip("/") != public_origin:
            # State and its signed cookie must originate on the callback domain.
            # Redirecting only redirect_uri would lose that cookie across aliases.
            return RedirectResponse(public_origin + "/auth/login", status_code=307)
        nonce = secrets.token_urlsafe(32)
        request.session["oauth_state"] = nonce
        callback = oauth_callback_url(request)
        query = httpx.QueryParams(
            {
                "client_id": os.environ["GITHUB_OAUTH_CLIENT_ID"],
                "state": nonce,
                "redirect_uri": callback,
                "scope": "read:user",
            }
        )
        return RedirectResponse("https://github.com/login/oauth/authorize?" + str(query))

    @app.get("/auth/callback", name="callback")
    def callback(request: Request, code: str, state: str):
        if public_origin and str(request.base_url).rstrip("/") != public_origin:
            raise HTTPException(400, "Bắt đầu lại đăng nhập tại " + public_origin + "/auth/login")
        expected = session(request).pop("oauth_state", None)
        if not expected or not secrets.compare_digest(state, expected):
            raise HTTPException(403, "OAuth state không khớp.")
        with httpx.Client(timeout=20) as client:
            result = client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                json={
                    "client_id": os.environ["GITHUB_OAUTH_CLIENT_ID"],
                    "client_secret": os.environ["GITHUB_OAUTH_CLIENT_SECRET"],
                    "code": code,
                    "redirect_uri": oauth_callback_url(request),
                },
            )
            result.raise_for_status()
            token = result.json().get("access_token")
            if not token:
                raise HTTPException(403, "GitHub chưa cấp quyền đăng nhập.")
            user = client.get("https://api.github.com/user", headers={"Authorization": "Bearer " + token})
            user.raise_for_status()
            if user.json()["id"] != OWNER_ID:
                raise HTTPException(403, "Tài khoản này chỉ có quyền xem dữ liệu công khai.")
        request.session.clear()
        request.session.update(user_id=OWNER_ID, csrf=secrets.token_urlsafe(32))
        return RedirectResponse("/", status_code=303)

    @app.post("/auth/logout")
    def logout(request: Request):
        require_owner(request)
        request.session.clear()
        return {"logged_out": True}

    @app.get("/api/setup")
    def setup():
        # Names and readiness only; never return values of any credential.
        keys = [
            "WEB_SESSION_SECRET",
            "GITHUB_OAUTH_CLIENT_ID",
            "GITHUB_OAUTH_CLIENT_SECRET",
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_INSTALLATION_ID",
        ]
        return {
            "settings": [{"name": key, "configured": bool(os.getenv(key))} for key in keys],
            "documentation": f"https://github.com/{REPOSITORY}/blob/main/docs/CLOUD_WEB.md",
        }

    return app
