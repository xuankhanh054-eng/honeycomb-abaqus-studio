"""Repair native state continuations; separate recovered branches and numerical failures."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from artifact_restore import restore_artifact
from continuation_contract import STEP, merge_rows, restart_deck
from legacy_worker import inventory, rows, run_solver, save, segment_rows, write_rows

from honeycomb_studio import cloud_archive, solver
from honeycomb_studio.cloud_archive import ReleaseArchive
from honeycomb_studio.cloud_github import Catalog, Github
from honeycomb_studio.cloud_restore import restore

KEY = "continuation-98-12ms-20261115-repair-v3/"


def publish(root, raw, plan, github, status, current_job, update):
    cloud_archive.ALLOWED.update({".abq", ".res", ".mdl", ".stt", ".pac", ".prt", ".sel"})
    cloud_archive.ALLOWED.update("." + str(i) for i in range(1, 5))
    stage = root / "publication"
    stage.mkdir()
    for path in root.iterdir():
        if path.is_file() and path.suffix in (".json", ".csv", ".parquet", ".log"):
            os.link(path, stage / path.name)
    native = stage / "raw"
    native.mkdir()
    for path in raw.iterdir():
        if path.is_file() and (
            path.name.startswith(current_job + ".")
            or path.name == plan["job_name"] + ".odb"
            or path.name == "continuation-stop-ack.json"
        ):
            if path.suffix in (".lck", ".023"):
                continue
            if status == "completed" and path.name == plan["job_name"] + ".odb":
                continue
            os.link(path, native / path.name)
    attempt = int(plan.get("repair_attempt", 0))
    tag = "repair12ms-20261115-v3-" + plan["design_id"] + "-" + str(attempt)
    source_sha = os.environ["TASK_CODE_SHA"]
    if github.repo("GET", "/git/ref/tags/" + tag, allow_missing=True) is None:
        github.repo("POST", "/git/refs", {"ref": "refs/tags/" + tag, "sha": source_sha})
    release = github.repo("GET", "/releases/tags/" + tag, allow_missing=True)
    if not release:
        release = github.repo(
            "POST",
            "/releases",
            {
                "tag_name": tag,
                "target_commitish": source_sha,
                "name": plan["design_id"] + " native recovery repair",
                "draft": False,
                "prerelease": True,
                "body": "Original native state reused. Inspect data_contract and status before combining results.",
            },
        )
    manifest = ReleaseArchive(github, release["id"], tag=tag).publish(
        stage, attempt=tag, complete=status == "completed"
    )
    update(
        {
            "status": status,
            "phase": status,
            "archive": manifest,
            "release_url": release["html_url"],
            "current_job": current_job,
        }
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--design-id", required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"(?:development|test)-\d{4}", args.design_id):
        raise ValueError("Invalid design")
    github = Github(token=os.environ["GITHUB_TOKEN"])
    catalog = Catalog(github)
    workflow = int(os.environ["GITHUB_RUN_ID"])
    key = KEY + args.design_id

    def claim(old):
        if not old or old["status"] not in ("ready", "dispatching") or old.get("workflow_run_id"):
            raise ValueError("Duplicate repair submission")
        if old["expected_task_sha"] != os.environ["TASK_CODE_SHA"] or old["repository"] != github.repository:
            raise ValueError("Repair source or repository differs")
        return {**old, "status": "running", "phase": "restoring_native_state", "workflow_run_id": workflow}

    plan = catalog.update(key, claim)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    save(root / "request.json", plan)

    def update(changes):
        def apply(old):
            if old.get("workflow_run_id") != workflow:
                raise ValueError("Repair claim differs")
            return {**old, **changes, "updated_epoch": time.time()}

        catalog.update(key, apply)
        save(root / "status.json", {"design_id": plan["design_id"], "workflow_run_id": workflow, **changes})

    current_job = plan.get("resume_job", plan["job_name"] + "to12")
    try:
        source = plan["restart_source"]
        if source["kind"] == "artifact_stream":
            raw = restore_artifact(plan, root, github)
        elif source["kind"] == "release":
            raw = root / "raw"
            raw.mkdir()
            manifest = source["manifest"]
            wanted = [r["path"] for r in manifest["files"] if r["path"].startswith("raw/")]
            target = root / "release-source"
            target.mkdir()
            restore(manifest, target, paths=wanted, allow_incomplete=True, repository=github.repository)
            for name in wanted:
                os.link(target / name, raw / Path(name).name)
        else:
            raise ValueError("Fresh initial solves are forbidden")
        # Hash every native state used for recovery; the whole original artifact was also verified.
        mode = plan["repair_mode"]
        if mode == "resume_native_step2":
            save(root / "recovery-state-inventory.json", inventory(raw, current_job))
        elif mode == "restart_from_retained_8ms":
            save(root / "recovery-state-inventory.json", inventory(raw, plan["job_name"]))
        sys.path.insert(0, str(Path.cwd() / "scripts"))
        from native_cloud_worker import native_runtime

        from honeycomb_studio.engine import ADAPTERS

        shutil.copyfile(
            Path(__file__).with_name("native_repair_extract.py"), ADAPTERS / "native_repair_extract_98.py"
        )
        shutil.copyfile(
            Path(__file__).with_name("damage_metrics_fast.py"), ADAPTERS / "damage_metrics_fast_98.py"
        )
        adapter = ADAPTERS / "native_repair_extract_98.py"
        control = raw / "continuation_control.c"
        control.write_bytes((Path.cwd() / "scripts/linux_media/continuation_control.c").read_bytes())
        with native_runtime(root, continuation=True) as executable:

            def extract(job, step, name, full=True):
                output = root / (name + ".json")
                with (root / (name + ".log")).open("w") as log:
                    subprocess.run(
                        solver.command(
                            str(executable),
                            "python",
                            str(adapter),
                            "full" if full else "histories",
                            str(raw / (job + ".odb")),
                            str(output),
                            step,
                        ),
                        cwd=raw,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        timeout=3600,
                    )
                return json.loads(output.read_text())

            if mode == "resume_native_step2":
                audit = extract(current_job, STEP, "checkpoint-history-before-recover", full=False)
                ack = plan["retained_stop_ack"]
                if abs(audit["step_start_s"] - 0.008) > 1e-7:
                    raise ValueError("Checkpoint is not the continuation step")
                for channel in audit["histories"]:
                    values = np.asarray(channel["data"], dtype=float)
                    if (
                        len(values) < 2
                        or not np.isfinite(values).all()
                        or np.any(np.diff(values[:, 0]) <= 0)
                        or np.max(np.diff(values[:, 0])) > 3e-6
                    ):
                        raise ValueError("Retained native checkpoint history has gaps or duplicate samples")
                    if abs(values[-1, 0] - ack["simulation_time_s"]) > 3e-6:
                        raise ValueError("Checkpoint history and stop acknowledgment differ")
                save(
                    root / "checkpoint-history-verification.json",
                    {"passed": True, "channels": len(audit["histories"]), "acknowledgment": ack},
                )
                arguments = [
                    "job=" + current_job,
                    "recover",
                    "cpus=4",
                    "double=both",
                    "user=" + str(control),
                    "interactive",
                ]
            elif mode == "restart_from_retained_8ms":
                current_job = plan["job_name"] + "to12"
                deck = raw / (current_job + ".inp")
                deck.write_text(
                    restart_deck((raw / (plan["job_name"] + ".inp")).read_text(encoding="ascii")),
                    encoding="ascii",
                )
                with (root / "restart-datacheck.log").open("w") as log:
                    subprocess.run(
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
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        timeout=600,
                    )
                dat = raw / (current_job + "_check.dat")
                if not dat.exists() or "***ERROR" in dat.read_text(errors="replace").upper():
                    raise ValueError("Native restart datacheck failed")
                arguments = [
                    "job=" + current_job,
                    "input=" + deck.name,
                    "oldjob=" + plan["job_name"],
                    "cpus=4",
                    "double=both",
                    "user=" + str(control),
                    "interactive",
                ]
            else:
                arguments = None
            if arguments:
                update({"phase": "recovering_retained_state", "current_job": current_job})
                done = run_solver(
                    executable,
                    raw,
                    arguments,
                    current_job,
                    0.012,
                    root,
                    float(os.environ["HONEYCOMB_WORKFLOW_STARTED_EPOCH"]) + 240 * 60,
                    update,
                )
                if not done:
                    publish(root, raw, plan, github, "checkpoint", current_job, update)
                    return
            update({"phase": "extracting_existing_native_results", "current_job": current_job})
            native = extract(current_job, STEP, "native-continuation")
            if mode == "extract_numerical_failure_diagnostic":
                save(
                    root / "result.json",
                    {
                        "design_id": plan["design_id"],
                        "data_contract": "numerical_failure_diagnostic_only",
                        "solver_reached_12ms": False,
                        "last_native_total_time_s": 0.008 + native["last_frame_s"],
                        "not_eligible_for_complete_12ms_dataset": True,
                        "no_new_solver_started": True,
                        "original_failure": plan.get("repair_failure_reason"),
                        "damage_partial_window": native["damage"],
                    },
                )
                publish(root, raw, plan, github, "diagnosed_numerical_failure", current_job, update)
                return
            segment = segment_rows(native, plan["original_resolved"])
            original_folder = root / "original-history"
            original_folder.mkdir()
            restore(
                plan["original_8ms_manifest"],
                original_folder,
                paths=[plan["original_history_path"]],
                repository=github.repository,
            )
            original = rows(original_folder / plan["original_history_path"])
            try:
                combined, boundary = merge_rows(original, segment)
                contract = "original_8ms_prefix_preserved"
            except ValueError as error:
                if not str(error).startswith("Restart boundary differs:"):
                    raise
                update({"phase": "verifying_consistent_recovered_branch"})
                recovered = extract(plan["job_name"], "Impact", "native-recovered-prefix", full=False)
                prefix = segment_rows(recovered, plan["original_resolved"], expected_end_s=0.008)
                combined, boundary = merge_rows(prefix, segment)
                contract = "consistent_recovered_branch_original_8ms_kept_separate"
                differences = {
                    k: {
                        "original": original[-1][k],
                        "recovered": prefix[-1][k],
                        "absolute_difference": abs(original[-1][k] - prefix[-1][k]),
                    }
                    for k in ("impactor_displacement_mm", "velocity_mm_s", "CORE_ALLPD_J", "CORE_ALLDMD_J")
                }
                save(root / "original_vs_recovered_boundary.json", differences)
                save(
                    root / "branch_provenance.json",
                    {
                        "data_contract": contract,
                        "original_0to8ms_overwritten": False,
                        "boundary_tolerances_unchanged": True,
                        "recovered_prefix_from_native_odb": plan["job_name"] + ".odb",
                    },
                )
        write_rows(root / "impact_history_0to12ms.csv", combined)
        pq.write_table(
            pa.Table.from_pylist(combined), root / "impact_history_0to12ms.parquet", compression="zstd"
        )
        write_rows(
            root / "rear_fields_8to12ms.csv", [{**r, "time_s": r["time_s"] + 0.008} for r in native["rear"]]
        )
        energy = np.asarray([r["CORE_ALLPD_J"] + r["CORE_ALLDMD_J"] for r in combined])
        force = np.asarray([r["contact_force_N"] for r in combined])
        tail = np.asarray([r["time_s"] for r in combined]) >= 0.0115
        summary = {
            "design_id": plan["design_id"],
            "solver_reached_12ms": True,
            "data_contract": contract,
            "last_time_s": combined[-1]["time_s"],
            "end_impactor_velocity_m_s": combined[-1]["velocity_mm_s"] / 1000,
            "SEA_core_J_kg": float(energy[-1]) / plan["core_mass_kg"],
            "core_dissipation_J": float(energy[-1]),
            "event_finished_diagnostic": bool(
                np.max(np.abs(force[tail])) <= max(1, 0.01 * np.max(force))
                and np.ptp(energy[tail]) <= 0.01 * max(energy[-1], 1e-6)
            ),
            "boundary_absolute_errors": boundary,
            "damage_8to12ms": native["damage"],
            "new_initial_solve_started": False,
            "no_fresh_initial_solve": True,
            "native_step_start_s": native["step_start_s"],
            "original_8ms_source_unchanged": True,
            "original_prefix_rows_unchanged": len(original)
            if contract == "original_8ms_prefix_preserved"
            else 0,
            "physical_validation": "not_established",
        }
        save(root / "result.json", summary)
        update({"phase": "publishing", "summary": summary})
        publish(root, raw, plan, github, "completed", current_job, update)
    except Exception as error:
        message = re.sub(r"https?://\S+", "<redacted-url>", str(error))[:800]
        save(
            root / "failure.json",
            {"error_type": type(error).__name__, "message": message, "current_job": current_job},
        )
        update({"status": "failed", "phase": "failed", "error": message, "current_job": current_job})
        raise RuntimeError(type(error).__name__ + ": " + message) from None


if __name__ == "__main__":
    main()
