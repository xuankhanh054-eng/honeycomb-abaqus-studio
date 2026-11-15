"""One native restart per hosted job; no path can submit a fresh initial solve."""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from continuation_contract import STEP, merge_rows, restart_deck
from repair_contract import orderly_stop

from honeycomb_studio import cloud_archive, solver
from honeycomb_studio.cloud_archive import ReleaseArchive
from honeycomb_studio.cloud_github import Catalog, Github
from honeycomb_studio.cloud_restore import restore

FAMILIES = ("abq", "res", "mdl", "stt", "pac", "prt", "sel", "odb")
KEY = "continuation-98-12ms-20261115/"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return [{k: None if v == "" else float(v) for k, v in r.items()} for r in csv.DictReader(stream)]


def write_rows(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def inventory(raw, job):
    files = [p for p in raw.glob(job + ".*") if p.is_file() and p.stat().st_size]
    missing = [
        e
        for e in FAMILIES
        if not any(p.name == job + "." + e or p.name.startswith(job + "." + e + ".") for p in files)
    ]
    if missing:
        raise ValueError("Missing native restart families: " + ",".join(missing))
    return {
        p.name: {"bytes": p.stat().st_size, "sha256": sha(p)}
        for p in files
        if p.suffix not in (".lck", ".023")
    }


def restore_source(plan, root, github):
    raw = root / "raw"
    raw.mkdir()
    source = plan["restart_source"]
    if source["kind"] == "artifact":
        artifact = github.repo("GET", f"/actions/artifacts/{source['artifact_id']}")
        if artifact["expired"] or artifact["digest"] != source["artifact_digest"]:
            raise ValueError("Source artifact expired or changed digest")
        required = source["artifact_bytes"] + source["native_bytes"] + 20 * 10**9
        if shutil.disk_usage(root).free < required:
            raise ValueError("Insufficient disk to verify and unpack original restart with 20 GB reserve")
        archive = root / "original.zip"
        digest, count = hashlib.sha256(), 0
        url = (
            f"https://api.github.com/repos/{github.repository}/actions/artifacts/{source['artifact_id']}/zip"
        )
        with (
            httpx.Client(timeout=120, follow_redirects=True) as client,
            client.stream("GET", url, headers={"Authorization": "Bearer " + github.token}) as response,
            archive.open("wb") as stream,
        ):
            if response.status_code != 200:
                raise ValueError("Artifact download failed: " + str(response.status_code))
            for block in response.iter_bytes(4 * 1024**2):
                count += len(block)
                if count > source["artifact_bytes"] + 1024**2:
                    raise ValueError("Artifact exceeds its pinned size")
                digest.update(block)
                stream.write(block)
        if "sha256:" + digest.hexdigest() != source["artifact_digest"]:
            raise ValueError("Original artifact SHA-256 mismatch")
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                name = Path(member.filename).name
                if name.startswith(plan["job_name"] + ".") and not name.endswith((".lck", ".023")):
                    with z.open(member) as src, (raw / name).open("xb") as dest:
                        shutil.copyfileobj(src, dest, 4 * 1024**2)
        archive.unlink()
    elif source["kind"] == "release":
        manifest = source["manifest"]
        selected = [
            f["path"]
            for f in manifest["files"]
            if Path(f["path"]).name.startswith((plan["job_name"] + ".", plan["job_name"] + "to12."))
            and not f["path"].endswith((".lck", ".023"))
        ]
        needed = sum(f["bytes"] for f in manifest["files"] if f["path"] in selected)
        if shutil.disk_usage(root).free < needed + 35 * 10**9:
            raise ValueError("Insufficient disk for retained checkpoint and continuation")
        restored = root / "restore"
        restored.mkdir()
        restore(manifest, restored, paths=selected, repository=github.repository, allow_incomplete=True)
        for name in selected:
            (restored / name).rename(raw / Path(name).name)
        shutil.rmtree(restored)
    else:
        raise ValueError("No original native restart state; fresh solve is forbidden")
    for name, expected in plan.get("source_native_sha256", {}).items():
        path = raw / name
        if path.exists() and sha(path) != expected:
            raise ValueError("Source native identity mismatch: " + name)
    save(
        root / "source-state-verification.json",
        {"job_name": plan["job_name"], "files": inventory(raw, plan["job_name"]), "new_initial_solve": False},
    )
    return raw


def total_progress(raw, job):
    return solver.sta_progress(raw / (job + ".sta"), 0.012)


def run_solver(executable, raw, arguments, job, target, root, deadline, update):
    (raw / "continuation.stop").unlink(missing_ok=True)
    (raw / "continuation-stop-ack.json").unlink(missing_ok=True)
    log = root / (job + "-solver.log")
    with log.open("w") as stream:
        process = subprocess.Popen(
            solver.command(str(executable), *arguments), cwd=raw, stdout=stream, stderr=subprocess.STDOUT
        )
        requested = False
        last_update = 0
        while process.poll() is None:
            if time.time() - last_update > 600:
                try:
                    update(
                        {
                            "phase": "solving",
                            "current_job": job,
                            "target_s": target,
                            "progress": total_progress(raw, job),
                        }
                    )
                except (httpx.HTTPError, RuntimeError) as error:
                    save(root / "progress-publication-warning.json", {"type": type(error).__name__})
                last_update = time.time()
            if time.time() > deadline and not requested:
                (raw / "continuation.stop").write_text("Orderly checkpoint; preserve native history\n")
                requested = True
            if time.time() > deadline + 600:
                raise RuntimeError("Orderly checkpoint not acknowledged; retain native diagnostics")
            time.sleep(5)
    progress = total_progress(raw, job)
    reached = (
        not progress.get("solver_error")
        and progress.get("total_time_s", 0) >= target - 1e-7
        and "THE ANALYSIS HAS COMPLETED SUCCESSFULLY" in solver.tail(raw / (job + ".sta"), 65536)
    )
    if reached:
        return True
    if requested and (raw / "continuation-stop-ack.json").exists():
        orderly_stop(log.read_text(encoding="utf-8", errors="replace"),
                     json.loads((raw / "continuation-stop-ack.json").read_text()), requested=True)
        inventory(raw, job)
        return False
    raise RuntimeError(
        "Native solver did not complete or yield a verified orderly checkpoint; see solver log"
    )


def segment_rows(native, model, expected_end_s=0.004):
    channels = native["histories"]

    def find(variable, region=None, optional=False):
        selected = [
            c
            for c in channels
            if c["variable"] == variable
            and (
                region.upper() in c["region"].upper()
                if region
                else c["region"].upper() in ("ASSEMBLY", "ASSEMBLY ASSEMBLY", "WHOLE MODEL")
            )
        ]
        if len(selected) != 1:
            if optional and not selected:
                return None
            raise ValueError("Nonunique or missing native history: " + variable + "/" + str(region))
        values = np.asarray(selected[0]["data"], dtype=float)
        if (
            len(values) < 2
            or not np.isfinite(values).all()
            or np.any(np.diff(values[:, 0]) <= 0)
            or np.max(np.diff(values[:, 0])) > 3e-6
        ):
            raise ValueError("Native history has missing, duplicate or nonfinite samples")
        if abs(values[0, 0]) > 3e-6 or abs(values[-1, 0] - expected_end_s) > 3e-6:
            raise ValueError("Native history does not cover continuation step")
        return values

    acceleration = find("A3", "IMPACTOR")
    times = acceleration[:, 0]

    def sample(variable, region=None, optional=False):
        values = find(variable, region, optional)
        return np.interp(times, values[:, 0], values[:, 1]) if values is not None else [None] * len(times)

    columns = {
        "time_s": times,
        "time_ms": times * 1000,
        "contact_force_N": model["impact_mass"] * (acceleration[:, 1] + model["gravity"] * 1000),
        "impactor_displacement_mm": -sample("U3", "IMPACTOR"),
        "velocity_mm_s": sample("V3", "IMPACTOR"),
        "acceleration_mm_s2": acceleration[:, 1],
        "top_U3_mm": sample("U3", "TOP", True),
        "bottom_U3_mm": sample("U3", "BOTTOM", True),
    }
    for variable in (
        "ALLKE",
        "ALLIE",
        "ALLAE",
        "ALLPD",
        "ALLSE",
        "ALLVD",
        "ALLWK",
        "ETOTAL",
        "ALLFD",
        "ALLDMD",
        "ALLCD",
    ):
        values = sample(variable, optional=True)
        columns[variable + "_J"] = [None if v is None else float(v) / 1000 for v in values]
    for group in ("CORE", "FRONT", "REAR"):
        for variable in ("ALLPD", "ALLDMD", "ALLSE", "ALLAE", "ALLIE"):
            columns[group + "_" + variable + "_J"] = sample(variable, group) / 1000
    return [
        {key: None if values[i] is None else float(values[i]) for key, values in columns.items()}
        for i in range(len(times))
    ]


def publish(root, raw, plan, github, status, update, current_job):
    # Preserve native state extensions, which the older general archive excluded.
    cloud_archive.ALLOWED.update("." + extension for extension in FAMILIES)
    stage = root / "publication"
    stage.mkdir(exist_ok=True)
    for path in root.iterdir():
        if path.is_file() and path.suffix in (".json", ".csv", ".parquet", ".log"):
            os.link(path, stage / path.name)
    native = stage / "raw"
    native.mkdir()
    for path in raw.iterdir():
        if not path.is_file() or path.suffix not in (
            *("." + e for e in FAMILIES),
            ".inp",
            ".sta",
            ".dat",
            ".msg",
            ".log",
        ):
            continue
        # Completed original ODB already has a durable source archive; a checkpoint must retain both jobs.
        if status == "completed" and path.name == plan["job_name"] + ".odb":
            continue
        os.link(path, native / path.name)
    attempt = int(plan.get("continuation_attempt", 0))
    tag = "continue-12ms-20261115-" + plan["design_id"] + "-" + str(attempt)
    source_sha = os.environ["TASK_CODE_SHA"]
    ref = github.repo("GET", "/git/ref/tags/" + tag, allow_missing=True)
    if not ref:
        github.repo("POST", "/git/refs", {"ref": "refs/tags/" + tag, "sha": source_sha})
    release = github.repo("GET", "/releases/tags/" + tag, allow_missing=True)
    if not release:
        release = github.repo(
            "POST",
            "/releases",
            {
                "tag_name": tag,
                "target_commitish": source_sha,
                "name": plan["design_id"] + " native continuation to 12 ms",
                "draft": False,
                "prerelease": True,
                "body": "Original native state continued; no fresh initial solve. Inspect verification and result status.",
            },
        )
    publisher = ReleaseArchive(github, release["id"], tag=tag)
    manifest = publisher.publish(stage, attempt=tag, complete=status == "completed")
    update(
        {
            "status": status,
            "phase": status,
            "archive": manifest,
            "release_url": release["html_url"],
            "current_job": current_job,
            "solver_reached_12ms": status == "completed",
        }
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--design-id", required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"(?:development|test)-\d{4}", args.design_id):
        raise ValueError("Invalid source design ID")
    github = Github(token=os.environ["GITHUB_TOKEN"])
    catalog = Catalog(github)
    key = KEY + args.design_id
    workflow = int(os.environ["GITHUB_RUN_ID"])

    def claim(old):
        if not old or old["status"] not in ("ready", "dispatching") or old.get("workflow_run_id"):
            raise ValueError("Duplicate or unapproved continuation")
        if old["repository"] != github.repository or old["expected_task_sha"] != os.environ["TASK_CODE_SHA"]:
            raise ValueError("Repository or continuation source mismatch")
        return {**old, "status": "preparing", "workflow_run_id": workflow}

    plan = catalog.update(key, claim)
    root = args.root.resolve()
    if root.exists():
        raise ValueError("Native continuation requires clean output directory")
    root.mkdir(parents=True)

    def update(changes):
        def transform(old):
            if old.get("workflow_run_id") != workflow:
                raise ValueError("Continuation claim changed")
            return {**old, **changes, "updated_epoch": time.time()}

        catalog.update(key, transform)
        save(root / "status.json", {"design_id": args.design_id, "workflow_run_id": workflow, **changes})

    current_job = plan["job_name"]
    save(root / "request.json", plan)
    try:
        raw = restore_source(plan, root, github)
        sys.path.insert(0, str(Path.cwd() / "scripts"))
        from native_cloud_worker import native_runtime

        from honeycomb_studio.engine import ADAPTERS

        shutil.copyfile(
            Path(__file__).with_name("extract_continuation.py"), ADAPTERS / "extract_continuation_98.py"
        )
        shutil.copyfile(
            Path(__file__).with_name("damage_metrics_fast.py"), ADAPTERS / "damage_metrics_fast_98.py"
        )
        control = raw / "continuation_control.c"
        control.write_bytes((Path.cwd() / "scripts/linux_media/continuation_control.c").read_bytes())
        deadline = float(os.environ["HONEYCOMB_WORKFLOW_STARTED_EPOCH"]) + 240 * 60
        with native_runtime(root, continuation=True) as executable:
            progress = total_progress(raw, current_job)
            if progress.get("total_time_s", 0) < 0.008 - 1e-7:
                update({"phase": "recover_original_to_8ms"})
                done = run_solver(
                    executable,
                    raw,
                    [
                        "job=" + current_job,
                        "recover",
                        "cpus=4",
                        "double=both",
                        "user=" + str(control),
                        "interactive",
                    ],
                    current_job,
                    0.008,
                    root,
                    deadline,
                    update,
                )
                if not done:
                    publish(root, raw, plan, github, "checkpoint", update, current_job)
                    return
            if total_progress(raw, plan["job_name"]).get("total_time_s", 0) < 0.008 - 1e-7:
                raise ValueError("Original state has not reached 8 ms")
            current_job = plan["job_name"] + "to12"
            deck = raw / (current_job + ".inp")
            if plan.get("resume_job") == current_job:
                inventory(raw, current_job)
                arguments = [
                    "job=" + current_job,
                    "recover",
                    "cpus=4",
                    "double=both",
                    "user=" + str(control),
                    "interactive",
                ]
            else:
                deck.write_text(
                    restart_deck((raw / (plan["job_name"] + ".inp")).read_text(encoding="ascii")),
                    encoding="ascii",
                )
                update({"phase": "native_restart_datacheck", "current_job": current_job})
                with (root / "restart-datacheck.log").open("w") as stream:
                    checked = subprocess.run(
                        solver.command(
                            str(executable),
                            "job=" + current_job + "_check",
                            "input=" + deck.name,
                            "oldjob=" + plan["job_name"],
                            "datacheck",
                            "cpus=4",
                            "double=both",
                            "interactive",
                        ),
                        cwd=raw,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=600,
                    )
                dat = raw / (current_job + "_check.dat")
                if (
                    checked.returncode
                    or not dat.is_file()
                    or "***ERROR" in solver.tail(dat, 1024**2).upper()
                    or "COMPLETED" not in (root / "restart-datacheck.log").read_text().upper()
                ):
                    raise ValueError("Native restart datacheck failed; no fresh solve substituted")
                update(
                    {
                        "phase": "native_restart_datacheck_passed",
                        "restart_datacheck_passed": True,
                        "current_job": current_job,
                    }
                )
                arguments = [
                    "job=" + current_job,
                    "input=" + deck.name,
                    "oldjob=" + plan["job_name"],
                    "cpus=4",
                    "double=both",
                    "user=" + str(control),
                    "interactive",
                ]
            done = run_solver(executable, raw, arguments, current_job, 0.012, root, deadline, update)
            if not done:
                publish(root, raw, plan, github, "checkpoint", update, current_job)
                return
            update({"phase": "extracting_native_8to12ms"})
            extraction = root / "native_continuation.json"
            # Both the native runtime and this adapter are mounted read-only for Abaqus Python.
            mounted_adapter = Path(os.environ["HONEYCOMB_ADAPTERS"]) / "extract_continuation_98.py"
            if not mounted_adapter.exists():
                raise ValueError("Pinned continuation extraction adapter is not staged")
            with (root / "extraction.log").open("w") as stream:
                subprocess.run(
                    solver.command(
                        str(executable),
                        "python",
                        str(mounted_adapter),
                        str(raw / (current_job + ".odb")),
                        str(extraction),
                        STEP,
                    ),
                    cwd=raw,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=3600,
                )
        native = json.loads(extraction.read_text())
        segment = segment_rows(native, plan["original_resolved"])
        source_folder = root / "original_history"
        source_folder.mkdir()
        restore(
            plan["original_8ms_manifest"],
            source_folder,
            paths=[plan["original_history_path"]],
            repository=github.repository,
        )
        original = rows(source_folder / plan["original_history_path"])
        combined, boundary = merge_rows(original, segment)
        write_rows(root / "impact_history_0to12ms.csv", combined)
        write_rows(root / "impact_history_8to12ms.csv", segment)
        pq.write_table(
            pa.Table.from_pylist(combined), root / "impact_history_0to12ms.parquet", compression="zstd"
        )
        rear = [{**r, "time_s": r["time_s"] + 0.008} for r in native["rear"]]
        write_rows(root / "rear_fields_8to12ms.csv", rear)
        energies = np.asarray([r["CORE_ALLPD_J"] + r["CORE_ALLDMD_J"] for r in combined])
        force = np.asarray([r["contact_force_N"] for r in combined])
        tail = np.asarray([r["time_s"] for r in combined]) >= 0.0115
        finished = bool(
            np.max(np.abs(force[tail])) <= max(1, 0.01 * np.max(force))
            and np.ptp(energies[tail]) <= 0.01 * max(energies[-1], 1e-6)
        )
        summary = {
            "design_id": plan["design_id"],
            "last_time_s": combined[-1]["time_s"],
            "SEA_core_J_kg": float(energies[-1]) / plan["core_mass_kg"],
            "core_dissipation_J": float(energies[-1]),
            "end_impactor_velocity_m_s": combined[-1]["velocity_mm_s"] / 1000,
            "event_finished_diagnostic": finished,
            "original_prefix_rows_unchanged": len(original),
            "boundary_absolute_errors": boundary,
            "native_step_start_s": native["step_start_s"],
            "native_odb_version": native["odb_version"],
            "damage_8to12ms": native["damage"],
            "physical_validation": "not_established",
            "solver_reached_12ms": True,
            "no_fresh_initial_solve": True,
            "original_8ms_source_unchanged": True,
        }
        save(root / "result.json", summary)
        update({"phase": "publishing", "summary": summary})
        publish(root, raw, plan, github, "completed", update, current_job)
    except Exception as error:
        message = re.sub(r"https?://\S+", "<redacted-url>", str(error))[:800]
        save(
            root / "failure.json",
            {"type": type(error).__name__, "message": message, "current_job": current_job},
        )
        update({"status": "failed", "phase": "failed", "error": message, "current_job": current_job})
        raise RuntimeError(type(error).__name__ + ": " + message) from None


if __name__ == "__main__":
    main()
