"""Small GitHub App transport and compare-and-swap catalog, independent of the solver."""

from __future__ import annotations

import base64
import json
import os
import random
import re
import time
from typing import Any

import httpx

DEFAULT_REPOSITORY = "xuankhanh24/honeycomb-abaqus-studio"


def configured_repository(environment=None):
    """Keep worker catalogs and release URLs in their executing repository."""
    environment = os.environ if environment is None else environment
    value = (environment.get("HONEYCOMB_GITHUB_REPOSITORY")
             or environment.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("Invalid GitHub repository; expected owner/name")
    if value.split("/")[1] in (".", ".."):
        raise ValueError("Invalid GitHub repository name")
    return value


REPOSITORY = configured_repository()
JOB_ID = re.compile(r"^[a-f0-9]{32}$")


def rate_limit_delay(response: httpx.Response, attempt: int) -> float | None:
    """Retry only explicit throttling, never disguise a permanent permission error."""
    if response.status_code not in (403, 429):
        return None
    message = response.text.lower()
    limited = (response.status_code == 429 or 'retry-after' in response.headers
               or response.headers.get('x-ratelimit-remaining') == '0'
               or 'rate limit' in message or 'abuse detection' in message)
    if not limited:
        return None
    delay = min(60 * 2 ** attempt, 900)
    if 'retry-after' in response.headers:
        delay = max(delay, float(response.headers['retry-after']))
    if response.headers.get('x-ratelimit-remaining') == '0' and 'x-ratelimit-reset' in response.headers:
        delay = max(delay, float(response.headers['x-ratelimit-reset']) - time.time() + 1)
    return delay + random.uniform(1, 10)


def wait_for_rate_limit(response: httpx.Response, attempt: int) -> bool:
    delay = rate_limit_delay(response, attempt)
    if delay is None:
        return False
    # Leave time for an Actions runner to retain its output before the hard deadline.
    started = os.getenv('HONEYCOMB_WORKFLOW_STARTED_EPOCH')
    if started and time.time() + delay > float(started) + 330 * 60 - 90:
        return False
    try:
        from .actions_log import get_actions_log
    except ImportError:  # The lightweight web service has no runner logger.
        logger = None
    else:
        logger = get_actions_log()
    if logger:
        logger.emit('WAIT', f'GitHub giới hạn API ({response.status_code}); chờ {delay:.0f}s rồi kiểm tra lại asset trước khi tải tiếp.')
    time.sleep(delay)
    return True


def raise_github_error(response: httpx.Response):
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            reason = str(response.json().get('message', ''))[:400]
        except (ValueError, AttributeError):
            reason = ''
        details = {k: response.headers[k] for k in (
            'x-github-request-id', 'x-ratelimit-remaining', 'x-ratelimit-reset', 'retry-after') if k in response.headers}
        raise httpx.HTTPStatusError(f'{exc}; GitHub message: {reason}; diagnostics: {details}',
                                    request=exc.request, response=response) from exc


class Github:
    def __init__(self, token: str | None = None, client=None, *, repository: str | None = None):
        self.token = token
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.expiry = 0.0
        self.refresh_token = None
        self.repository = configured_repository({"GITHUB_REPOSITORY": repository}) if repository else REPOSITORY

    @property
    def configured(self) -> bool:
        return bool(
            self.token
            or all(
                os.getenv(k)
                for k in (
                    "GITHUB_APP_ID",
                    "GITHUB_APP_PRIVATE_KEY",
                    "GITHUB_APP_INSTALLATION_ID",
                )
            )
        )

    def access_token(self) -> str | None:
        if self.token and (not self.expiry or time.time() < self.expiry):
            return self.token
        if self.refresh_token:
            refreshed = self.refresh_token()
            self.token, self.expiry = refreshed.token, refreshed.expiry
            return self.token
        if not self.configured:
            return None
        import jwt

        now = int(time.time())
        signed = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": os.environ["GITHUB_APP_ID"]},
            os.environ["GITHUB_APP_PRIVATE_KEY"],
            algorithm="RS256",
        )
        response = self.client.post(
            f"https://api.github.com/app/installations/{os.environ['GITHUB_APP_INSTALLATION_ID']}/access_tokens",
            headers={"Authorization": "Bearer " + signed, "Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        self.token = response.json()["token"]
        self.expiry = time.time() + 3000
        return self.token

    def request(self, method: str, path: str, body=None, *, allow_missing=False) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = self.access_token()
        if token:
            headers["Authorization"] = "Bearer " + token
        for attempt in range(8):
            try:
                response = self.client.request(method, "https://api.github.com" + path, headers=headers, json=body)
                if attempt < 7 and wait_for_rate_limit(response, attempt):
                    continue
                if method != "GET" or response.status_code < 500 or attempt == 3:
                    break
            except httpx.TransportError:
                if method != "GET" or attempt == 3:
                    raise
            time.sleep(2 ** attempt)
        if allow_missing and response.status_code == 404:
            return None
        raise_github_error(response)
        return response.json() if response.content else None

    def repo(self, method: str, path: str, body=None, **kwargs):
        return self.request(method, f"/repos/{self.repository}" + path, body, **kwargs)


class CampaignApp:
    """Issue repository-scoped installation tokens; the private key stays in the controller."""

    def __init__(self, app_id=None, private_key=None, client=None):
        self.app_id = app_id or os.environ["CAMPAIGN_APP_ID"]
        self.private_key = private_key or os.environ["CAMPAIGN_APP_PRIVATE_KEY"]
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.clients = {}

    def repository(self, repository):
        from .campaign import OWNERS

        if repository not in {owner + "/honeycomb-abaqus-studio" for owner in OWNERS}:
            raise ValueError("Repository is outside the approved campaign")
        cached = self.clients.get(repository)
        if cached and cached.expiry > time.time() + 120:
            return cached
        import jwt

        now = int(time.time())
        signed = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id},
                            self.private_key, algorithm="RS256")
        headers = {"Authorization": "Bearer " + signed, "Accept": "application/vnd.github+json"}
        installation = self.client.get("https://api.github.com/repos/" + repository + "/installation",
                                       headers=headers)
        raise_github_error(installation)
        response = self.client.post(
            f"https://api.github.com/app/installations/{installation.json()['id']}/access_tokens",
            headers=headers, json={"repositories": [repository.split('/')[1]],
                                  "permissions": {"actions": "write", "contents": "write"}})
        raise_github_error(response)
        result = Github(response.json()["token"], self.client, repository=repository)
        result.expiry = time.time() + 3000
        result.refresh_token = lambda: self.repository(repository)
        self.clients[repository] = result
        return result


