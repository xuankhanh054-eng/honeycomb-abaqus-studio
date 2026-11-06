"""Durable, per-design Actions dispatch with CAS claims and explicit predecessor receipts."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from copy import deepcopy

import httpx

from .campaign import OWNERS
from .cloud_github import Catalog, Github
from .models import digest

CAMPAIGN = re.compile(r"^core-damage-v2-[0-9]{8}$")
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
TERMINAL = {"completed", "quarantined", "budget_exhausted"}


def campaign_key(identity):
    if not CAMPAIGN.fullmatch(identity):
        raise ValueError("Invalid campaign ID")
    return "campaigns/" + identity


def ticket_key(identity, ticket):
    if not re.fullmatch(r"[a-f0-9]{32}", ticket):
        raise ValueError("Invalid execution ticket")
    return campaign_key(identity) + "/work/" + ticket


class CampaignStore:
    """Separate metadata and owner shards, each comfortably below Contents' 1-MiB limit."""

    def __init__(self, github):
        self.catalog = Catalog(github)

    def initialize(self, manifest):
        key = campaign_key(manifest["id"])
        for owner in OWNERS:
            rows = [r for r in manifest["designs"] + manifest["pilot_designs"] if r["owner"] == owner]
            self.catalog.update(key + "/owners/" + owner, lambda old, rows=rows: old or {"designs": rows})
        metadata = {k: v for k, v in manifest.items() if k not in ("designs", "pilot_designs")}
        metadata.update(phase="material", enabled=False, audit_ids=[], infrastructure={})
        return self.catalog.update(key + "/meta", lambda old: old or metadata)

    def load(self, identity):
        key = campaign_key(identity)
        metadata, _ = self.catalog.get(key + "/meta")
        if metadata is None:
            raise ValueError("Campaign has not been initialized")
        rows = []
        for owner in OWNERS:
            shard, _ = self.catalog.get(key + "/owners/" + owner)
            if shard is None:
                raise ValueError("Missing owner shard")
            rows.extend(shard["designs"])
        return {**metadata, "designs": [r for r in rows if r["role"] != "pilot"],
                "pilot_designs": [r for r in rows if r["role"] == "pilot"]}

    def update_design(self, identity, owner, design_id, transform):
        if owner not in OWNERS:
            raise ValueError("Unknown owner")

        def change(old):
            value = deepcopy(old)
            matches = [r for r in value["designs"] if r["id"] == design_id]
            if len(matches) != 1:
                raise ValueError("Unknown or ambiguous design")
            updated = transform(deepcopy(matches[0]))
            value["designs"][value["designs"].index(matches[0])] = updated
            return value

        return self.catalog.update(campaign_key(identity) + "/owners/" + owner, change)


def claim(old, *, ticket, workflow, source_sha, repository):
    """Called by the worker before any solver starts; duplicate dispatches cannot both claim."""
    if not old or old.get("ticket") != ticket or old.get("repository") != repository:
        raise ValueError("Execution ticket identity mismatch")
    if old.get("source_sha") != source_sha:
        raise ValueError("Worker source differs from frozen campaign source")
    if old.get("status") != "ready" or old.get("workflow_run_id"):
        raise ValueError("Execution already claimed; refusing duplicate solver")
    if old["segment"]:
        previous = old.get("predecessor", {})
        if (previous.get("outcome") != "checkpoint" or previous.get("segment") != old["segment"] - 1
                or previous.get("physics_hash") != old["design"]["physics_hash"]
                or previous.get("source_sha") != source_sha or not previous.get("archive_sha256")):
            raise ValueError("Missing or mismatched predecessor checkpoint")
    return {**old, "workflow_run_id": workflow, "status": "claimed", "claimed_at": time.time()}


def admission(manifest, design):
    if not manifest.get("enabled") or not manifest.get("source_sha") or not manifest.get("runtime_hash"):
        return False
    if manifest.get('execution_scope') == 'pilot' and design['role'] != 'pilot':
        return False
    infrastructure = manifest.get("infrastructure", {}).get(design["owner"], {})
    if infrastructure.get("passed") is not True:
        return False
    if design["role"] == "pilot":
        return manifest.get("gates", {}).get("material", {}).get("passed") is True
    if design["role"] in ("qualification", "material"):
        return (manifest.get('gates', {}).get('material', {}).get('passed') is True
                and manifest.get('gates', {}).get('restart', {}).get('passed') is True)
    if not manifest.get("frozen"):
        return False
    gates = manifest.get("gates", {})
    if not all(gates.get(k, {}).get("passed") is True
               for k in ("material", "time", "mesh", "damage_sensitivity", "archive_restore")):
        return False
    if design["id"] in manifest.get("audit_ids", []):
        return True
    return gates.get("development_time_audit", {}).get("passed") is True


