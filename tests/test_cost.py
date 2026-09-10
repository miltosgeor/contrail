"""Tests for cost attribution.

Two constraints from docs/spec.md drive most of these:

- Tokens are the stored truth; no dollar figure is ever persisted. There is a
  test that walks the whole database schema to enforce it.
- An unpriced model yields None and a reported count, never 0.0. A silent
  zero is the failure mode this project has already been bitten by once.

Fixtures are synthetic. Prices used in assertions are the real table, because
the ratio invariant is a property of the real figures and testing it against
made-up ones would prove nothing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from contrail.cost import (
    EXPECTED_RATIOS,
    MTOK,
    NON_BILLABLE_MODELS,
    Price,
    PriceTable,
    Tokens,
    attribute_cost,
    check_against_counter,
    check_attribution_invariant,
    check_cross_source,
    load_prices,
    normalise_model,
    price_tokens,
)
from contrail.store import Store

CONFIRMED_ON = date(2026, 9, 10)
LATER = date(2027, 3, 1)
EARLIER = date(2026, 1, 1)


# ------------------------------------------------------------------ helpers

def rec(uuid, model="claude-opus-5", *, input=0, output=0, cache_read=0,
        w5m=0, w1h=0, thinking=0, tier="standard", request_id=None,
        agent_id=None):
    """A minimal object shaped like a TranscriptRecord."""
    return type("R", (), {
        "uuid": uuid, "model": model, "service_tier": tier,
        "input_tokens": input, "output_tokens": output,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": w5m + w1h,
        "cache_write_5m_tokens": w5m, "cache_write_1h_tokens": w1h,
        "thinking_tokens": thinking, "request_id": request_id,
        "agent_id": agent_id,
    })()


def node(node_id, kind, parent=None, *, label="", depth=0, input=0, output=0,
         cache_read=0, w5m=0, w1h=0, thinking=0):
    return type("N", (), {
        "node_id": node_id, "kind": kind, "label": label or node_id,
        "parent_node_id": parent, "depth": depth,
        "input_tokens": input, "output_tokens": output,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": w5m + w1h,
        "cache_write_5m_tokens": w5m, "cache_write_1h_tokens": w1h,
        "thinking_tokens": thinking,
    })()


@pytest.fixture()
def prices():
    return PriceTable()


# ------------------------------------------------------- the price table

def test_every_row_holds_the_expected_ratios(prices):
    """A typo in a future row must fail loudly, not misprice runs quietly.

    Every model prices cache read at 0.1x base input, a 5-minute-TTL cache
    write at 1.25x, a 1-hour write at 2x, and output at 5x.
    """
    for price in prices.prices:
        actual = price.ratios()
        for name, expected in EXPECTED_RATIOS.items():
            assert actual[name] == pytest.approx(expected), (
                f"{price.model}: {name} is {actual[name]}x base input, "
                f"expected {expected}x"
            )


def test_every_row_cites_a_source_and_a_fetch_date(prices):
    for price in prices.prices:
        assert price.source, f"{price.model} has no source"
        assert "http" in price.source, f"{price.model} source is not a URL"
        assert "fetched" in price.source, f"{price.model} source has no fetch date"


def test_no_row_claims_an_effective_date_we_cannot_vouch_for(prices):
    """effective_from is when the figure was confirmed, not an invented date.

    We do not know when these prices actually took effect, and back-dating
    them would be a lie in a file that looks authoritative.
    """
    for price in prices.prices:
        assert price.effective_from >= CONFIRMED_ON, (
            f"{price.model} claims to be effective from {price.effective_from}, "
            "which predates the date the figure was confirmed"
        )


def test_the_table_covers_every_model_seen_in_the_corpus(prices):
    for model in ("claude-opus-5", "claude-opus-4-8", "claude-sonnet-4-6",
                  "claude-haiku-4-5"):
        assert prices.find(model, CONFIRMED_ON) is not None, f"{model} unpriced"


def test_prices_json_is_loadable_data_not_code():
    loaded = load_prices()
    assert loaded, "price table is empty"
    assert all(isinstance(p, Price) for p in loaded)


# ------------------------------------------------------------ dated lookup

def test_a_run_older_than_the_earliest_price_is_unpriced(prices):
    """We do not know the rate on that date, so we decline to guess."""
    assert prices.find("claude-opus-5", EARLIER) is None


def test_a_run_after_the_effective_date_is_priced(prices):
    assert prices.find("claude-opus-5", LATER) is not None


def test_a_superseding_row_wins_for_later_runs():
    old = Price("m", "standard", date(2026, 1, 1), date(2026, 6, 1),
                1.0, 5.0, 0.1, 1.25, 2.0, "src")
    new = Price("m", "standard", date(2026, 6, 1), None,
                2.0, 10.0, 0.2, 2.5, 4.0, "src")
    table = PriceTable([old, new])
    assert table.find("m", date(2026, 3, 1)).input_usd_per_mtok == 1.0
    assert table.find("m", date(2026, 9, 1)).input_usd_per_mtok == 2.0


def test_a_march_run_stays_costed_at_march_prices():
    """The whole point of dating the table: history must not move."""
    old = Price("m", "standard", date(2026, 1, 1), date(2026, 6, 1),
                1.0, 5.0, 0.1, 1.25, 2.0, "src")
    new = Price("m", "standard", date(2026, 6, 1), None,
                9.0, 45.0, 0.9, 11.25, 18.0, "src")
    table = PriceTable([old, new])
    tokens = Tokens(input=MTOK)
    march = price_tokens(tokens, table.find("m", date(2026, 3, 1)))
    september = price_tokens(tokens, table.find("m", date(2026, 9, 1)))
    assert march == pytest.approx(1.0)
    assert september == pytest.approx(9.0)


def test_an_unknown_model_is_unpriced_not_free(prices):
    assert prices.find("some-model-from-2028", CONFIRMED_ON) is None


def test_an_unknown_service_tier_is_unpriced(prices):
    """Tier is a price dimension; a batch run must not silently use standard."""
    assert prices.find("claude-opus-5", CONFIRMED_ON, "batch") is None


# --------------------------------------------------------- model normalising

def test_long_context_variant_normalises_to_the_base_model():
    """The metrics stream says claude-opus-5[1m]; the transcript says
    claude-opus-5. Measured: pricing the variant at base rates agreed with
    Claude Code's own counter to six decimal places on a real run."""
    assert normalise_model("claude-opus-5[1m]") == "claude-opus-5"


