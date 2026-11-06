"""Local progress each minute; remote reports share the repository API budget."""

import threading
import time

from .actions_log import get_actions_log, runs_markdown


def execution_deadline(workflow_started_epoch, *, wall_time=None, monotonic_time=None):
    """Reserve 15 minutes for archival within the entire 330-minute job."""
    elapsed = max(0, (time.time() if wall_time is None else wall_time) - float(workflow_started_epoch))
    remaining = 315 * 60 - elapsed
    if remaining <= 0:
        raise RuntimeError("timed_out: environment preparation consumed the execution budget")
    return (time.monotonic() if monotonic_time is None else monotonic_time) + remaining


class Progress:
    def __init__(self, github, catalog, identity, store, batch_id, deadline):
        self.github, self.catalog, self.identity = github, catalog, identity
        self.store, self.batch_id, self.deadline = store, batch_id, deadline
        self.done = threading.Event()
        self.check = None
        self.sha = None
        self.final_status = None
        self.stop_reason = None
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.watchdog = threading.Thread(target=self.check_deadline, daemon=True)

    def start(self, sha):
        self.sha = sha
        # A throttled status request must never delay the actual FE process.
        self.watchdog.start()
        self.thread.start()

    def check_deadline(self):
        while not self.done.is_set():
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.stop_solver("timed_out")
                return
            self.done.wait(min(5, remaining))

    def stop_solver(self, reason="cancelled"):
        self.stop_reason = self.stop_reason or reason
        batch = self.store.get("batches", self.batch_id)
        batch.update(pause_requested=True, stop_immediately=True)
        self.store.save("batches", batch)

    def loop(self):
        previous = None
        next_remote = 0.
        while not self.done.is_set():
            # A GitHub outage must not disable the local solver deadline.
            if time.monotonic() >= self.deadline:
                self.stop_solver("timed_out")
            try:
                runs = self.store.list("runs", self.batch_id)
                logger = get_actions_log()
                if logger:
                    logger.runs(runs)
                    logger.resources()
            except Exception:
                runs = []
            if time.monotonic() < next_remote:
                self.done.wait(60)
                continue
            # Twenty workers consume about 480 ordinary status requests/hour,
            # leaving room within GITHUB_TOKEN's 1000/repository hourly limit.
            next_remote = time.monotonic() + 300
            try:
                job, _ = self.catalog.get("jobs/" + self.identity)
                if (job or {}).get("cancel_requested"):
                    self.stop_solver()
                if self.check is None and self.sha:
                    self.check = self.github.repo("POST", "/check-runs", {
                        "name": "Honeycomb solver", "head_sha": self.sha,
                        "status": "in_progress", "external_id": self.identity,
                    })["id"]
                active = [r for r in runs if r["status"] not in ("completed", "review", "failed", "queued")]
                phase = ", ".join(sorted({r["status"] for r in active})) if active else "waiting"
                if phase != previous:
                    self.catalog.update("jobs/" + self.identity, lambda old: {**old, "phase": phase})
                    previous = phase
                self.github.repo(
                    "PATCH",
                    f"/check-runs/{self.check}",
                    {
                        "output": {
                            "title": "Phase: " + phase,
                            "summary": f"{len(active)} active runs. Progress is observed, not predicted.",
                            "text": runs_markdown(runs)[:10000],
                        }
                    },
                )
            except Exception:
                # A lost status update must not kill the actual solver or publish invented progress.
                pass
            self.done.wait(60)
        if self.final_status is not None:
            self.finish_check(self.final_status)

    def finish(self, status):
        self.final_status = status
        self.done.set()
        # Final check publication also runs in the reporter, so archival can
        # proceed while GitHub is throttling optional progress traffic.
        self.thread.join(timeout=1)
        self.watchdog.join(timeout=1)
        try:
            logger = get_actions_log()
            if logger:
                runs = self.store.list("runs", self.batch_id)
                logger.runs(runs)
                logger.emit("RESULT", f"Batch: {status} | Review yêu cầu xem chất lượng; không suy ra kiểm chứng vật lý.")
                for run in runs:
                    if run.get("error"):
                        logger.failure(run["error"], run["id"], "artifact: logs của attempt và manifest trên Releases")
        except Exception:
            pass  # Reporting must preserve finalization even if the store is unavailable.

    def finish_check(self, status):
        if self.check:
            try:
                self.github.repo(
                    "PATCH",
                    f"/check-runs/{self.check}",
                    {
                        "status": "completed",
                        "conclusion": "success"
                        if status == "completed"
                        else "neutral"
                        if status == "review"
                        else "cancelled"
                        if status == "cancelled"
                        else "timed_out"
                        if status == "timed_out"
                        else "failure",
                        "output": {
                            "title": status,
                            "summary": "See the job catalog and release manifest for retained outputs.",
                        },
                    },
                )
            except Exception:
                pass