def available_slots(capacity, owner_jobs, reserved=0):
    """Apply the solver budget and shared owner quota independently."""
    active = [j for j in owner_jobs if j.get("status") in ACTIVE]
    # Unclassified observations count conservatively against both limits.
    solvers = sum(j.get("campaign_solver", True) for j in active)
    return max(0, min(min(20, capacity) - solvers - reserved, 20 - len(active) - reserved))


def ramp_capacity(manifest):
    """Probe the next stage only after successful, overlapping solver and archive evidence."""
    stage = manifest.get('capacity_stage', 8)
    rows = [r for r in manifest['designs'] + manifest['pilot_designs']
            if r.get('archive_verified') and r.get('quality', {}).get('passed')
            and r.get('solver_started_at') and r.get('solver_finished_at')]
    events = sorted([(r['solver_started_at'], 1) for r in rows]
                    + [(r['solver_finished_at'], -1) for r in rows])
    active = peak = 0
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    manifest['observed_successful_parallel_solvers'] = peak
    if peak >= stage and len(rows) >= stage:
        stages = (8, 20, 40, 80, 160)
        next_stage = next((level for level in stages if level > stage), stage)
        manifest['capacity_stage'] = min(next_stage, manifest['license_capacity'])
    return manifest


def page_jobs(github, *, warnings=None):
    warnings = [] if warnings is None else warnings
    owner = github.repository.split("/")[0]
    repositories = github.request("GET", "/users/" + owner + "/repos?per_page=100&type=owner")
    if len(repositories) == 100:
        raise ValueError("Owner repository inventory exceeds bounded admission audit")
    jobs = []
    for repo in repositories:
        for status in ("in_progress", "queued", "waiting", "pending", "requested"):
            path = f"/repos/{repo['full_name']}/actions/runs?per_page=100&status={status}"
            client = github
            try:
                runs = client.request("GET", path, allow_missing=True)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (403, 404) or repo['full_name'] == github.repository:
                    raise
                client = Github()  # Public read only; never broaden the App installation.
                try:
                    runs = client.request('GET', path, allow_missing=True)
                except httpx.HTTPError:
                    warnings.append('Other public workflow inventory unavailable: ' + repo['full_name'])
                    break
            if runs is None:
                if repo.get("private"):
                    raise ValueError("Cannot audit other private workflows sharing owner quota")
                continue
            if runs.get("total_count", 0) > 100:
                raise ValueError("Actions inventory exceeds bounded admission audit")
            for run in runs.get("workflow_runs", []):
                result = client.request("GET", f"/repos/{repo['full_name']}/actions/runs/{run['id']}/jobs?per_page=100")
                if result.get("total_count", 0) > 100:
                    raise ValueError("Workflow job inventory exceeds bounded admission audit")
                workflow = (run.get('path') or '').split('@')[0]
                is_solver = (repo['full_name'] == github.repository and
                             (not workflow or workflow == '.github/workflows/core-damage-worker.yml'))
                jobs.extend({**job, 'campaign_solver': is_solver} for job in result["jobs"])
    return jobs


class Dispatcher:
    def __init__(self, store, app):
        self.store, self.app = store, app

    def reserve(self, manifest, row):
        if not admission(manifest, row):
            raise ValueError("Scientific or infrastructure admission gate is closed")
        ticket = uuid.uuid4().hex

        def reserve(old):
            if old["status"] not in ("pending", "checkpoint", "solver_retry", "preparation_retry"):
                raise ValueError("Design is not dispatchable")
            if old.get("ticket") and old.get("status") == "dispatching":
                return old
            segment = old.get("segment", 0)
            attempt = old.get("attempt", 0)
            if segment >= 4 or attempt > 2:
                raise ValueError("Execution budget exhausted")
            return {**old, "status": "dispatching", "ticket": ticket,
                    "dispatch_started_at": time.time()}

        shard = self.store.update_design(manifest["id"], row["owner"], row["id"], reserve)
        current = next(r for r in shard["designs"] if r["id"] == row["id"])
        worker_source = (current['predecessor']['source_sha'] if current.get('segment', 0)
                         else manifest['source_sha'])
        github = self.app.repository(row["repository"])
        work = {"campaign_id": manifest["id"], "ticket": ticket, "design": current,
                "repository": row["repository"], "source_sha": worker_source,
                "runtime_hash": manifest["runtime_hash"], "protocol_hash": manifest.get("protocol_hash"),
                "segment": current.get("segment", 0), "attempt": current.get("attempt", 0),
                "predecessor": current.get("predecessor"), "status": "ready"}
        Catalog(github).update(ticket_key(manifest["id"], ticket), lambda old: old or work)
        # A lost response is left reserved. Reconciliation reuses this ticket; never invent a new solver.
        github.repo("POST", "/actions/workflows/core-damage-worker.yml/dispatches",
                    {"ref": "main", "inputs": {"campaign_id": manifest["id"], "ticket": ticket,
                                               "source_sha": worker_source}})
        return ticket