def test_dated_model_id_normalises_to_the_family():
    assert normalise_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"


def test_normalising_tolerates_absent_and_blank(prices):
    assert normalise_model(None) is None
    assert prices.find(None, CONFIRMED_ON) is None


def test_synthetic_model_is_never_priced(prices):
    assert "<synthetic>" in NON_BILLABLE_MODELS
    assert prices.find("<synthetic>", CONFIRMED_ON) is None


# ------------------------------------------------------------------ pricing

def test_each_category_is_charged_at_its_own_rate(prices):
    price = prices.find("claude-opus-5", CONFIRMED_ON)
    assert price_tokens(Tokens(input=MTOK), price) == pytest.approx(5.0)
    assert price_tokens(Tokens(output=MTOK), price) == pytest.approx(25.0)
    assert price_tokens(Tokens(cache_read=MTOK), price) == pytest.approx(0.5)
    assert price_tokens(Tokens(cache_write_5m=MTOK), price) == pytest.approx(6.25)
    assert price_tokens(Tokens(cache_write_1h=MTOK), price) == pytest.approx(10.0)


def test_the_two_cache_write_ttls_price_differently(prices):
    """Collapsing them, as the earlier schema did, makes pricing wrong."""
    price = prices.find("claude-opus-5", CONFIRMED_ON)
    five_min = price_tokens(Tokens(cache_write_5m=MTOK), price)
    one_hour = price_tokens(Tokens(cache_write_1h=MTOK), price)
    assert one_hour > five_min
    assert one_hour / five_min == pytest.approx(1.6)


def test_thinking_tokens_are_never_charged(prices):
    """They are billed inside output_tokens; charging them double-counts."""
    price = prices.find("claude-opus-5", CONFIRMED_ON)
    without = price_tokens(Tokens(output=1000), price)
    with_thinking = price_tokens(Tokens(output=1000, thinking=900), price)
    assert without == with_thinking


