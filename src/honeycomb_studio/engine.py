from __future__ import annotations

import json
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil

from . import solver
from .data import checksum, consume_spool, history_summary, manifest, quality, verify_export
from .execution import disk_requirement, execution_plan
from .models import BatchSpec, digest
from .reports import batch_report, run_report
from .store import Store, now, write_input_csv, write_json

ADAPTERS = Path(__file__).parent / "adapters"
TERMINAL = {"completed", "failed", "review"}
ACTIVE = {
    "building",
    "checking",
    "solving",
    "extracting",
    "cataloguing",
    "verifying_export",
    "analyzing",
    "rendering",
    "reporting",
    "recovering",
}


class Interrupted(Exception):
    pass


class Detached(Exception):
    """Engine shutdown preserves the owned native process for later adoption."""


class LicenseUnavailable(Exception):
    pass


class Engine:
    def __init__(self, store: Store, abaqus="C:/SIMULIA/Commands/abaqus.bat", fake=False):
        self.store = store
        self.abaqus = abaqus
        self.fake = fake
        self.shutdown = threading.Event()
        self.runtime_fingerprint = "native-unqualified"
        self.explicit_continuation = False
        self.wake = threading.Event()
        self.guard = threading.RLock()
        self.worker = None
        self.current_run = None
        self.current_runs = set()
        self.build_guard = threading.Lock()
        self.postprocess_guard = threading.Lock()
        self.backup = None
        self.field_sink = None
        self.continuation_control: Path | None = None
        self.explicit_continuation = False
        # Process identity survives engine crashes. Never launch a replacement while
        # an owned process is still alive; worker can adopt and drain it first.
        for r in store.list("runs"):
            if r["status"] in ACTIVE:
                r["status"] = "recovering"
                self.store.save("runs", r)
                b = store.get("batches", r["batch_id"])
                b["status"] = "running"
                self.store.save("batches", b)

    def start_worker(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._loop, name="honeycomb-worker", daemon=True)
        self.worker.start()

    def control(self, bid: str, action: str):
        with self.guard:
            b = self.store.get("batches", bid)
            if action in ("start", "resume"):
                if len(self.store.list("runs", bid)) != b["runs_total"]:
                    raise ValueError(
                        "Job tạo chưa đầy đủ số lượt trong preview; tạo job mới từ cấu hình đã lưu"
                    )
                b.update(status="running", pause_requested=False, stop_immediately=False)
                b.pop("resource_error", None)
                for r in self.store.list("runs", bid):
                    if r["status"] == "interrupted":
                        r.update(status="queued", phase="queued", error=None)
                        self.store.save("runs", r)
                    elif r["status"] == "waiting_license":
                        r.update(status="queued", retry_after=0, license_retry_count=0)
                        self.store.save("runs", r)
                    elif r["status"] in ACTIVE and r["id"] not in self.current_runs:
                        r["status"] = "recovering"
                        self.store.save("runs", r)
            elif action in ("stop", "pause_after_current"):
                b.update(pause_requested=True, stop_immediately=action == "stop")
                if not any(r["status"] in ACTIVE for r in self.store.list("runs", bid)):
                    b["status"] = "paused"
            else:
                raise ValueError("Hành động không hợp lệ")
            self.store.save("batches", b)
            self.store.event(bid, None, {"type": "control", "action": action})
            self.wake.set()
            return b

    def retry(self, rid: str):
        with self.guard:
            r = self.store.get("runs", rid)
            if r["status"] in ACTIVE:
                raise ValueError("Lượt đang chạy")
            if r["status"] in ("completed", "review"):
                r["force_new_solver"] = True
            r.update(status="queued", phase="queued", error=None)
            self.store.save("runs", r)
            return self.control(r["batch_id"], "resume")

    def _phase(self, r: dict, phase: str, **extra):
        r.update(status=phase, phase=phase, **extra)
        self.store.save("runs", r)
        self.store.event(r["batch_id"], r["id"], {"type": "phase", "phase": phase})

    def reprocess(self, rid):
        with self.guard:
            r = self.store.get("runs", rid)
            if r["status"] in ACTIVE:
                raise ValueError("Lượt đang chạy")
            if not r["attempts"] or not (Path(r["attempts"][-1]["path"]) / "solver-complete.json").exists():
                raise ValueError("Chưa có solver output hoàn tất để hậu xử lý")
            r.update(status="queued", phase="queued", force_new_solver=False, error=None)
            self.store.save("runs", r)
            return self.control(r["batch_id"], "resume")

    def _loop(self):
        while not self.shutdown.is_set():
            try:
                work = False
                for batch in reversed(self.store.list("batches")):
                    if batch["status"] != "running":
                        continue
                    runs = self.store.list("runs", batch["id"])
                    if batch.get("pause_requested") and not any(r["status"] == "recovering" for r in runs):
                        batch["status"] = "paused"
                        self.store.save("batches", batch)
                        continue
                    candidates = [
                            r
                            for r in runs
                            if r["status"] in ("queued", "recovering")
                            or r["status"] == "waiting_license"
                            and r.get("retry_after", 0) <= time.time()
                    ]
                    if candidates:
                        self.execute_wave(candidates, batch)
                        work = True
                        break
                    if any(r["status"] == "waiting_license" for r in runs):
                        continue
                    batch["status"] = (
                        "completed_with_errors"
                        if any(r["status"] not in ("completed", "review") for r in runs)
                        else "completed"
                    )
                    self.store.save("batches", batch)
                    try:
                        batch_report(batch, runs)
                    except Exception as exc:
                        batch["report_error"] = str(exc)
                        self.store.save("batches", batch)
                if not work:
                    self.wake.wait(1)
                    self.wake.clear()
            except Exception as exc:
                self.store.event(None, None, {"type": "worker_error", "error": str(exc)})
                self.shutdown.wait(1)

    def execute_wave(self, candidates, batch):
        if not candidates:
            return []
        adopting = [r for r in candidates if r["status"] == "recovering" and r.get("attempts")
                    and solver.owned_tree_alive(r["attempts"][-1].get("process"))]
        if adopting:
            # These processes already exist and consume RAM. Monitor/stop them
            # even when no budget remains for launching any additional process.
            candidates = adopting
            slots = len(adopting)
        elif self.fake:
            slots = batch["spec"].get("max_parallel_runs", 2) if batch["spec"].get("execution_mode") == "parallel" else 1
        else:
            plan = execution_plan(batch["spec"], [r["resolved"] for r in candidates],
                                  psutil.cpu_count() or 1, psutil.virtual_memory().available / 1024**3,
                                  shutil.disk_usage(self.store.root).free / 1e9,
                                  linked_checkpoint=self.explicit_continuation,
                                  retained_disk_gb=[self._retained_solver_gb(r) for r in candidates])
            slots = plan["effective_parallel_runs"]
            self.store.event(batch["id"], None, {"type": "admission", **plan})
            if not slots:
                batch = self.store.get("batches", batch["id"])
                batch.update(status="paused", pause_requested=True,
                             resource_error="Không đủ CPU/RAM/đĩa cho một lượt với cấu hình đã nhập")
                self.store.save("batches", batch)
                return []
        wave = candidates[:slots]
        self.current_runs = {r["id"] for r in wave}
        self.current_run = wave[0]["id"]

        def execute(run):
            try:
                self._execute(run, batch)
            finally:
                with self.guard:
                    self.current_runs = self.current_runs - {run["id"]}
                    self.current_run = next(iter(self.current_runs), None)

        try:
            with ThreadPoolExecutor(max_workers=slots, thread_name_prefix="honeycomb-run") as pool:
                futures = [pool.submit(execute, run) for run in wave]
                for future in futures:
                    future.result()
        finally:
            self.current_runs = set()
            self.current_run = None
        return [self.store.get("runs", r["id"]) for r in wave]

    def _retained_solver_gb(self, run):
        if not self.explicit_continuation or not run.get('explicit_recover') or not run.get('attempts'):
            return 0.
        attempt = run['attempts'][-1]
        raw = Path(attempt['path']) / 'raw'
        return sum(p.stat().st_size for p in raw.glob(attempt['job_name'] + '.*')
                   if p.is_file() and not p.is_symlink()) / 1e9

    def _stopping(self, r: dict):
        b = self.store.get("batches", r["batch_id"])
        return b.get("pause_requested") and b.get("stop_immediately")

    def _runtime_memory_pressure(self, spec: dict) -> bool:
        return psutil.virtual_memory().available < spec.get("min_available_ram_gb", 2) * 1024**3

    def _monitor(self, r: dict, a: dict, phase: str, args: list[str] | None, spool: Path | None = None):
        raw = Path(a["path"]) / "raw"
        logs = Path(a["path"]) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        record = a.get("process") if args is None else None
        process = None
        if args is not None:
            if phase == 'solving' and self.continuation_control is not None:
                # Abaqus 2020 replaces every '.c' when deriving object names.
                control = raw / (a['job_name'] + '_continuation.c')
                expected = checksum(self.continuation_control)
                if control.exists() and checksum(control) != expected:
                    raise ValueError('Continuation callback changed; recover requires the original source')
                if not control.exists():
                    shutil.copy2(self.continuation_control, control)
                for marker in ('continuation.stop', 'continuation-stop-ack.json'):
                    (raw / marker).unlink(missing_ok=True)
                a['continuation_control_sha256'] = expected
                args = [*args, 'user=' + str(control)]
            if phase == "solving":
                initial = solver.sta_progress(raw / (a["job_name"] + ".sta"), r["resolved"]["duration"]) if "recover" in args else {}
                a.setdefault("phase_simulation_start_s", {})[phase] = initial.get("simulation_time_s", 0.0)
            a.setdefault("phase_started_at", {})[phase] = time.time()
            process = solver.launch(self.abaqus, args, raw, logs / (phase + ".log"))
            record = solver.owner(process)
            a["process"] = record
            a["process_phase"] = phase
            self.store.save("runs", r)
        started = time.monotonic()
        sim0 = a.get("phase_simulation_start_s", {}).get(phase, 0.0)
        previous_cpu = None
        stopped_at = None
        while solver.owned_tree_alive(record):
            if self.shutdown.is_set():
                raise Detached()
            solver.refresh_owned_tree(record)
            if (
                not self.fake
                and shutil.disk_usage(self.store.root).free / 1e9
                < self.store.get("batches", r["batch_id"])["spec"]["min_free_gb"]
            ):
                b = self.store.get("batches", r["batch_id"])
                b.update(
                    pause_requested=True,
                    stop_immediately=True,
                    resource_error="Đã chạm dung lượng dự phòng; giải phóng/chuyển dữ liệu rồi tiếp tục",
                )
                self.store.save("batches", b)
            if (not self.fake and self._runtime_memory_pressure(
                    self.store.get("batches", r["batch_id"])["spec"])):
                b = self.store.get("batches", r["batch_id"])
                b.update(pause_requested=True, stop_immediately=True,
                         resource_error="RAM khả dụng dưới mức dự phòng; dừng các lượt đang chạy để giữ dữ liệu")
                self.store.save("batches", b)
            if self._stopping(r):
                if stopped_at is None:
                    stopped_at = time.monotonic()
                    if phase == "solving":
                        if self.continuation_control is not None:
                            # The callback writes restart data and exits through
                            # Abaqus, allowing buffered history to close normally.
                            (raw / 'continuation.stop').touch()
                        else:
                            p = solver.launch(
                                self.abaqus, ["terminate", "job=" + a["job_name"]], raw, logs / "terminate.log"
                            )
                            try:
                                p.wait(timeout=10)
                            except Exception:
                                pass
                grace = 90 if self.continuation_control is not None else 15
                if phase != "solving" or time.monotonic() - stopped_at > grace:
                    solver.terminate_owned(record)
            if spool is not None:
                consume_spool(spool, self.field_sink)
            progress = (
                solver.sta_progress(raw / (a["job_name"] + ".sta"), r["resolved"]["duration"])
                if phase == "solving"
                else {}
            )
            elapsed = time.time() - a.get("phase_started_at", {}).get(
                phase, time.time() - (time.monotonic() - started)
            )
            if "simulation_time_s" in progress:
                t = progress["simulation_time_s"]
                # Recover can roll back to the last saved interval. Estimate
                # throughput only from work done during this solver launch.
                if t < sim0:
                    sim0 = t
                    a.setdefault("phase_simulation_start_s", {})[phase] = sim0
                if t > sim0:
                    progress["eta_solver_seconds"] = max(0.0, elapsed * (r["resolved"]["duration"] - t) / (t - sim0))
            progress["phase_elapsed_seconds"] = elapsed
            if record:
                try:
                    processes = [
                        psutil.Process(x["pid"])
                        for x in [record, *record.get("descendants", [])]
                        if solver.owned_alive(x)
                    ]
                    progress["process_ram_bytes"] = sum(
                        c.memory_info().rss for c in processes if c.is_running()
                    )
                    cpu = sum(c.cpu_times().user + c.cpu_times().system for c in processes if c.is_running())
                    moment = time.monotonic()
                    if previous_cpu is not None:
                        progress["process_cpu_percent_of_machine"] = max(
                            0.0,
                            100
                            * (cpu - previous_cpu[0])
                            / max(0.001, moment - previous_cpu[1])
                            / (psutil.cpu_count() or 1),
                        )
                    previous_cpu = (cpu, moment)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if spool is not None and (spool / "progress.json").exists():
                try:
                    progress.update(json.loads((spool / "progress.json").read_text()))
                    count = progress.get("frame", -1) + 1
                    total = progress.get("frames_total", 0)
                    if total:
                        progress["extraction_percent"] = 100 * count / total
                        progress["eta_extraction_seconds"] = max(
                            0.0, elapsed * (total - count) / max(count, 1)
                        )
                except (OSError, json.JSONDecodeError):
                    pass
            r["progress"] = progress
            self.store.save("runs", r)
            self.shutdown.wait(0.05 if spool is not None else 0.5)
            if self.shutdown.is_set():
                raise Detached()
        if spool is not None:
            consume_spool(spool, self.field_sink)
        if stopped_at is not None or self._stopping(r):
            if phase == 'solving' and self.continuation_control is not None:
                from .cloud_continuation import verify_solver_cleanup
                verify_solver_cleanup(logs / (phase + '.log'))
            raise Interrupted("Đã dừng; tiếp tục sẽ tạo attempt mới")
        log_text = solver.tail(logs / (phase + ".log"), 12000)
        if re.search(
            r"unable to check.?out|license.*(?:not available|unavailable|checkout failed)|FLEXnet Licensing error|licensed number.*exceeded",
            log_text,
            re.I,
        ):
            raise LicenseUnavailable(log_text[-3000:])
        if process is not None and process.wait() != 0:
            raise RuntimeError(phase + " lỗi: " + solver.tail(logs / (phase + ".log"), 5000))
        a.setdefault("durations", {})[phase] = time.time() - a.get("phase_started_at", {}).get(
            phase, time.time()
        )
        a["process"] = None
        self.store.save("runs", r)

    def _execute(self, r: dict, batch: dict):
        try:
            recovering = r["status"] == "recovering"
            previous = r["attempts"][-1] if r["attempts"] else None
            live = recovering and previous and solver.owned_tree_alive(previous.get("process"))
            if (self._stopping(r) or self.shutdown.is_set()) and not live:
                raise Interrupted("Đã dừng trước khi cấp tiến trình cho lượt")
            if recovering and previous and solver.owned_tree_alive(previous.get("process")):
                phase = previous.get("process_phase", "solving")
                # Adopted extractors share the same bounded publication sink.
                if phase in ("extracting", "rendering"):
                    with self.postprocess_guard:
                        self._monitor(r, previous, phase, None,
                                      Path(previous["path"]) / "extracted" if phase == "extracting" else None)
                else:
                    self._monitor(r, previous, phase, None)
            # Reuse an attempt only when solver success is established independently.
            solved = previous and (Path(previous["path"]) / "solver-complete.json").exists()
            if previous and not solved:
                solved = solver.sta_progress(
                    Path(previous["path"]) / "raw" / (previous["job_name"] + ".sta"),
                    r["resolved"]["duration"],
                ).get("solver_success", False)
            if r.pop("force_new_solver", False):
                solved = False
            if r.pop("explicit_recover", False):
                # Only the continuation worker sets this after verifying the
                # complete checkpoint inventory, runtime and scientific input.
                if not previous or solved or recovering:
                    raise ValueError("Invalid Explicit continuation state")
                self._phase(r, "solving")
                self._monitor(r, previous, "solving", [
                    "job=" + previous["job_name"], "recover",
                    "cpus=" + str(r["resolved"]["cpus"]), "double=both", "interactive",
                ])
                sta = solver.sta_progress(
                    Path(previous["path"]) / "raw" / (previous["job_name"] + ".sta"),
                    r["resolved"]["duration"],
                )
                if not sta.get("solver_success"):
                    raise RuntimeError("Recovered solver did not finish; inspect .sta/.log")
                write_json(Path(previous["path"]) / "solver-complete.json", {"completed_at": now(), "sta": sta})
                solved = True
            if recovering and not solved:
                raise Interrupted(
                    "Tiến trình trước đã kết thúc nhưng solver chưa hoàn tất; chọn tiếp tục để chạy lại"
                )
            if solved:
                assert previous is not None
                a = previous
                attempt = Path(a["path"])
            else:
                free = shutil.disk_usage(self.store.root).free / 1e9
                required = disk_requirement(batch['spec'], r['resolved'], linked_checkpoint=self.explicit_continuation)
                if not self.fake and free < batch["spec"]["min_free_gb"] + required:
                    b = self.store.get("batches", r["batch_id"])
                    b.update(
                        status="paused",
                        pause_requested=True,
                        resource_error=f"Thiếu dung lượng: cần reserve + khoảng {required:.1f} GB/lượt",
                    )
                    self.store.save("batches", b)
                    return
                if (
                    not self.fake
                    and psutil.virtual_memory().available
                    < batch["spec"].get("min_available_ram_gb", 2) * 1024**3
                ):
                    b = self.store.get("batches", r["batch_id"])
                    b.update(
                        status="paused",
                        pause_requested=True,
                        resource_error=f"RAM khả dụng dưới {batch['spec'].get('min_available_ram_gb', 2):g} GiB",
                    )
                    self.store.save("batches", b)
                    return
                num = len(r["attempts"]) + 1
                attempt = Path(r["path"]) / "attempts" / ("%03d" % num)
                for directory in ("raw", "extracted", "figures", "reports", "logs"):
                    (attempt / directory).mkdir(parents=True, exist_ok=True)
                a = {
                    "schema_version": "1",
                    "number": num,
                    "path": str(attempt),
                    "job_name": "hc" + r["input_hash"][:9] + "a" + str(num),
                    "started_at": now(),
                    "process": None,
                    "software": {
                        "engine_version": "0.2.0",
                        "source_sha256": {
                            p.name: checksum(p)
                            for p in [*Path(__file__).parent.glob("*.py"), *ADAPTERS.glob("*.py")]
                        },
                    },
                }
                r["attempts"].append(a)
                r["error"] = None
                for key in ("summary", "quality", "report"):
                    r.pop(key, None)
                r["test_only"] = self.fake
                cfg = {
                    **r["resolved"],
                    "explicit_continuation": getattr(self, "explicit_continuation", False),
                    "adapter_directory": str(ADAPTERS),
                    "output_directory": str(attempt / "raw"),
                    "job_name": a["job_name"],
                    "studio_software": a["software"],
                }
                cache_spec = {
                    k: v
                    for k, v in r["resolved"].items()
                    if k not in ("energy_J", "velocity_m_s", "velocity", "loading")
                }
                cache_key = digest(
                    {
                        "input": cache_spec,
                        "explicit_continuation": getattr(self, "explicit_continuation", False),
                        "runtime": self.runtime_fingerprint,
                        "builder": checksum(ADAPTERS / "build.py"),
                        "reentrant_geometry": checksum(ADAPTERS / "reentrant.py"),
                        "hexagonal_geometry": checksum(ADAPTERS / "hexagonal.py"),
                        "clone": checksum(ADAPTERS / "clone.py"),
                    }
                )
                if self.fake:
                    self._fake(r, a)
                    return
                with self.build_guard:
                    if self._stopping(r) or self.shutdown.is_set():
                        raise Interrupted("Đã dừng trong khi chờ dựng mô hình")
                    cache = self.store.root / "cache" / cache_key
                    hit = (cache / "model.cae").exists() and (cache / "model_manifest.json").exists()
                    if hit:
                        cfg.update(
                            cache_cae=str(cache / "model.cae"), cache_manifest=str(cache / "model_manifest.json")
                        )
                    config = attempt / "build-config.json"
                    write_json(config, cfg)
                    self._phase(r, "building")
                    self._monitor(
                        r,
                        a,
                        "building",
                        [
                            "cae",
                            "noGUI=" + str(ADAPTERS / ("clone.py" if hit else "build.py")),
                            "--",
                            str(config),
                        ],
                    )
                    if not (attempt / "raw" / (a["job_name"] + ".inp")).exists():
                        raise RuntimeError("Bộ dựng không tạo INP")
                    if (attempt / "raw/model_manifest.json").exists():
                        write_input_csv(
                            attempt / "actual-input.csv",
                            json.loads((attempt / "raw/model_manifest.json").read_text()),
                        )
                    self._phase(r, "checking")
                    self._monitor(
                        r,
                        a,
                        "checking",
                        [
                            "job=" + a["job_name"] + "_check",
                            "input=" + a["job_name"] + ".inp",
                            "datacheck",
                            "cpus=" + str(r["resolved"]["cpus"]),
                            "double=both",
                            "interactive",
                        ],
                    )
                    dat = solver.tail(attempt / "raw" / (a["job_name"] + "_check.dat"), 100000)
                    if "***ERROR" in dat.upper():
                        raise RuntimeError("Datacheck có lỗi; xem .dat")
                    if not hit:
                        cache.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(attempt / "raw" / (a["job_name"] + ".cae"), cache / "model.cae")
                        shutil.copy2(attempt / "raw" / "model_manifest.json", cache / "model_manifest.json")
                    a["cache_key"] = cache_key
                    a["cache_hit"] = hit
                self._phase(r, "solving")
                self._monitor(
                    r,
                    a,
                    "solving",
                    [
                        "job=" + a["job_name"],
                        "input=" + a["job_name"] + ".inp",
                        "cpus=" + str(r["resolved"]["cpus"]),
                        "double=both",
                        "interactive",
                    ],
                )
                sta = solver.sta_progress(
                    attempt / "raw" / (a["job_name"] + ".sta"), r["resolved"]["duration"]
                )
                if not sta.get("solver_success"):
                    raise RuntimeError("Solver chưa xác nhận hoàn tất thành công; xem .sta/.log")
                write_json(attempt / "solver-complete.json", {"completed_at": now(), "sta": sta})
                a["solver_progress"] = sta
            assert a is not None
            # Arrow/Matplotlib, rolling publication and shared batch reports stay
            # serialized while independent solver processes can overlap.
            with self.postprocess_guard:
                if self._stopping(r) or self.shutdown.is_set():
                    raise Interrupted("Đã dừng trong khi chờ hậu xử lý; ODB hoàn tất được giữ")
                self._postprocess(r, a, batch)
        except Detached:
            self.store.event(r["batch_id"], r["id"], {"type": "engine_detached", "process_preserved": True})
        except LicenseUnavailable as exc:
            if r["attempts"]:
                r["attempts"][-1].update(ended_at=now(), process=None, error=str(exc))
            count = r.get("license_retry_count", 0) + 1
            r.update(
                status="waiting_license",
                phase="waiting_license",
                error=str(exc),
                license_retry_count=count,
                retry_after=time.time() + 60,
            )
            self.store.save("runs", r)
            if count >= 3:
                b = self.store.get("batches", r["batch_id"])
                b.update(
                    status="paused",
                    pause_requested=True,
                    resource_error="Chưa có license sau 3 lần. Kiểm tra license rồi chọn Tiếp tục.",
                )
                self.store.save("batches", b)
            self.store.event(
                r["batch_id"], r["id"], {"type": "waiting_license", "retry_after": r["retry_after"]}
            )
        except Interrupted as exc:
            with self.guard:
                r.update(status="interrupted", phase="interrupted", error=str(exc))
                if r["attempts"]:
                    r["attempts"][-1].update(ended_at=now(), process=None, interrupted=True)
                self.store.save("runs", r)
                b = self.store.get("batches", r["batch_id"])
                b.update(status="paused", pause_requested=True)
                self.store.save("batches", b)
        except Exception as exc:
            if r["attempts"]:
                solver.terminate_owned(r["attempts"][-1].get("process"))
            r.update(status="failed", error=str(exc), failed_phase=r.get("phase"))
            if r["attempts"]:
                r["attempts"][-1]["error"] = str(exc)
                r["attempts"][-1]["ended_at"] = now()
            self.store.save("runs", r)
            self.store.event(r["batch_id"], r["id"], {"type": "error", "error": str(exc)})

    def _postprocess(self, r: dict, a: dict, batch: dict):
        sources = {
            p.name: checksum(p)
            for p in [
                Path(__file__),
                Path(__file__).with_name("data.py"),
                Path(__file__).with_name("reports.py"),
                Path(__file__).with_name("research_report.py"),
                Path(__file__).with_name("research.py"),
                ADAPTERS / "extract.py",
                ADAPTERS / "damage_metrics.py",
                ADAPTERS / "viewer.py",
                ADAPTERS / "render.py",
            ]
        }
        if not a.get("postprocess_sources") or a["postprocess_sources"][-1]["sha256"] != sources:
            a.setdefault("postprocess_sources", []).append(
                {"at": now(), "version": "0.2.0", "sha256": sources}
            )
        attempt = Path(a["path"])
        extracted = attempt / "extracted"
        odb = attempt / "raw" / (a["job_name"] + ".odb")
        config = attempt / "extract-config.json"
        full_fields = batch["spec"].get("export_all_fields", True)
        write_json(
            config,
            {
                "odb": str(odb),
                "extract_directory": str(extracted),
                "chunk_size": 48000,
                "full_fields": full_fields,
                "damage_metrics": bool(r["resolved"]["core_material"].get("johnson_cook")),
            },
        )
        write_json(extracted / "export-policy.json", {"full_fields": full_fields})
        if not (extracted / "complete.json").exists():
            if (extracted / "fields").exists() or (extracted / "reduced-fields").exists():
                history = attempt / "postprocess-history"
                history.mkdir(exist_ok=True)
                destination = history / ("extract-" + str(time.time_ns()))
                if not extracted.resolve().is_relative_to(attempt.resolve()):
                    raise ValueError("Extraction path escaped attempt")
                extracted.rename(destination)
                extracted.mkdir()
                write_json(extracted / "export-policy.json", {"full_fields": full_fields})
            spool_directory = extracted / "spool"
            if spool_directory.exists():
                for stale in spool_directory.iterdir():
                    if stale.is_file() and stale.suffix in (".npz", ".ready", ".tmp"):
                        stale.unlink()
            self._phase(r, "extracting")
            self._monitor(
                r, a, "extracting", ["python", str(ADAPTERS / "extract.py"), str(config)], spool=extracted
            )
        consume_spool(extracted, self.field_sink)
        if self.field_sink is not None:
            self.field_sink.flush()
        if not (extracted / "complete.json").exists():
            raise RuntimeError("Trích xuất ODB chưa hoàn tất")
        last_tick = [0.0]
        analysis_started = [time.monotonic()]

        def checkpoint(done, total):
            if self._stopping(r):
                raise Interrupted("Đã dừng hậu xử lý; solver output hoàn tất được giữ nguyên")
            if time.monotonic() - last_tick[0] > 0.5:
                elapsed = time.monotonic() - analysis_started[0]
                r["progress"] = {
                    "analysis_percent": 100 * done / total if total else None,
                    "phase_elapsed_seconds": elapsed,
                    "eta_analysis_seconds": elapsed * (total - done) / done if done else None,
                    "process_ram_bytes": psutil.Process().memory_info().rss,
                }
                self.store.save("runs", r)
                last_tick[0] = time.monotonic()

        self._phase(r, "verifying_export")
        verify_export(extracted, checkpoint)
        actual_path = attempt / "raw" / "model_manifest.json"
        actual = json.loads(actual_path.read_text()) if actual_path.exists() else {}
        self._phase(r, "analyzing")
        analysis_started[0] = time.monotonic()
        summary, rows = history_summary(extracted, r["resolved"], actual, checkpoint=checkpoint)
        expected = r["resolved"]["energy_J"]
        q = quality(summary)
        data_profile = batch["spec"].get("data_profile", "research")
        if data_profile == "smoke":
            r["acceptance_profile"] = "smoke"
            summary["data_profile"] = "smoke"
            q.update(ml_eligible=False, physical_validation="environment_smoke_only",
                     scope_note="Short environment smoke; not a research dataset or physical acceptance")
            write_json(extracted / "summary.json", summary)
        if abs(summary["initial_kinetic_energy_J"] - expected) / expected > 0.01:
            q["findings"].append("Động năng đầu sai khác >1% so với input")
            q.update(numerical_quality="review", ml_eligible=False)
        r.update(summary=summary, quality=q)
        write_json(extracted / "quality.json", q)
        if not self.fake and odb.is_file() and not (extracted / "field-view.json").exists():
            self._phase(r, "rendering")
            viewer_config = attempt / "viewer-config.json"
            write_json(
                viewer_config,
                {
                    "odb": str(odb),
                    "output": str(extracted / "field-view.json"),
                    "source": {
                        "run_id": r["id"],
                        "input_hash": r["input_hash"],
                        "clamp_mode": r["resolved"]["clamp_mode"],
                        "energy_J": r["resolved"]["energy_J"],
                        "runtime_fingerprint": self.runtime_fingerprint,
                        "test_only": False,
                        "data_profile": data_profile,
                    },
                },
            )
            self._monitor(r, a, "rendering", ["python", str(ADAPTERS / "viewer.py"), str(viewer_config)])
            if not (extracted / "field-view.json").exists():
                raise RuntimeError("Chưa xuất được dữ liệu hiển thị ODB")
        if batch["spec"]["render_images"] and not (attempt / "figures" / "render-manifest.json").exists():
            self._phase(r, "rendering")
            cfg = attempt / "render-config.json"
            write_json(
                cfg,
                {
                    "odb": str(odb),
                    "figures_directory": str(attempt / "figures"),
                    "summary": str(extracted / "summary.json"),
                    "compact": True,
                },
            )
            self._monitor(r, a, "rendering", ["cae", "noGUI=" + str(ADAPTERS / "render.py"), "--", str(cfg)])
            if not (attempt / "figures" / "render-manifest.json").exists():
                raise RuntimeError("Render chưa tạo manifest")
        self._phase(r, "reporting")
        report_started = time.monotonic()
        r["report"] = run_report(r, attempt, rows, checkpoint=checkpoint)
        a.setdefault("durations", {})["reporting"] = time.monotonic() - report_started
        self._phase(r, "cataloguing")
        analysis_started[0] = time.monotonic()
        manifest(
            attempt,
            {
                "run_id": r["id"],
                "input_hash": r["input_hash"],
                "extractor_version": "0.2.0",
                "software": a.get("software"),
                "postprocess_sources": a["postprocess_sources"],
            },
            checkpoint=checkpoint,
        )
        final_status = "completed" if q["ml_eligible"] else "review"
        final_run = {**r, "status": final_status, "phase": "completed", "error": None}
        batch_report(
            self.store.get("batches", r["batch_id"]),
            [final_run if item["id"] == r["id"] else item for item in self.store.list("runs", r["batch_id"])],
        )
        r.update(
            status=final_status,
            phase="completed",
            error=None,
            progress={**r.get("progress", {}), **a.get("solver_progress", {}), "solver_percent": 100.0},
        )
        a["ended_at"] = now()
        if a.get("error"):
            a.setdefault("recovered_errors", []).append({"error": a.pop("error"), "recovered_at": now()})
        a["process"] = None
        self.store.save("runs", r)
        self.store.event(r["batch_id"], r["id"], {"type": "completed", "quality": q})
        if batch["spec"].get("backup_after_run") and self.backup:
            try:
                self.backup.enqueue(r["batch_id"], r["id"])
            except Exception as exc:
                r["backup_status"] = "waiting_connection"
                self.store.save("runs", r)
                self.store.event(r["batch_id"], r["id"], {"type": "backup_error", "error": str(exc)})

    def _fake(self, r, a):
        """Explicit test-only solver; never presented as physical simulation."""
        self._phase(r, "solving")
        for i in range(20):
            if self._stopping(r):
                raise Interrupted("Test solver stopped")
            r["progress"] = {
                "solver_percent": i * 5.0,
                "simulation_time_s": r["resolved"]["duration"] * i / 20,
            }
            self.store.save("runs", r)
            time.sleep(0.05)
        r.update(
            status="completed",
            phase="completed",
            test_only=True,
            quality={"ml_eligible": False, "physical_validation": "test_solver"},
        )
        a["ended_at"] = now()
        self.store.save("runs", r)

    def import_reference(self, source: Path, model=None, render=True):
        from .models import ModelSpec

        if not source.exists() or source.suffix.lower() != ".odb":
            raise ValueError("Cần đường dẫn ODB tồn tại")
        spec = BatchSpec(
            name="Tham chiếu: " + source.stem,
            model=ModelSpec.model_validate(model or {}),
            render_images=render,
        )
        b = self.store.create(spec)
        r = self.store.list("runs", b["id"])[0]
        attempt = Path(r["path"]) / "attempts" / "001"
        for d in ("raw", "extracted", "figures", "reports", "logs"):
            (attempt / d).mkdir(parents=True, exist_ok=True)
        a = {
            "number": 1,
            "path": str(attempt),
            "job_name": "reference",
            "started_at": now(),
            "process": None,
            "imported_from": str(source.resolve()),
        }
        # An imported artifact is copied, not hard-linked, so neither backup nor
        # viewer actions can modify the source through a shared writable inode.
        shutil.copy2(source, attempt / "raw" / "reference.odb")
        for ext in (".inp", ".cae"):
            candidates = (
                [source.with_suffix(ext)]
                if source.with_suffix(ext).exists()
                else list(source.parent.glob("*" + ext))
            )
            if len(candidates) == 1:
                shutil.copy2(candidates[0], attempt / "raw" / ("reference" + ext))
        if (source.parent / "model_manifest.json").exists():
            shutil.copy2(source.parent / "model_manifest.json", attempt / "raw" / "model_manifest.json")
        write_json(attempt / "solver-complete.json", {"imported": True, "source": str(source.resolve())})
        r["attempts"] = [a]
        r["status"] = "queued"
        self.store.save("runs", r)
        return b
