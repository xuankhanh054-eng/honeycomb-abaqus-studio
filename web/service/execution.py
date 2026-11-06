"""Deterministic admission: CPU/RAM/disk budgets never change requested physics."""

from .estimates import mesh_work, predict


def disk_requirement(spec, model, *, linked_checkpoint=False, retained_disk_gb=0):
    prediction = predict(model)
    if linked_checkpoint and spec.get('export_all_fields') is False:
        # Measured full-mesh probes bound native state below twice the mesh
        # complexity in GB. Publication/restore hard-link the ODB and retain
        # only one bounded 1-GiB transfer volume at a time.
        transfer_gb = 1024**3 / 1e9
        total = prediction['raw_odb_gb'] + max(4., 2 * mesh_work(model)[0]) + transfer_gb
        return max(transfer_gb, total - max(0, retained_disk_gb))
    return prediction['recommended_free_gb_above_reserve']


def execution_plan(spec, models, cpus, available_ram_gb, free_disk_gb, *, linked_checkpoint=False, retained_disk_gb=()):
    requested = spec.get("max_parallel_runs", 2) if spec.get("execution_mode", "sequential") == "parallel" else 1
    cpu_per_run = models[0]["cpus"] if models else 1
    retained = list(retained_disk_gb) if retained_disk_gb else [0] * len(models)
    if len(retained) != len(models):
        raise ValueError('Retained disk inventory must match the candidate runs')
    disk_per_run = disk_requirement(spec, models[0], linked_checkpoint=linked_checkpoint,
                                    retained_disk_gb=retained[0]) if models else 0
    ram_per_run = spec.get("ram_per_run_gb", 4)
    limits = {
        "requested": requested,
        "cpu": int(cpus // cpu_per_run),
        "ram": int(max(0, available_ram_gb - spec.get("min_available_ram_gb", 2)) // ram_per_run),
        "disk": int(max(0, free_disk_gb - spec.get("min_free_gb", 20)) // disk_per_run) if disk_per_run else requested,
    }
    cpu_left = cpus
    ram_left = max(0, available_ram_gb - spec.get("min_available_ram_gb", 2))
    disk_left = max(0, free_disk_gb - spec.get("min_free_gb", 20))
    slots = 0
    for model, existing in zip(models[:requested], retained[:requested], strict=True):
        disk = disk_requirement(spec, model, linked_checkpoint=linked_checkpoint, retained_disk_gb=existing)
        if model["cpus"] > cpu_left or ram_per_run > ram_left or disk > disk_left:
            break
        slots += 1
        cpu_left -= model["cpus"]
        ram_left -= ram_per_run
        disk_left -= disk
    # Timing is an estimate; parallel wall time is bounded below by solver work
    # and shared build/postprocess work, which stay serialized for reproducibility.
    predictions = [predict(m) for m in models]
    serial = sum(p["solver_minutes"] + sum(p["other_phase_minutes"].values()) for p in predictions)
    return {"requested_parallel_runs": requested, "effective_parallel_runs": slots,
            "cpu_per_run": cpu_per_run, "limits": limits, "ram_per_run_gb": ram_per_run,
            "disk_per_run_gb": disk_per_run, "estimated_serial_minutes": serial,
            "disk_policy": "linked-checkpoint" if linked_checkpoint and spec.get('export_all_fields') is False else "full-export",
            "optimistic_solver_parallel_minutes": sum(p["solver_minutes"] for p in predictions) / max(1, slots),
            "estimate_only": True,
            "note": "Tài nguyên web là số đo tham khảo; worker kiểm tra lại. Không tự đổi CPU/lưới/thời gian vật lý."}