def test_thinking_tokens_are_excluded_from_the_billable_total():
    assert Tokens(output=10, thinking=8).billable_total == 10


def test_a_total_only_cache_source_is_charged_at_the_cheaper_rate(prices):
    """An OTel span carries only the cache-creation total, with no TTL. An
    unknown TTL must not silently inflate a cost figure."""
    tokens = Tokens.from_obj({"cache_creation_tokens": MTOK})
    assert tokens.cache_write_5m == MTOK
    assert tokens.cache_write_1h == 0
    price = prices.find("claude-opus-5", CONFIRMED_ON)
    assert price_tokens(tokens, price) == pytest.approx(6.25)


def test_the_ttl_split_is_preferred_when_present():
    tokens = Tokens.from_obj({
        "cache_creation_tokens": 100,
        "cache_write_5m_tokens": 30,
        "cache_write_1h_tokens": 70,
    })
    assert (tokens.cache_write_5m, tokens.cache_write_1h) == (30, 70)


# -------------------------------------------------------------- attribution

def simple_tree():
    """session -> turn -> [tool, subagent]"""
    return [
        node("session:s", "session"),
        node("turn:t1", "turn", "session:s", depth=1, input=100),
        node("tool:x", "tool", "turn:t1", depth=2),
        node("agent:a1", "subagent", "turn:t1", depth=2, output=1000),
    ]


def test_self_cost_lands_on_the_node_that_owns_the_record(prices):
    records = [rec("r1", output=MTOK)]
    run = attribute_cost(simple_tree(), records, {"r1": "agent:a1"},
                         prices, CONFIRMED_ON, "s")
    assert run.nodes["agent:a1"].self_usd == pytest.approx(25.0)
    assert run.nodes["turn:t1"].self_usd == 0.0


def test_a_subagents_cost_rolls_up_into_its_ancestors(prices):
    records = [rec("r1", output=MTOK)]
    run = attribute_cost(simple_tree(), records, {"r1": "agent:a1"},
                         prices, CONFIRMED_ON, "s")
    assert run.nodes["agent:a1"].total_usd == pytest.approx(25.0)
    assert run.nodes["turn:t1"].total_usd == pytest.approx(25.0)
    assert run.total_usd == pytest.approx(25.0)


def test_a_turn_keeps_its_own_cost_separate_from_its_subagents(prices):
    """This is the distinction that makes '$1.90 of it was one subagent' true."""
    records = [rec("r1", input=MTOK), rec("r2", output=MTOK)]
    run = attribute_cost(simple_tree(), records,
                         {"r1": "turn:t1", "r2": "agent:a1"},
                         prices, CONFIRMED_ON, "s")
    turn = run.nodes["turn:t1"]
    assert turn.self_usd == pytest.approx(5.0)
    assert turn.total_usd == pytest.approx(30.0)
    assert run.nodes["agent:a1"].total_usd == pytest.approx(25.0)


def test_records_on_different_models_in_one_node_are_priced_separately(prices):
    """A node's records can span models; pricing once per node would be wrong."""
    records = [
        rec("r1", "claude-opus-5", output=MTOK),
        rec("r2", "claude-haiku-4-5", output=MTOK),
    ]
    run = attribute_cost(simple_tree(), records,
                         {"r1": "agent:a1", "r2": "agent:a1"},
                         prices, CONFIRMED_ON, "s")
    assert run.nodes["agent:a1"].self_usd == pytest.approx(25.0 + 5.0)
    assert set(run.nodes["agent:a1"].models) == {"claude-opus-5", "claude-haiku-4-5"}


def test_an_unpriced_model_yields_none_and_a_count_never_zero(prices):
    records = [rec("r1", "model-from-2029", output=MTOK)]
    run = attribute_cost(simple_tree(), records, {"r1": "agent:a1"},
                         prices, CONFIRMED_ON, "s")
    assert run.nodes["agent:a1"].self_usd is None
    assert run.unpriced_records == 1
    assert run.unpriced_models == ("model-from-2029",)
    assert not run.is_fully_priced


