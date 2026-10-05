import pytest

from ticketpilot.budget import Budget, BudgetExceeded
from ticketpilot.llm.pricing import Usage, cost_usd
from ticketpilot.retry import (
    CircuitBreaker,
    CircuitOpen,
    RetryExhausted,
    RetryPolicy,
    TransientError,
    call_with_retry,
)


def test_retries_transient_errors_with_growing_backoff():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("503")
        return "ok"

    result, attempts = call_with_retry(flaky, RetryPolicy(max_attempts=5, base_delay=1, max_delay=100),
                                       sleep=sleeps.append)
    assert (result, attempts) == ("ok", 3)
    assert len(sleeps) == 2 and all(0 <= s <= 2 for s in sleeps)  # full jitter within 1*2^(n-1)


def test_does_not_retry_non_transient_errors():
    calls = []

    def bad():
        calls.append(1)
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        call_with_retry(bad, RetryPolicy(max_attempts=5), sleep=lambda s: None)
    assert len(calls) == 1


def test_gives_up_after_max_attempts():
    with pytest.raises(RetryExhausted) as exc:
        call_with_retry(lambda: (_ for _ in ()).throw(TransientError("down")), RetryPolicy(max_attempts=3),
                        sleep=lambda s: None)
    assert exc.value.attempts == 3


def test_respects_retry_after_hint():
    sleeps = []
    attempts = iter([TransientError("429", retry_after=7), None])

    def fn():
        err = next(attempts)
        if err:
            raise err
        return 1

    call_with_retry(fn, RetryPolicy(max_attempts=3, base_delay=0.01), sleep=sleeps.append)
    assert sleeps == [7]


def test_circuit_breaker_opens_and_half_opens():
    now = [0.0]
    cb = CircuitBreaker("x", failure_threshold=2, reset_after=10, clock=lambda: now[0])
    cb.record_failure()
    cb.record_failure()
    with pytest.raises(CircuitOpen):
        cb.before_call()
    now[0] = 11
    cb.before_call()  # half-open lets one probe through
    cb.record_success()
    assert cb.state == "closed"


def test_budget_preflight_caps_output_and_refuses_unaffordable_calls():
    b = Budget(max_cost_usd=0.01, max_seconds=60, max_steps=5, max_total_tokens=100_000)
    assert b.preflight("claude-haiku-4-5", 1000, 16000) < 16000  # shrunk to what $0.01 can buy
    with pytest.raises(BudgetExceeded):
        b.preflight("claude-opus-5-5", 5000, 16000)  # input alone costs $0.02


def test_budget_stops_on_steps_and_cost():
    b = Budget(max_cost_usd=1, max_seconds=60, max_steps=1, max_total_tokens=10**6)
    b.steps = 2
    with pytest.raises(BudgetExceeded, match="steps"):
        b.check()
    b = Budget(max_cost_usd=0.001, max_seconds=60, max_steps=9, max_total_tokens=10**6)
    b.charge("claude-opus-5-5", Usage(input_tokens=1000))
    with pytest.raises(BudgetExceeded, match="cost"):
        b.check()


def test_cache_reads_are_much_cheaper_than_fresh_input():
    fresh = cost_usd("claude-opus-5-5", Usage(input_tokens=10_000))
    cached = cost_usd("claude-opus-5-5", Usage(cache_read_input_tokens=10_000))
    assert cached == pytest.approx(fresh * 0.05)