class Catalog:
    branch = "cloud-data"

    def __init__(self, github: Github):
        self.github = github

    def ensure_branch(self):
        if self.github.repo("GET", "/git/ref/heads/" + self.branch, allow_missing=True):
            return
        head = self.github.repo("GET", "/git/ref/heads/main")
        body = {"ref": "refs/heads/" + self.branch, "sha": head["object"]["sha"]}
        # Parallel Actions publishers can briefly receive a 403 while GitHub
        # serializes ref creation. Retry that transient race, but preserve a
        # real permission failure after a bounded number of attempts.
        for attempt in range(4):
            try:
                self.github.repo("POST", "/git/refs", body)
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 422:
                    return  # another publisher created the branch first
                if exc.response.status_code != 403 or attempt == 3:
                    raise
                delay = rate_limit_delay(exc.response, attempt)
                time.sleep(delay if delay is not None else min(60 * 2**attempt, 300))

    def get(self, key: str):
        response = self.github.repo(
            "GET", f"/contents/catalog/{key}.json?ref={self.branch}", allow_missing=True
        )
        if response is None:
            return None, None
        if response.get('encoding') == 'none':
            # Contents omits payloads above 1 MiB. Fetch the immutable blob
            # selected by this read so compare-and-swap keeps the same revision.
            blob = self.github.repo('GET', '/git/blobs/' + response['sha'])
            if blob.get('sha') != response['sha'] or blob.get('encoding') != 'base64':
                raise ValueError('Catalog blob identity or encoding differs')
            payload = base64.b64decode(blob['content'])
            if len(payload) != response['size']:
                raise ValueError('Catalog blob size differs')
            return json.loads(payload), response['sha']
        return json.loads(base64.b64decode(response["content"])), response["sha"]

    def write(self, key: str, value: dict, sha=None):
        self.ensure_branch()
        body = {
            "branch": self.branch,
            "message": "Update cloud catalog: " + key,
            "content": base64.b64encode(
                json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
            ).decode(),
        }
        if sha:
            body["sha"] = sha
        return self.github.repo("PUT", f"/contents/catalog/{key}.json", body)

    def update(self, key: str, transform):
        uncertain = None
        for attempt in range(10):
            value, sha = self.get(key)
            # A write can commit even when its response is lost. Confirm before
            # invoking a non-idempotent transform (for example a job claim) again.
            if uncertain is not None and value == uncertain:
                return value
            try:
                updated = transform(value)
                if updated == value:
                    return updated
                self.write(key, updated, sha)
                if key.startswith("jobs/") and any(
                    (value or {}).get(k) != updated.get(k) for k in ("status", "workflow_run_id")
                ):
                    row = {k: updated[k] for k in ("id", "name", "status", "profile")}
                    if updated.get("workflow_run_id"):
                        row["workflow_run_id"] = updated["workflow_run_id"]
                    if updated.get("parameter_title"):
                        row["parameter_title"] = updated["parameter_title"]

                    def index(old):
                        rows = (old or {}).get("jobs", [])
                        return {"jobs": [row] + [r for r in rows if r["id"] != row["id"]]}

                    try:
                        self.update("index", index)
                    except (httpx.HTTPError, RuntimeError):
                        # The job document is authoritative; an index failure must not retry a claim.
                        pass
                return updated
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (409, 422, 429) and exc.response.status_code < 500:
                    raise
                uncertain = updated if exc.response.status_code >= 500 else None
            except httpx.TransportError:
                uncertain = updated
            # Re-read the current SHA and merge sibling updates; never replay a
            # stale PUT after a conflict or an uncertain server response.
            time.sleep(min(0.25 * 2 ** attempt, 4))
        raise RuntimeError("Catalog is busy; retry without changing the request ID")

    def jobs(self):
        index, _ = self.get("index")
        return (index or {}).get("jobs", [])