def test_a_node_with_no_billable_tokens_is_free_not_unpriced(prices):
    """A tool node costs nothing. Free and unpriced must not look alike."""
    run = attribute_cost(simple_tree(), [], {}, prices, CONFIRMED_ON, "s")
    assert run.nodes["tool:x"].self_usd == 0.0
    assert run.nodes["tool:x"].total_usd == 0.0


def test_a_wholly_unpriced_run_totals_none_not_zero(prices):
    records = [rec("r1", "model-from-2029", output=MTOK)]
    run = attribute_cost(
        [node("session:s", "session"), node("agent:a1", "subagent", "session:s")],
        records, {"r1": "agent:a1"}, prices, CONFIRMED_ON, "s",
    )
    assert run.total_usd is None


def test_a_partly_unpriced_run_reports_a_floor_and_a_count(prices):
    records = [rec("r1", output=MTOK), rec("r2", "model-from-2029", output=MTOK)]
    run = attribute_cost(simple_tree(), records,
                         {"r1": "agent:a1", "r2": "turn:t1"},
                         prices, CONFIRMED_ON, "s")
    assert run.total_usd == pytest.approx(25.0)
    assert run.unpriced_records == 1


def test_a_non_billable_model_is_excluded_not_priced_at_zero(prices):
    records = [rec("r1", "<synthetic>", output=MTOK)]
    run = attribute_cost(simple_tree(), records, {"r1": "agent:a1"},
                         prices, CONFIRMED_ON, "s")
    assert run.non_billable_records == 1
    assert run.unpriced_records == 0, "excluded, not counted as unpriced"
    assert run.total_usd == 0.0
    assert any("non-billable" in w for w in run.warnings)


def test_a_record_matching_no_node_is_counted_not_silently_dropped(prices):
    records = [rec("orphan", output=MTOK)]
    run = attribute_cost(simple_tree(), records, {}, prices, CONFIRMED_ON, "s")
    assert run.unattributed_records == 1
    assert any("matched no tree node" in w for w in run.warnings)


def test_by_kind_ranks_the_most_expensive_first(prices):
    nodes = simple_tree() + [node("agent:a2", "subagent", "turn:t1", depth=2)]
    records = [rec("r1", output=MTOK), rec("r2", output=100)]
    run = attribute_cost(nodes, records, {"r1": "agent:a2", "r2": "agent:a1"},
                         prices, CONFIRMED_ON, "s")
    ranked = run.by_kind("subagent")
    assert [n.node_id for n in ranked] == ["agent:a2", "agent:a1"]


def test_a_cyclic_parent_chain_does_not_hang_the_rollup(prices):
    """Phase 2 hit a real parent cycle from colliding node ids."""
    nodes = [node("a", "turn", "b"), node("b", "turn", "a")]
    run = attribute_cost(nodes, [], {}, prices, CONFIRMED_ON, "s")
    assert set(run.nodes) == {"a", "b"}


# ----------------------------------------------------- Layer 1: invariant

def test_the_attribution_invariant_holds_on_a_consistent_tree(prices):
    records = [rec("r1", output=1000, input=50)]
    nodes = [node("session:s", "session"),
             node("agent:a1", "subagent", "session:s", output=1000, input=50)]
    run = attribute_cost(nodes, records, {"r1": "agent:a1"}, prices,
                         CONFIRMED_ON, "s")
    result = check_attribution_invariant(run, records, {"r1": "agent:a1"})
    assert result.ok
    assert result.detail["records_attributed"] == 1


def test_the_invariant_catches_a_double_count(prices):
    """Phase 2 had exactly this bug: records folded in twice."""
    records = [rec("r1", output=1000)]
    nodes = [node("session:s", "session"),
             node("agent:a1", "subagent", "session:s", output=2000)]
    run = attribute_cost(nodes, records, {"r1": "agent:a1"}, prices,
                         CONFIRMED_ON, "s")
    result = check_attribution_invariant(run, records, {"r1": "agent:a1"})
    assert not result.ok
    assert result.detail["diffs"]["output"] == 1000