def apply_outcome(row, result):
    """Only archived receipts advance a design or schedule a continuation."""
    if result.get("ticket") != row.get("ticket") or result.get("design", {}).get("physics_hash") != row["physics_hash"]:
        raise ValueError("Outcome does not belong to the reserved design")
    value = deepcopy(row)
    if result.get('source_sha'):
        value['source_sha'] = result['source_sha']
    outcome = result.get("outcome")
    if outcome in ("completed", "checkpoint") and not result.get("archive_verified"):
        raise ValueError("Unverified output cannot release a solver slot")
    if outcome == "completed":
        value.update(status="completed", summary=result["summary"], quality=result["quality"],
                     archive_verified=True, archive=archive_reference(row, result), workflow_run_id=result["workflow_run_id"],
                     solver_wall_seconds=result.get('solver_wall_seconds'),
                     execution_wall_seconds=sum(h.get('execution_wall_seconds') or 0 for h in value.get('execution_history', []))
                     + (result.get('execution_wall_seconds') or 0),
                     solver_started_at=result.get('solver_started_at'), solver_finished_at=result.get('solver_finished_at'),
                     error=None)
        if result['quality'].get('passed') is not True:
            attempt = value.get('attempt', 0) + 1
            value.update(status='solver_retry' if attempt <= 2 else 'quarantined', attempt=attempt,
                         segment=0, predecessor=None, error='Numerical quality gate: ' + str(result['quality'].get('errors')))
    elif outcome == "checkpoint":
        segment = result["segment"] + 1
        value.update(status="checkpoint" if segment < 4 else "budget_exhausted", segment=segment,
                     predecessor=result["receipt"], archive=result["archive"])
    elif outcome == "solver_failed":
        attempt = value.get("attempt", 0) + 1
        value.update(status="solver_retry" if attempt <= 2 else "quarantined", attempt=attempt,
                     segment=0, predecessor=None, error=result.get("error"))
    elif outcome == 'prepare_failed':
        attempts = value.get('preparation_attempt', 0) + 1
        value.update(status='preparation_retry' if attempts <= 3 else 'needs_investigation',
                     preparation_attempt=attempts, error=result.get('error'))
    elif outcome == "upload_failed":
        value.update(status="publication_pending", publication_ticket=result['ticket'],
                     recoverable_artifact=result.get("recoverable_artifact"),
                     workflow_run_id=result['workflow_run_id'])
    else:
        return value
    value.setdefault("execution_history", []).append({k: result.get(k) for k in
        ("ticket", "workflow_run_id", "outcome", "segment", "attempt", "error", 'source_sha', 'execution_wall_seconds',
         'solver_started_at', 'solver_finished_at')})
    value["ticket"] = None
    return value


def archive_reference(row, result):
    """Keep owner shards bounded; full immutable inventories stay in their release assets."""
    archive = result['archive']
    if archive.get('kind') != 'honeycomb-release-archive':
        return archive
    payload = json.dumps(archive, indent=2, ensure_ascii=True).encode()
    ticket = result['ticket']
    return {'kind': 'honeycomb-archive-reference', 'repository': row['repository'],
            'manifest_url': f"https://github.com/{row['repository']}/releases/download/core-damage-{ticket}/{ticket}-artifact-manifest.json",
            'manifest_sha256': hashlib.sha256(payload).hexdigest(), 'complete': archive['complete'],
            'logical_files': len(archive['files']), 'bytes': sum(f['bytes'] for f in archive['files'])}


def receipt_hash(receipt):
    return digest({k: v for k, v in receipt.items() if k != "receipt_hash"})
