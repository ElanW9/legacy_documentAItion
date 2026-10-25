import pytest

from docgen.retry import RetryDecision, RetryPolicy, call_with_retry


class Boom(Exception):
    pass


def test_retries_then_succeeds_with_backoff():
    calls, sleeps = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise Boom()
        return "ok"

    result, attempts = call_with_retry(
        fn,
        classify=lambda e: RetryDecision(True),
        policy=RetryPolicy(max_retries=5, base_s=1, cap_s=100),
        what="t",
        sleep=sleeps.append,
        rng=lambda: 1.0,
    )
    assert (result, attempts) == ("ok", 3)
    assert sleeps == [1, 2]  # base * 2**attempt with jitter factor 1.0


def test_retry_after_is_respected_and_capped():
    p = RetryPolicy(base_s=1, cap_s=30)
    assert p.delay(0, 10, lambda: 0.0) == 10
    assert p.delay(0, 999, lambda: 0.0) == 30


def test_decision_can_lower_the_retry_limit():
    sleeps: list[float] = []
    with pytest.raises(Boom):
        call_with_retry(
            lambda: (_ for _ in ()).throw(Boom()),
            classify=lambda e: RetryDecision(True, max_retries=1),
            policy=RetryPolicy(max_retries=5),
            what="t",
            sleep=sleeps.append,
        )
    assert len(sleeps) == 1


def test_min_delay_is_a_floor_not_a_cap():
    p = RetryPolicy(base_s=1, cap_s=60)
    assert p.delay(0, None, lambda: 0.0, min_delay_s=30) == 30
    assert p.delay(6, None, lambda: 1.0, min_delay_s=30) == 60  # backoff above the floor
    assert p.delay(0, 40, lambda: 0.0, min_delay_s=30) == 40


def test_gives_up_after_max_retries_and_on_non_retryable():
    sleeps = []
    with pytest.raises(Boom):
        call_with_retry(
            lambda: (_ for _ in ()).throw(Boom()),
            classify=lambda e: RetryDecision(True),
            policy=RetryPolicy(max_retries=2),
            what="t",
            sleep=sleeps.append,
        )
    assert len(sleeps) == 2
    with pytest.raises(Boom):
        call_with_retry(
            lambda: (_ for _ in ()).throw(Boom()),
            classify=lambda e: RetryDecision(False),
            policy=RetryPolicy(),
            what="t",
            sleep=sleeps.append,
        )
    assert len(sleeps) == 2
