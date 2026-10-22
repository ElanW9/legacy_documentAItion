"""RestPublisher against an in-memory fake of the Confluence Cloud REST API."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

import httpx
import pytest

from docgen.config import Settings
from docgen.publish import PROPERTY_KEY, PublishError
from docgen.publish.rest import RestPublisher
from docgen.retry import RetryPolicy

TOKEN = "fake-token-for-tests-only"
TITLE = "PKG (PL/SQL package)"


@dataclass
class FakeConfluence:
    space_key: str = "DOC"
    space_id: str = "100"
    pages: dict[str, dict] = field(default_factory=dict)
    properties: dict[str, dict] = field(default_factory=dict)  # page id -> property
    labels: dict[str, set[str]] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    fail_next: list[int] = field(default_factory=list)  # status codes to return first
    next_id: int = 1000

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/wiki")
        method = request.method
        self.requests.append((method, path))
        creds = base64.b64encode(f"bot@example.com:{TOKEN}".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {creds}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"Retry-After": "0"}, json={})
        body = json.loads(request.content) if request.content else None
        q = request.url.params

        if method == "GET" and path == "/api/v2/spaces":
            hit = q.get("keys") == self.space_key
            return httpx.Response(200, json={"results": [{"id": self.space_id}] if hit else []})
        if method == "GET" and path == "/api/v2/pages":
            hits = [
                p
                for p in self.pages.values()
                if p["title"] == q.get("title") and p["spaceId"] == q.get("space-id")
            ]
            return httpx.Response(200, json={"results": hits})
        if method == "POST" and path == "/api/v2/pages":
            return self._create_page(body)
        if m := re.fullmatch(r"/api/v2/pages/(\d+)", path):
            return self._update_page(m.group(1), body)
        if m := re.fullmatch(r"/api/v2/pages/(\d+)/properties", path):
            pid = m.group(1)
            if method == "GET":
                prop = self.properties.get(pid)
                hit = prop is not None and prop["key"] == q.get("key")
                return httpx.Response(200, json={"results": [prop] if hit else []})
            self.properties[pid] = {
                "id": f"p{pid}",
                "key": body["key"],
                "value": body["value"],
                "version": {"number": 1},
            }
            return httpx.Response(200, json=self.properties[pid])
        if m := re.fullmatch(r"/api/v2/pages/(\d+)/properties/(p\d+)", path):
            prop = self.properties[m.group(1)]
            if body["version"]["number"] != prop["version"]["number"] + 1:
                return httpx.Response(409, json={"message": "version conflict"})
            prop.update(value=body["value"], version=body["version"])
            return httpx.Response(200, json=prop)
        if m := re.fullmatch(r"/rest/api/content/(\d+)/label", path):
            self.labels.setdefault(m.group(1), set()).update(lbl["name"] for lbl in body)
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={"message": f"no route {method} {path}"})

    def _create_page(self, body: dict) -> httpx.Response:
        if any(p["title"] == body["title"] for p in self.pages.values()):
            return httpx.Response(400, json={"message": "title exists"})
        pid = str(self.next_id)
        self.next_id += 1
        self.pages[pid] = {
            "id": pid,
            "title": body["title"],
            "spaceId": body["spaceId"],
            "parentId": body["parentId"],
            "body": body["body"]["value"],
            "version": {"number": 1},
            "_links": {"webui": f"/spaces/DOC/pages/{pid}"},
        }
        return httpx.Response(200, json=self.pages[pid])

    def _update_page(self, pid: str, body: dict) -> httpx.Response:
        page = self.pages[pid]
        if body["version"]["number"] != page["version"]["number"] + 1:
            return httpx.Response(409, json={"message": "version conflict"})
        page.update(title=body["title"], body=body["body"]["value"], version=body["version"])
        return httpx.Response(200, json=page)


def _settings(**overrides) -> Settings:
    values = {
        "atlassian_email": "bot@example.com",
        "atlassian_token": TOKEN,
        "confluence_site": "example.atlassian.net",
        "confluence_space": "DOC",
        "confluence_parent_id": "200",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _publisher(fake: FakeConfluence, **overrides) -> RestPublisher:
    return RestPublisher(
        _settings(**overrides),
        client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
        retry_policy=RetryPolicy(max_retries=3, base_s=0.001, cap_s=0.001),
        sleep=lambda s: None,
    )


def _publish(pub: RestPublisher, body: str, key: str):
    return pub.publish(
        title=TITLE,
        body=body,
        metadata={"cache_key": key},
        labels=["ai-generated"],
        existing=pub.get_page_state(TITLE),
        version_message="docgen test",
    )


def test_create_then_update_is_idempotent():
    fake = FakeConfluence()
    pub = _publisher(fake)
    assert pub.get_page_state(TITLE) is None

    r1 = _publish(pub, "<p>v1</p>", "k1")
    assert r1.action == "created"
    assert r1.url == "https://example.atlassian.net/wiki/spaces/DOC/pages/1000"
    state = pub.get_page_state(TITLE)
    assert state is not None and state.cache_key == "k1" and state.version == 1

    r2 = _publish(pub, "<p>v2</p>", "k2")
    assert r2.action == "updated" and r2.page_id == r1.page_id
    assert len(fake.pages) == 1
    page = fake.pages[r1.page_id]
    assert page["body"] == "<p>v2</p>" and page["version"]["number"] == 2
    assert page["parentId"] == "200"
    assert fake.properties[r1.page_id]["key"] == PROPERTY_KEY
    assert fake.properties[r1.page_id]["value"] == {"cache_key": "k2"}
    assert fake.labels[r1.page_id] == {"ai-generated"}


def test_metadata_is_written_after_body():
    fake = FakeConfluence()
    _publish(_publisher(fake), "<p>v1</p>", "k1")
    writes = [(m, p) for m, p in fake.requests if m in ("POST", "PUT")]
    assert writes[0] == ("POST", "/api/v2/pages")
    assert writes[-1][1].endswith("/properties")


def test_refuses_page_with_same_title_outside_parent():
    fake = FakeConfluence()
    fake.pages["1"] = {
        "id": "1",
        "title": TITLE,
        "spaceId": "100",
        "parentId": "999",
        "version": {"number": 4},
    }
    with pytest.raises(PublishError, match="outside the configured parent"):
        _publisher(fake).get_page_state(TITLE)


def test_retries_on_429_and_5xx():
    fake = FakeConfluence(fail_next=[429, 503])
    assert _publish(_publisher(fake), "<p>x</p>", "k").action == "created"


def test_errors_do_not_leak_credentials():
    pub = _publisher(FakeConfluence(), atlassian_token="wrong-token-value")
    with pytest.raises(PublishError) as exc:
        pub.get_page_state("x")
    msg = str(exc.value)
    assert "401" in msg
    assert "wrong-token-value" not in msg and TOKEN not in msg


def test_unknown_space_is_reported():
    with pytest.raises(PublishError, match="space with key 'NOPE' not found"):
        _publisher(FakeConfluence(), confluence_space="NOPE").get_page_state("x")
