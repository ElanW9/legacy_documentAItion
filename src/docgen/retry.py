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

    def delay(
        self,
        attempt: int,
        retry_after_s: float | None,
        rng: Callable[[], float],
        min_delay_s: float | None = None,
    ) -> float:
        """Full jitter: uniform(0, min(cap, base * 2**attempt)), never below
        Retry-After (capped) or the caller's minimum delay."""
        backoff = min(self.cap_s, self.base_s * (2**attempt)) * rng()
        if retry_after_s is not None:
            backoff = max(min(retry_after_s, self.cap_s), backoff)
        if min_delay_s is not None:
            backoff = max(min_delay_s, backoff)
        return backoff


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    retry_after_s: float | None = None
    # Floor for the delay, e.g. for an overloaded service where seconds won't help.
    min_delay_s: float | None = None
    # Overrides the policy's max_retries for this kind of error.
    max_retries: int | None = None


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
            limit = policy.max_retries if decision.max_retries is None else decision.max_retries
            if not decision.retry or attempt >= limit:
                raise
            delay = policy.delay(attempt, decision.retry_after_s, rng, decision.min_delay_s)
            # Messages of our exception types carry status codes, never credentials.
            log.warning(
                "%s failed (%s: %.200s); retry %d/%d in %.1fs",
                what,
                type(exc).__name__,
                str(exc),
                attempt + 1,
                limit,
                delay,
            )
            sleep(delay)
            attempt += 1
