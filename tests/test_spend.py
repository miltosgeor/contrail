"""Tests for aggregate spend analysis.

Two things here are load-bearing beyond ordinary coverage:

- `price_breakdown` must sum to `price_tokens` for every row in the real price
  table. That is what keeps "one pricing path" a rule rather than an
  aspiration -- a second path is how the two silently diverge.
- The cache economics must flip sign when reads are rare. A model that always
  concludes "caching is paying for itself" concludes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from contrail.cost import (
    TOKEN_CLASSES,
    Price,
    PriceTable,
    Tokens,
    price_breakdown,
    price_tokens,
)
from contrail.spend import (
    BASIS_OWN_DATE,
    BASIS_UNIFORM,
    aggregate_spend,
    reprice_at_model,
)

AT = date(2026, 9, 12)
BEFORE_TABLE = date(2026, 1, 1)
SEC = 1_000_000_000
BASE_NS = 1_757_000_000 * SEC


# ------------------------------------------------------------------ helpers

@dataclass
class Rec:
    uuid: str = "u"
    ts_ns: int = 0
    model: str = "claude-opus-5"
    service_tier: str = "standard"
    agent_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    thinking_tokens: int = 0


def rec(uuid="u", *, ts_ns=0, model="claude-opus-5", service_tier="standard",
        agent_id=None, input=0, output=0, cache_read=0,
        cache_write_5m=0, cache_write_1h=0, thinking=0):
    """Build a record from the short token names the tests read best."""
    return Rec(
        uuid=uuid, ts_ns=ts_ns, model=model, service_tier=service_tier,
        agent_id=agent_id,
        input_tokens=input, output_tokens=output, cache_read_tokens=cache_read,
        cache_creation_tokens=cache_write_5m + cache_write_1h,
        cache_write_5m_tokens=cache_write_5m,
        cache_write_1h_tokens=cache_write_1h,
        thinking_tokens=thinking,
    )


def session(sid="s1", project="proj", start=None):
    return {
        "session_id": sid,
        "project_slug": project,
        "start_ns": int((start or AT).toordinal()) * 86400 * SEC,
    }


def dated_session(sid, when, project="proj"):
    """A session whose start_ns really decodes to `when` in UTC."""
    from datetime import datetime, timezone

    stamp = datetime(when.year, when.month, when.day, 12, tzinfo=timezone.utc)
    return {"session_id": sid, "project_slug": project,
            "start_ns": int(stamp.timestamp() * 1e9)}


def run(sessions, records, *, at=AT, own_date=False):
    return aggregate_spend(
        sessions, records, PriceTable(), priced_at=at, own_date=own_date
    )


# ------------------------------ the single-pricing-path guard

def test_price_breakdown_sums_to_price_tokens_for_every_real_row():
    """The rule that keeps one pricing path honest. If this fails, two code
    paths are applying rates and they have diverged."""
    tokens = Tokens(
        input=3_333, output=777, cache_read=99_999,
        cache_write_5m=4_321, cache_write_1h=8_765, thinking=555,
    )
    for price in PriceTable().prices:
        assert sum(price_breakdown(tokens, price).values()) == pytest.approx(
            price_tokens(tokens, price)
        ), f"{price.model} diverges between breakdown and total"


def test_price_breakdown_covers_exactly_the_declared_classes():
    price = PriceTable().find("claude-opus-5", AT)
    assert set(price_breakdown(Tokens(), price)) == set(TOKEN_CLASSES)


def test_thinking_tokens_are_in_no_priced_class():
    """Billed inside output; a class of their own would double-charge."""
    price = PriceTable().find("claude-opus-5", AT)
    assert sum(price_breakdown(Tokens(thinking=1_000_000), price).values()) == 0


# ------------------------------------------------------ the price basis

def test_uniform_basis_prices_a_session_older_than_the_table():
    """The deliberate inversion: shares need one basis to be comparable."""
    old = dated_session("old", BEFORE_TABLE)
    report = run([old], {"old": [rec(output=1_000_000)]})
    assert report.basis == BASIS_UNIFORM
    assert report.total_usd == pytest.approx(25.0)
    assert report.unpriced_records == 0


def test_own_date_basis_leaves_that_session_unpriced():
    """The strict view, kept available: a run older than the earliest
    confirmed price genuinely has no price we can vouch for."""
    old = dated_session("old", BEFORE_TABLE)
    report = run([old], {"old": [rec(output=1_000_000)]}, own_date=True)
    assert report.basis == BASIS_OWN_DATE
    assert report.total_usd == 0.0
    assert report.unpriced_records == 1
    assert report.unpriced_sessions == ("old",)


def test_tokens_are_counted_even_when_unpriced():
    """Tokens are the spine: always known, never unpriced."""
    old = dated_session("old", BEFORE_TABLE)
    report = run([old], {"old": [rec(output=1_000_000)]}, own_date=True)
    assert report.tokens.billable_total == 1_000_000
    assert report.by_session[0].tokens == 1_000_000


def test_ranking_is_on_tokens_not_dollars():
    sessions = [dated_session("cheap", AT), dated_session("old", BEFORE_TABLE)]
    records = {
        "cheap": [rec(output=10)],
        "old": [rec(output=5_000_000)],
    }
    report = run(sessions, records, own_date=True)
    assert [b.key for b in report.by_session] == ["old", "cheap"]


# --------------------------------------------- classes are shares of spend

def test_class_shares_are_spend_not_token_count():
    """95% of tokens being cache reads tells you nothing; the spend share is
    the number that points anywhere."""
    # 1M cache-read tokens (0.1x) against 1M output tokens (5x base).
    report = run([session()], {"s1": [rec(cache_read=1_000_000, output=1_000_000)]})
    assert report.by_class["cache_read"].tokens == report.by_class["output"].tokens
    assert report.class_share("cache_read") == pytest.approx(0.5 / 25.5, abs=1e-6)
    assert report.class_share("output") == pytest.approx(25.0 / 25.5, abs=1e-6)


def test_context_share_excludes_output_and_uncached_input():
    report = run([session()], {"s1": [
        rec(cache_read=1_000_000, cache_write_1h=1_000_000, output=1_000_000,
            input=1_000_000),
    ]})
    # reads 0.5 + 1h writes 10.0 = 10.5 of (10.5 + 25 output + 5 input) = 40.5
    assert report.context_share == pytest.approx(10.5 / 40.5, abs=1e-6)


def test_every_class_appears_even_at_zero():
    report = run([session()], {"s1": [rec(output=10)]})
    assert set(report.by_class) == set(TOKEN_CLASSES)


# --------------------------------------------------------------- breakdowns

def test_scope_splits_on_agent_id():
    report = run([session()], {"s1": [
        rec(uuid="a", output=1_000_000),
        rec(uuid="b", output=1_000_000, agent_id="ag1"),
    ]})
    scopes = {b.key: b for b in report.by_scope}
    assert scopes["main"].usd == pytest.approx(25.0)
    assert scopes["subagent"].usd == pytest.approx(25.0)


def test_model_breakdown_separates_rates():
    report = run([session()], {"s1": [
        rec(uuid="a", model="claude-opus-5", output=1_000_000),
        rec(uuid="b", model="claude-haiku-4-5", output=1_000_000),
    ]})
    by_model = {b.key: b.usd for b in report.by_model}
    assert by_model["claude-opus-5"] == pytest.approx(25.0)
    assert by_model["claude-haiku-4-5"] == pytest.approx(5.0)


def test_a_non_billable_model_is_excluded_and_counted():
    report = run([session()], {"s1": [rec(model="<synthetic>", output=1_000_000)]})
    assert report.non_billable_records == 1
    assert report.total_usd == 0.0
    assert report.tokens.billable_total == 0


def test_an_unknown_model_is_reported_not_hidden():
    report = run([session()], {"s1": [rec(model="model-from-2029", output=10)]})
    assert report.unpriced_records == 1
    assert report.unpriced_models == ("model-from-2029",)
    assert not report.is_fully_priced


def test_a_session_with_no_billable_tokens_is_omitted_from_the_ranking():
    report = run([session()], {"s1": [rec()]})
    assert report.by_session == []


# ---------------------------------------------------------- cache economics

def test_caching_pays_for_itself_when_reads_dominate():
    """One 1h write, then many reads of it -- the normal shape."""
    report = run([session()], {"s1": [
        rec(uuid="w", cache_write_1h=1_000_000),
        rec(uuid="r", cache_read=20_000_000),
    ]})
    cache = report.cache
    # premium = 1M * (10 - 5) / 1M = $5; saving = 20M * (5 - 0.5) / 1M = $90
    assert cache.write_premium_usd == pytest.approx(5.0)
    assert cache.read_saving_usd == pytest.approx(90.0)
    assert cache.net_usd == pytest.approx(85.0)
    assert cache.return_ratio == pytest.approx(18.0)
    assert cache.pays_for_itself


def test_caching_does_not_pay_when_the_cache_is_barely_read():
    """The test that makes the model falsifiable: write once, read almost
    nothing, and the premium is wasted."""
    report = run([session()], {"s1": [
        rec(uuid="w", cache_write_1h=1_000_000),
        rec(uuid="r", cache_read=100_000),
    ]})
    cache = report.cache
    assert cache.net_usd < 0
    assert not cache.pays_for_itself


def test_the_uncached_equivalent_is_actual_plus_the_net_benefit():
    report = run([session()], {"s1": [
        rec(uuid="w", cache_write_1h=1_000_000),
        rec(uuid="r", cache_read=20_000_000),
    ]})
    cache = report.cache
    assert cache.uncached_usd == pytest.approx(cache.actual_usd + cache.net_usd)
    assert cache.uncached_usd > cache.actual_usd


def test_return_ratio_is_none_with_no_write_premium():
    report = run([session()], {"s1": [rec(cache_read=1_000_000)]})
    assert report.cache.return_ratio is None


def test_write_ttls_are_tracked_apart():
    report = run([session()], {"s1": [
        rec(uuid="a", cache_write_5m=7), rec(uuid="b", cache_write_1h=11),
    ]})
    assert (report.cache.tokens_5m, report.cache.tokens_1h) == (7, 11)


# ------------------------------------------------------------------ cadence

def ticks(*seconds):
    return [rec(uuid=f"u{i}", ts_ns=BASE_NS + int(s * SEC), output=10)
            for i, s in enumerate(seconds)]


def test_cadence_measures_gaps_between_requests():
    report = run([session()], {"s1": ticks(0, 4, 8, 12)})
    assert report.cadence.gaps == 3
    assert report.cadence.median_s == pytest.approx(4.0)
    assert report.cadence.over_5m == 0
    assert report.cadence.expiry_rate == pytest.approx(0.0)


def test_a_gap_over_five_minutes_would_expire_a_short_cache():
    report = run([session()], {"s1": ticks(0, 4, 4 + 601)})
    assert report.cadence.over_5m == 1
    assert report.cadence.expiry_rate == pytest.approx(0.5)


def test_an_overnight_break_is_not_counted_as_a_gap():
    """Coming back the next day is not a pause inside a working session."""
    report = run([session()], {"s1": ticks(0, 4, 4 + 12 * 3600)})
    assert report.cadence.gaps == 1


def test_cadence_is_empty_rather_than_dividing_by_zero():
    report = run([session()], {"s1": [rec(output=10)]})
    assert report.cadence.gaps == 0
    assert report.cadence.expiry_rate is None


# ----------------------------------------------- the TTL counterfactual

def test_the_ttl_comparison_is_absent_without_one_hour_writes():
    report = run([session()], {"s1": ticks(0, 4)})
    assert report.cache.ttl_switch_usd is None


def test_the_ttl_comparison_nets_off_the_rewrites_a_short_cache_forces():
    """Gross saving would be 1M * (10 - 6.25) = $3.75. With a 50% expiry rate
    the rewrites cost 0.5 * 1M * 6.25 = $3.125, so the net is far smaller --
    and that netting is the whole reason this is a model."""
    records = [
        rec(uuid="a", ts_ns=BASE_NS, cache_write_1h=1_000_000),
        rec(uuid="b", ts_ns=BASE_NS + 601 * SEC, output=10),
    ]
    report = run([session()], {"s1": records})
    assert report.cache.ttl_switch_expiry_rate == pytest.approx(1.0)
    # gross 3.75 - rewrites 6.25 = -2.50, i.e. switching would cost more
    assert report.cache.ttl_switch_usd == pytest.approx(2.50, abs=1e-6)


def test_a_fast_cadence_makes_the_short_ttl_cheaper():
    records = [
        rec(uuid="a", ts_ns=BASE_NS, cache_write_1h=1_000_000),
        rec(uuid="b", ts_ns=BASE_NS + 4 * SEC, output=10),
    ]
    report = run([session()], {"s1": records})
    assert report.cache.ttl_switch_expiry_rate == pytest.approx(0.0)
    assert report.cache.ttl_switch_usd == pytest.approx(-3.75, abs=1e-6)


# ------------------------------------------------------------- repricing

def test_repricing_at_a_cheaper_model_is_a_saving():
    out = reprice_at_model(
        [session()], {"s1": [rec(output=1_000_000)]},
        PriceTable(), "claude-haiku-4-5", priced_at=AT,
    )
    assert out.baseline_usd == pytest.approx(25.0)
    assert out.usd == pytest.approx(5.0)
    assert out.delta_usd == pytest.approx(-20.0)
    assert out.share == pytest.approx(-0.8)


def test_repricing_at_a_dearer_model_is_a_cost():
    out = reprice_at_model(
        [session()], {"s1": [rec(model="claude-haiku-4-5", output=1_000_000)]},
        PriceTable(), "claude-opus-5", priced_at=AT,
    )
    assert out.delta_usd > 0


def test_repricing_holds_token_counts_fixed_which_is_why_it_is_a_ceiling():
    """Documented explicitly: a different model writes different amounts and
    may need more turns. The docstring has to say so."""
    from contrail.spend import Repricing
    assert "ceiling" in Repricing.__doc__
    assert "cannot say whether the work would have been done" in Repricing.__doc__


def test_repricing_counts_what_it_could_not_price():
    out = reprice_at_model(
        [session()], {"s1": [rec(model="model-from-2029", output=10)]},
        PriceTable(), "claude-opus-5", priced_at=AT,
    )
    assert out.unpriced_records == 1
    assert out.priced_records == 0


def test_repricing_an_unknown_target_prices_nothing():
    out = reprice_at_model(
        [session()], {"s1": [rec(output=10)]},
        PriceTable(), "no-such-model", priced_at=AT,
    )
    assert out.priced_records == 0
    assert out.usd == 0.0


# --------------------------------------------------------------- empty store

def test_an_empty_store_reports_zero_rather_than_failing():
    report = run([], {})
    assert report.sessions == 0
    assert report.total_usd == 0.0
    assert report.context_share is None
    assert report.class_share("output") is None


def test_a_superseded_price_row_is_respected_on_the_uniform_basis():
    """The basis date still selects a dated row; uniform means one date for
    all sessions, not no date."""
    cheap = Price("m", "standard", date(2026, 1, 1), date(2026, 6, 1),
                  1.0, 5.0, 0.1, 1.25, 2.0, "src")
    dear = Price("m", "standard", date(2026, 6, 1), None,
                 9.0, 45.0, 0.9, 11.25, 18.0, "src")
    table = PriceTable([cheap, dear])
    records = {"s1": [rec(model="m", output=1_000_000)]}
    march = aggregate_spend([session()], records, table, priced_at=date(2026, 3, 1))
    september = aggregate_spend([session()], records, table, priced_at=date(2026, 9, 1))
    assert march.total_usd == pytest.approx(5.0)
    assert september.total_usd == pytest.approx(45.0)
