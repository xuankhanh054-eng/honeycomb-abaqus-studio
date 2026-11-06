import httpx
from fastapi.testclient import TestClient

from honeycomb_studio.cloud_api import create_app
from honeycomb_studio.cloud_github import REPOSITORY, Catalog, Github


def test_activity_only_observes_known_workflows_and_builds_owned_links():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"workflow_runs": [
            {"id": 123, "path": ".github/workflows/linux-impact-7j.yml", "status": "in_progress", "html_url": "https://evil.test"},
            {"id": 456, "path": ".github/workflows/ci.yml"},
            {"id": "invalid", "path": ".github/workflows/abaqus-linux.yml"},
        ] if "in_progress" in str(request.url) else []})
    github = Github(client=httpx.Client(transport=httpx.MockTransport(handle)))
    with TestClient(create_app(github)) as client:
        value = client.get("/api/activity").json()
    assert value["available"] is True
    assert len(value["runs"]) == 1
    assert value["runs"][0]["url"] == f"https://github.com/{REPOSITORY}/actions/runs/123"
    assert value["runs"][0]["scope"] == "Thử nghiệm native 7 J"
    assert all(r.method == "GET" and "/actions/runs" in r.url.path for r in requests)


def test_activity_outage_is_separate_from_saved_jobs():
    github = Github(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with TestClient(create_app(github)) as client:
        assert client.get("/api/activity").json() == {"available": False, "runs": []}


def test_catalog_indexes_workflow_link_even_without_status_change():
    class Memory(Catalog):
        def __init__(self):
            self.data = {"jobs/one": {"id": "one", "name": "One", "profile": "research", "status": "running"}}

        def get(self, key):
            return self.data.get(key), "sha"

        def write(self, key, value, sha=None):
            self.data[key] = value

    catalog = Memory()
    catalog.update("jobs/one", lambda old: {**old, "workflow_run_id": 123})
    assert catalog.jobs()[0]["workflow_run_id"] == 123
    assert catalog.jobs()[0]["status"] == "running"
