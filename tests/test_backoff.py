import pytest

from cliphunter.backoff import ExponentialBackoff, backoff_delay


def test_backoff_doubles_and_caps():
    delays = [backoff_delay(attempt, base=1, cap=60, jitter=0) for attempt in range(8)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]


def test_backoff_jitter_stays_in_band():
    for seed in range(10):
        delay = backoff_delay(3, base=1, cap=60, jitter=0.2, rng=lambda seed=seed: (seed % 10) / 10)
        assert 8 * 0.8 - 1e-9 <= delay <= 8 * 1.2 + 1e-9


def test_backoff_rejects_negative_attempt():
    with pytest.raises(ValueError):
        backoff_delay(-1)


def test_healthy_attempt_resets_counter():
    now = [0.0]
    backoff = ExponentialBackoff(jitter=0, clock=lambda: now[0], healthy_after=30, rng=lambda: 0.5)
    backoff.mark_attempt_started()
    now[0] = 1
    backoff.mark_attempt_finished()
    assert backoff.attempt == 0
    assert backoff.next_delay() == 1
    assert backoff.attempt == 1
    backoff.mark_attempt_started()
    now[0] = 1
    now[0] = 40
    backoff.mark_attempt_finished()
    assert backoff.attempt == 0
    assert backoff.next_delay() == 1
