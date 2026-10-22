"""Confluence Cloud REST publisher (API v2, plus v1 for adding labels).

Endpoints (verified against the published OpenAPI specs):
  GET  /wiki/api/v2/spaces?keys=KEY                      resolve space key -> id
  GET  /wiki/api/v2/pages?space-id=&title=&status=current find page by title
  POST /wiki/api/v2/pages                                 create
  PUT  /wiki/api/v2/pages/{id}                            update (version.number = current + 1)
  GET  /wiki/api/v2/pages/{id}/properties?key=            read docgen metadata
  POST /wiki/api/v2/pages/{id}/properties                 create metadata
  PUT  /wiki/api/v2/pages/{id}/properties/{property-id}   update metadata (version + 1)
  POST /wiki/rest/api/content/{id}/label                  add labels (v2 has no write API)

Idempotency: pages are identified by title (unique per space) and must live
under the configured parent. The metadata property (holding the cache key) is
written *after* the body, so a partial failure leads to regeneration on the
next run rather than to a stale page being treated as up to date.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from docgen.config import Settings
from docgen.publish.base import PROPERTY_KEY, PageState, PublishError, PublishResult
from docgen.retry import RetryDecision, RetryPolicy, call_with_retry

log = logging.getLogger(__name__)


class _HttpStatusError(Exception):
    def __init__(self, status: int, method: str, path: str, detail: str, retry_after: float | None):
        super().__init__(f"{method} {path} -> HTTP {status}: {detail}")
        self.status = status
        self.retry_after = retry_after


def _classify(exc: Exception) -> RetryDecision:
    if isinstance(exc, _HttpStatusError) and (exc.status == 429 or exc.status >= 500):
        return RetryDecision(True, exc.retry_after)
    if isinstance(exc, httpx.TransportError):
        return RetryDecision(True)
    return RetryDecision(False)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class RestPublisher:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Any = None,
    ) -> None:
        settings.require_publish()
        assert settings.atlassian_email and settings.atlassian_token
        self._space_key = settings.confluence_space
        self._parent_id = str(settings.confluence_parent_id)
        self._base = settings.confluence_base_url
        self._client = client or httpx.Client(timeout=settings.confluence_timeout_s)
        # Secrets are unwrapped only here, into the auth object of the HTTP client.
        self._auth = httpx.BasicAuth(
            settings.atlassian_email.get_secret_value(),
            settings.atlassian_token.get_secret_value(),
        )
        self._policy = retry_policy or RetryPolicy(max_retries=4, base_s=1.0, cap_s=30.0)
        self._sleep = sleep
        self._space_id: str | None = None

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base}{path}"

        def once() -> Any:
            resp = self._client.request(
                method,
                url,
                auth=self._auth,
                headers={"Accept": "application/json"},
                **kwargs,
            )
            if resp.status_code >= 400:
                detail = resp.text[:300].replace("\n", " ")
                raise _HttpStatusError(resp.status_code, method, path, detail, _retry_after(resp))
            return resp.json() if resp.content else None

        extra = {"sleep": self._sleep} if self._sleep else {}
        try:
            result, _ = call_with_retry(
                once, classify=_classify, policy=self._policy, what=f"{method} {path}", **extra
            )
        except _HttpStatusError as exc:
            raise PublishError(str(exc)) from None
        except httpx.HTTPError as exc:
            raise PublishError(f"{method} {path} failed: {type(exc).__name__}") from None
        return result

    # ----------------------------------------------------------------- lookups
    def _get_space_id(self) -> str:
        if self._space_id is None:
            data = self._request("GET", "/api/v2/spaces", params={"keys": self._space_key})
            results = data.get("results", [])
            if not results:
                raise PublishError(f"Confluence space with key {self._space_key!r} not found")
            self._space_id = str(results[0]["id"])
        return self._space_id

    def get_page_state(self, title: str) -> PageState | None:
        data = self._request(
            "GET",
            "/api/v2/pages",
            params={"space-id": self._get_space_id(), "title": title, "status": "current"},
        )
        results = data.get("results", [])
        if not results:
            return None
        page = results[0]
        page_id = str(page["id"])
        if str(page.get("parentId")) != self._parent_id:
            raise PublishError(
                f"A page titled {title!r} (id {page_id}) already exists outside the configured "
                f"parent page {self._parent_id}; refusing to overwrite it"
            )
        props = self._request(
            "GET", f"/api/v2/pages/{page_id}/properties", params={"key": PROPERTY_KEY}
        ).get("results", [])
        prop = props[0] if props else None
        value = prop.get("value") if prop else None
        return PageState(
            page_id=page_id,
            title=title,
            version=int(page["version"]["number"]),
            metadata={str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {},
            property_id=str(prop["id"]) if prop else None,
            property_version=int(prop["version"]["number"]) if prop else None,
        )

    # ----------------------------------------------------------------- publish
    def publish(
        self,
        *,
        title: str,
        body: str,
        metadata: Mapping[str, str],
        labels: Sequence[str],
        existing: PageState | None,
        version_message: str,
    ) -> PublishResult:
        storage = {"representation": "storage", "value": body}
        if existing is None:
            page = self._request(
                "POST",
                "/api/v2/pages",
                json={
                    "spaceId": self._get_space_id(),
                    "status": "current",
                    "title": title,
                    "parentId": self._parent_id,
                    "body": storage,
                },
            )
            action = "created"
        else:
            page = self._request(
                "PUT",
                f"/api/v2/pages/{existing.page_id}",
                json={
                    "id": existing.page_id,
                    "status": "current",
                    "title": title,
                    "parentId": self._parent_id,
                    "body": storage,
                    "version": {"number": existing.version + 1, "message": version_message},
                },
            )
            action = "updated"
        page_id = str(page["id"])

        if labels:
            self._request(
                "POST",
                f"/rest/api/content/{page_id}/label",
                json=[{"prefix": "global", "name": name} for name in labels],
            )
        self._write_metadata(page_id, dict(metadata), existing)

        webui = (page.get("_links") or {}).get("webui")
        url = f"{self._base}{webui}" if webui else None
        log.info("%s page %r (id %s)", action, title, page_id)
        return PublishResult(action=action, title=title, page_id=page_id, url=url)  # type: ignore[arg-type]

    def _write_metadata(
        self, page_id: str, metadata: dict[str, str], existing: PageState | None
    ) -> None:
        if existing is not None and existing.property_id is not None:
            self._request(
                "PUT",
                f"/api/v2/pages/{page_id}/properties/{existing.property_id}",
                json={
                    "key": PROPERTY_KEY,
                    "value": metadata,
                    "version": {"number": (existing.property_version or 0) + 1},
                },
            )
        else:
            self._request(
                "POST",
                f"/api/v2/pages/{page_id}/properties",
                json={"key": PROPERTY_KEY, "value": metadata},
            )