def test_the_invariant_catches_a_dropped_record(prices):
    records = [rec("r1", output=1000)]
    nodes = [node("session:s", "session"),
             node("agent:a1", "subagent", "session:s", output=0)]
    run = attribute_cost(nodes, records, {"r1": "agent:a1"}, prices,
                         CONFIRMED_ON, "s")
    assert not check_attribution_invariant(run, records, {"r1": "agent:a1"}).ok


def test_the_invariant_is_about_tokens_so_it_holds_when_nothing_is_priced(prices):
    """It must not depend on the price table being able to price the run."""
    records = [rec("r1", "model-from-2029", output=1000)]
    nodes = [node("session:s", "session"),
             node("agent:a1", "subagent", "session:s", output=1000)]
    run = attribute_cost(nodes, records, {"r1": "agent:a1"}, prices,
                         CONFIRMED_ON, "s")
    assert run.total_usd is None
    assert check_attribution_invariant(run, records, {"r1": "agent:a1"}).ok


# ------------------------------------------------ Layer 2: cross-source

def span(request_id, input=0, output=0, cache_read=0, creation=0):
    return {"attributes": json.dumps({
        "request_id": request_id, "input_tokens": input,
        "output_tokens": output, "cache_read_tokens": cache_read,
        "cache_creation_tokens": creation,
    })}


def test_cross_source_agrees_when_both_paths_report_the_same(prices):
    records = [rec("r1", input=2, output=1200, cache_read=84562, w1h=7096,
                   request_id="req_1")]
    spans = [span("req_1", input=2, output=1200, cache_read=84562, creation=7096)]
    result = check_cross_source(records, spans)
    assert result.ok
    assert result.detail["matched"] == 1


def test_cross_source_detects_a_disagreement(prices):
    records = [rec("r1", output=1200, request_id="req_1")]
    spans = [span("req_1", output=999)]
    result = check_cross_source(records, spans)
    assert not result.ok
    assert result.detail["disagreeing_request_ids"] == ["req_1"]


def test_cross_source_compares_the_cache_total_since_spans_lack_the_ttl(prices):
    """Spans carry only cache_creation_tokens, with no TTL breakdown."""
    records = [rec("r1", w5m=96, w1h=7000, request_id="req_1")]
    spans = [span("req_1", creation=7096)]
    assert check_cross_source(records, spans).ok


def test_cross_source_reports_transcript_only_calls_without_failing(prices):
    """Export can start mid-session, so this is normal, not an error."""
    records = [rec("r1", output=5, request_id="req_1"),
               rec("r2", output=5, request_id="req_2")]
    spans = [span("req_1", output=5)]
    result = check_cross_source(records, spans)
    assert result.ok
    assert result.detail["transcript_only"] == 1


def test_cross_source_with_no_overlap_is_not_a_pass(prices):
    result = check_cross_source([rec("r1", request_id="req_1")], [])
    assert not result.ok
    assert any("no overlap" in n for n in result.notes)


# --------------------------------------------- Layer 3: vs the CLI counter

def counter_run(prices, usd_records):
    records = [rec("r1", output=usd_records)]
    nodes = [node("session:s", "session"),
             node("agent:a1", "subagent", "session:s", output=usd_records)]
    return attribute_cost(nodes, records, {"r1": "agent:a1"}, prices,
                          CONFIRMED_ON, "s")


def test_layer3_always_states_it_is_not_ground_truth(prices):
    run = counter_run(prices, MTOK)
    result = check_against_counter(run, {"claude-opus-5[1m]": 25.0})
    assert any("not a billing figure" in n for n in result.notes)
    assert "price table" in result.proves


def test_layer3_passes_within_tolerance(prices):
    run = counter_run(prices, MTOK)          # $25.00
    result = check_against_counter(run, {"claude-opus-5[1m]": 25.1})
    assert result.ok


def test_layer3_fails_outside_tolerance(prices):
    run = counter_run(prices, MTOK)          # $25.00
    result = check_against_counter(run, {"claude-opus-5[1m]": 40.0})
    assert not result.ok


def test_layer3_explains_the_expected_direction_of_a_shortfall(prices):
    """Auxiliary calls are billed by the counter but absent from transcripts."""
    run = counter_run(prices, MTOK)
    result = check_against_counter(run, {"claude-opus-5[1m]": 25.0,
                                         "claude-haiku-4-5-20251001": 0.02})
    assert any("auxiliary" in n for n in result.notes)
    assert result.detail["delta_usd"] < 0


