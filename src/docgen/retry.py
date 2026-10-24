"""Exponential backoff with full jitter, shared by the LLM and Confluence clients."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_s: float = 2.0
    cap_s: float = 60.0

    def delay(self, attempt: int, retry_after_s: float | None, rng: Callable[[], float]) -> float:
        """Full jitter: uniform(0, min(cap, base * 2**attempt)), never below Retry-After."""
        backoff = min(self.cap_s, self.base_s * (2**attempt)) * rng()
        if retry_after_s is not None:
            return max(min(retry_after_s, self.cap_s), backoff)
        return backoff


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    retry_after_s: float | None = None


def call_with_retry[T](
    fn: Callable[[], T],
    *,
    classify: Callable[[Exception], RetryDecision],
    policy: RetryPolicy,
    what: str,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> tuple[T, int]:
    """Run `fn`, retrying retryable exceptions. Returns (result, attempts)."""
    attempt = 0
    while True:
        try:
            return fn(), attempt + 1
        except Exception as exc:
            decision = classify(exc)
            if not decision.retry or attempt >= policy.max_retries:
                raise
            delay = policy.delay(attempt, decision.retry_after_s, rng)
            log.warning(
                "%s failed (%s); retry %d/%d in %.1fs",
                what,
                type(exc).__name__,
                attempt + 1,
                policy.max_retries,
                delay,
            )
            sleep(delay)
            attempt += 1
