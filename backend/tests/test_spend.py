"""Read-time pricing and the ``spend`` projection on /usage/summary."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import alita.api.server as server
from alita.core import pricing


# ─── Pricing ──────────────────────────────────────────────────────


def test_exact_model_match():
    rates, known = pricing.get_price("deepseek-chat")
    assert known is True
    assert rates == pricing.PRICES["deepseek-chat"]


def test_prefix_model_match():
    rates, known = pricing.get_price("deepseek-chat-v2")
    assert known is True
    assert rates == pricing.PRICES["deepseek-chat"]

    dated = pricing.get_price("gpt-4o-2024-08-06")
    assert dated[1] is True
    assert dated[0] == pricing.PRICES["gpt-4o"]


def test_unknown_model_is_priced_at_the_highest_rate():
    rates, known = pricing.get_price("totally-new-model")
    assert known is False
    assert rates == pricing.HIGHEST_RATES
    cost = pricing.compute_cost("totally-new-model", 1_000_000)
    assert cost == pytest.approx(pricing.HIGHEST_RATES["input"])


def test_cache_hit_math():
    cost = pricing.compute_cost(
        "deepseek-chat",
        prompt_tokens=1_000_000,
        completion_tokens=100_000,
        cached_tokens=400_000,
    )
    expected = (
        400_000 * 0.028
        + 600_000 * 0.28
        + 100_000 * 0.42
    ) / 1_000_000
    assert cost == pytest.approx(expected)


def test_prompt_minus_cached_is_clamped_at_zero():
    cost = pricing.compute_cost(
        "deepseek-chat",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        cached_tokens=2_000_000,
    )
    assert cost == pytest.approx(2_000_000 * 0.028 / 1_000_000)


def test_events_cost_sums_rows():
    events = [
        {"model": "deepseek-chat", "prompt_tokens": 1_000_000},
        {"model": "deepseek-chat", "completion_tokens": 1_000_000},
    ]
    assert pricing.events_cost(events) == pytest.approx(0.28 + 0.42)


# ─── Projection ───────────────────────────────────────────────────


def test_spend_projection_matches_a_hand_computed_value():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    events = [
        {"source": "chat", "model": "deepseek-chat", "prompt_tokens": 1_000_000,
         "created_at": "2026-11-01 10:00:00"},
        {"source": "job", "model": "deepseek-chat", "prompt_tokens": 2_000_000,
         "created_at": "2026-11-04 09:00:00"},
        # Previous month: ignored.
        {"source": "chat", "model": "deepseek-chat", "prompt_tokens": 9_000_000,
         "created_at": "2026-10-31 23:00:00"},
    ]

    spend = pricing.summarize_spend(events, now=now)

    assert spend["period"] == "2026-11"
    assert spend["currency"] == "USD"
    assert spend["pricing_known"] is True
    assert spend["month_to_date"] == pytest.approx(0.84)
    assert spend["projected_month"] == pytest.approx(0.84 / 4 * 30)
    assert spend["by_source"] == {"chat": pytest.approx(0.28), "job": pytest.approx(0.56)}
    assert spend["by_model"] == {"deepseek-chat": pytest.approx(0.84)}
    assert spend["by_day"] == [
        {"date": "2026-11-01", "cost": pytest.approx(0.28)},
        {"date": "2026-11-04", "cost": pytest.approx(0.56)},
    ]


def test_projection_clamps_days_elapsed_to_one():
    now = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    events = [{"source": "chat", "model": "deepseek-chat", "prompt_tokens": 1_000_000,
               "created_at": "2026-11-01 01:00:00"}]
    spend = pricing.summarize_spend(events, now=now)
    assert spend["month_to_date"] == pytest.approx(0.28)
    assert spend["projected_month"] == pytest.approx(0.28 / 1 * 30)


def test_unknown_model_marks_pricing_unknown():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    events = [{"source": "job", "model": "mystery-model", "prompt_tokens": 1_000_000,
               "created_at": "2026-11-02 10:00:00"}]
    spend = pricing.summarize_spend(events, now=now)
    assert spend["pricing_known"] is False
    assert spend["month_to_date"] == pytest.approx(pricing.HIGHEST_RATES["input"])


# ─── API ──────────────────────────────────────────────────────────


@pytest.fixture
def client(agent, monkeypatch):
    monkeypatch.setattr(server, "_agent", agent)
    return TestClient(server.app)


def test_api_usage_summary_includes_spend_without_removing_fields(client, agent):
    agent.sqlite.record_usage(
        session_id=None, job_id=None, source="chat", model="deepseek-chat",
        prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=0,
    )
    response = client.get("/usage/summary")
    assert response.status_code == 200
    data = response.json()

    # Existing contract is intact.
    assert data["total"]["prompt_tokens"] == 1_000_000
    assert data["total"]["runs"] == 1
    assert len(data["by_day"]) == 1
    assert data["by_model"][0]["model"] == "deepseek-chat"

    spend = data["spend"]
    assert spend["currency"] == "USD"
    assert spend["pricing_known"] is True
    assert spend["period"] == datetime.now().astimezone().strftime("%Y-%m")
    assert spend["month_to_date"] == pytest.approx(0.28)
    assert spend["by_source"] == {"chat": pytest.approx(0.28)}
    assert spend["by_model"] == {"deepseek-chat": pytest.approx(0.28)}
    assert spend["projected_month"] >= spend["month_to_date"]