def test_layer3_on_a_real_captured_counter(prices):
    """A real spike capture: one Opus call plus one auxiliary Haiku call.

    Our computed figure matched the counter's Opus figure exactly, and the
    whole delta was the auxiliary Haiku call the transcript never records.
    """
    records = [rec("r1", input=2, output=4, cache_read=28522, w1h=11074)]
    nodes = [node("session:s", "session"),
             node("agent:a1", "subagent", "session:s",
                  input=2, output=4, cache_read=28522, w1h=11074)]
    run = attribute_cost(nodes, records, {"r1": "agent:a1"}, prices,
                         CONFIRMED_ON, "s")
    assert run.total_usd == pytest.approx(0.125111, abs=5e-7)

    result = check_against_counter(run, {
        "claude-opus-5[1m]": 0.125111,
        "claude-haiku-4-5-20251001": 0.000957,
    })
    assert result.ok
    assert result.detail["delta_usd"] == pytest.approx(-0.000957, abs=5e-7)


def test_layer3_cannot_pass_when_nothing_could_be_priced(prices):
    records = [rec("r1", "model-from-2029", output=MTOK)]
    nodes = [node("session:s", "session"),
             node("agent:a1", "subagent", "session:s", output=MTOK)]
    run = attribute_cost(nodes, records, {"r1": "agent:a1"}, prices,
                         CONFIRMED_ON, "s")
    result = check_against_counter(run, {"claude-opus-5": 25.0})
    assert not result.ok


# ------------------------------------------------------------------- store

def test_no_dollar_figure_is_stored_anywhere_in_the_schema(tmp_path):
    """The central constraint of this phase, enforced against the schema.

    Prices change. A stored cost would silently falsify every historical run
    at the next pricing update, so cost is computed at query time and only
    tokens are persisted.
    """
    store = Store(tmp_path / "t.db")
    offenders = []
    for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ):
        table = row["name"]
        for col in store.conn.execute(f"PRAGMA table_info({table})"):
            name = col["name"].lower()
            if "usd" in name or "cost" in name or "price" in name:
                offenders.append(f"{table}.{col['name']}")
    store.close()
    assert offenders == [], f"cost must not be persisted: {offenders}"


def test_new_token_columns_are_added_to_an_older_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS will not add a column to an existing table."""
    # The shape Phase 2 actually wrote: agent scoping and signatures, but
    # none of the columns cost attribution needs.
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        """CREATE TABLE transcript_records (
               uuid                  TEXT PRIMARY KEY,
               session_id            TEXT NOT NULL,
               agent_id              TEXT,
               parent_uuid           TEXT,
               type                  TEXT NOT NULL,
               ts_ns                 INTEGER NOT NULL DEFAULT 0,
               tool_use_id           TEXT,
               tool_signature        TEXT,
               input_tokens          INTEGER NOT NULL DEFAULT 0,
               output_tokens         INTEGER NOT NULL DEFAULT 0,
               cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
               cache_creation_tokens INTEGER NOT NULL DEFAULT 0
           )"""
    )
    old.execute(
        "INSERT INTO transcript_records (uuid, session_id, type, "
        "cache_creation_tokens) VALUES ('u1', 's1', 'assistant', 42)"
    )
    old.commit()
    old.close()

    store = Store(path)
    columns = {r["name"] for r in
               store.conn.execute("PRAGMA table_info(transcript_records)")}
    assert {"cache_write_5m_tokens", "cache_write_1h_tokens",
            "thinking_tokens", "service_tier", "node_id"} <= columns
    kept = store.conn.execute(
        "SELECT cache_creation_tokens FROM transcript_records WHERE uuid = 'u1'"
    ).fetchone()
    assert kept["cache_creation_tokens"] == 42, "migration must not lose data"
    store.close()


def test_migrating_twice_is_a_no_op(tmp_path):
    path = tmp_path / "t.db"
    Store(path).close()
    store = Store(path)
    assert store._migrate() == 0
    store.close()
