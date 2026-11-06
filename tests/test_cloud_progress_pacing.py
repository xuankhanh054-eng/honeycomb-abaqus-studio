import threading
import time
from types import SimpleNamespace

from honeycomb_studio import cloud_progress


class LocalStore:
    def __init__(self):
        self.batch = {}
        self.stopped = threading.Event()

    def get(self, _kind, _identity):
        return dict(self.batch)

    def save(self, _kind, batch):
        self.batch = dict(batch)
        if batch.get('stop_immediately'):
            self.stopped.set()

    def list(self, _kind, _identity):
        return []


def test_slow_remote_check_cannot_block_solver_start_or_local_checkpoint_deadline(monkeypatch):
    monkeypatch.setattr(cloud_progress, 'get_actions_log', lambda: None)
    blocked, release, returned = threading.Event(), threading.Event(), threading.Event()
    store = LocalStore()

    class Github:
        def repo(self, method, _path, _body):
            if method == 'POST':
                blocked.set()
                release.wait(3)
                return {'id': 1}

    class Catalog:
        def get(self, _key):
            return {}, None

        def update(self, _key, transform):
            return transform({})

    progress = cloud_progress.Progress(Github(), Catalog(), 'fixture', store, 'batch',
                                       time.monotonic() + .1)

    def start():
        progress.start('a' * 40)
        returned.set()

    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert blocked.wait(1)
        assert returned.wait(.3), 'Remote reporting is holding up the actual solver'
        assert store.stopped.wait(.5), 'A blocked GitHub request disabled the local checkpoint deadline'
        assert progress.stop_reason == 'timed_out'
    finally:
        release.set()
        progress.done.set()
        starter.join(4)
        progress.thread.join(4)
        if hasattr(progress, 'watchdog'):
            progress.watchdog.join(4)


def test_twenty_workers_leave_api_budget_for_archival(monkeypatch):
    now = [0.]
    requests = []
    monkeypatch.setattr(cloud_progress, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(cloud_progress, 'get_actions_log', lambda: None)

    class ClockEvent:
        def is_set(self):
            return now[0] >= 3600

        def wait(self, seconds):
            now[0] += seconds

    class Github:
        def repo(self, method, _path, _body):
            requests.append(method)
            return {'id': 1}

    class Catalog:
        def get(self, _key):
            requests.append('GET')
            return {}, None

        def update(self, _key, transform):
            requests.extend(['GET', 'PUT'])
            return transform({})

    progress = cloud_progress.Progress(Github(), Catalog(), 'fixture', LocalStore(), 'batch', 7200)
    progress.done = ClockEvent()
    progress.sha = 'a' * 40
    progress.loop()
    # GITHUB_TOKEN has 1000 requests/hour/repository; reserve 400 for claims,
    # phase changes and verified data publication across twenty concurrent jobs.
    assert 20 * len(requests) <= 600


def test_finishing_telemetry_cannot_hold_computed_bytes_before_archival(monkeypatch):
    monkeypatch.setattr(cloud_progress, 'get_actions_log', lambda: None)
    blocked, release, archived = threading.Event(), threading.Event(), threading.Event()

    class Github:
        def repo(self, method, _path, _body):
            if method == 'POST':
                blocked.set()
                release.wait(5)
                return {'id': 1}

    class Catalog:
        def get(self, _key):
            return {}, None

        def update(self, _key, transform):
            return transform({})

    progress = cloud_progress.Progress(Github(), Catalog(), 'fixture', LocalStore(), 'batch',
                                       time.monotonic() + 60)
    progress.start('a' * 40)

    def finish_and_archive():
        progress.finish('completed')
        archived.set()

    finisher = threading.Thread(target=finish_and_archive)
    try:
        assert blocked.wait(1)
        finisher.start()
        assert archived.wait(2), 'Optional status publication is blocking durable data archival'
    finally:
        release.set()
        progress.done.set()
        if finisher.ident is not None:
            finisher.join(6)
        progress.thread.join(6)
        progress.watchdog.join(6)
