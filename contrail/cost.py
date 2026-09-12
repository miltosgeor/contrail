"""Cost attribution over a reconstructed run tree.

This is Gap 2 from docs/spec.md. See "Cost attribution, as verified" there for
what was measured rather than assumed.

Two rules shape this whole module.

**Tokens are the stored truth. No dollar figure is ever persisted.** Prices
change, and a stored cost silently falsifies every historical run at the next
pricing update. Cost is computed here, at query time, from a price table with
effective dates -- so a run recorded in March is still costed at March's
prices however many times prices move afterwards.

**An unknown model yields None, never 0.0.** A silent zero is the failure mode
this project has already been bitten by once: `ALIASES` was missing the
attribute names Claude Code actually emits, so every real span stored zero
tokens and nothing complained. Anything unpriced is counted and reported.

Everything here is a pure function over data passed in. No filesystem, no
store, no clock -- the same discipline Phase 4's detectors need, and what
makes a run reproducible at a stated date.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

PRICES_PATH = Path(__file__).with_name("prices.json")

MTOK = 1_000_000

# Models that are not real API calls. Excluded from cost entirely rather than
# priced at zero: a zero would claim we costed them, which we did not.
NON_BILLABLE_MODELS = frozenset({"<synthetic>"})

# The ratios every row of the price table must hold against its base input
# price. Asserted by the test suite across the whole table so that a typo in a
# future row fails loudly instead of quietly mispricing runs.
EXPECTED_RATIOS: dict[str, float] = {
    "output_usd_per_mtok": 5.0,
    "cache_read_usd_per_mtok": 0.1,
    "cache_write_5m_usd_per_mtok": 1.25,
    "cache_write_1h_usd_per_mtok": 2.0,
}

# `claude-opus-5[1m]` in the metrics stream, `claude-opus-5` in the
# transcript, `claude-haiku-4-5-20251001` in both. Normalise to the family
# name the price table is keyed on.
_VARIANT_SUFFIX = re.compile(r"\[[^\]]*\]\s*$")
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalise_model(model: str | None) -> str | None:
    """Reduce a model id to the name the price table is keyed on.

    Strips a bracketed variant suffix and a trailing release date. The
    bracketed form marks a long-context variant that the transcript does not
    expose at all, so a long-context run is priced at base rates here and any
    resulting divergence is what the metrics cross-check exists to surface --
    see `check_against_counter`.
    """
    if not model:
        return None
    name = _VARIANT_SUFFIX.sub("", model.strip())
    return _DATE_SUFFIX.sub("", name) or None


# ------------------------------------------------------------------ prices

@dataclass(frozen=True)
class Price:
    """One dated row of the price table, in USD per million tokens."""

    model: str
    service_tier: str
    effective_from: date
    effective_to: date | None
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    cache_write_5m_usd_per_mtok: float
    cache_write_1h_usd_per_mtok: float
    source: str

    def covers(self, when: date) -> bool:
        if when < self.effective_from:
            return False
        return self.effective_to is None or when < self.effective_to

    def ratios(self) -> dict[str, float]:
        """Each rate as a multiple of this row's base input price."""
        base = self.input_usd_per_mtok
        if not base:
            return {}
        return {
            name: getattr(self, name) / base for name in EXPECTED_RATIOS
        }


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def load_prices(path: str | Path | None = None) -> list[Price]:
    """Read the price table from JSON. Data, not constants in code."""
    raw = json.loads(Path(path or PRICES_PATH).read_text(encoding="utf-8"))
    rows = raw.get("prices", raw) if isinstance(raw, dict) else raw
    prices = []
    for row in rows:
        prices.append(
            Price(
                model=row["model"],
                service_tier=row.get("service_tier") or "standard",
                effective_from=_as_date(row["effective_from"]),
                effective_to=_as_date(row.get("effective_to")),
                input_usd_per_mtok=float(row["input_usd_per_mtok"]),
                output_usd_per_mtok=float(row["output_usd_per_mtok"]),
                cache_read_usd_per_mtok=float(row["cache_read_usd_per_mtok"]),
                cache_write_5m_usd_per_mtok=float(row["cache_write_5m_usd_per_mtok"]),
                cache_write_1h_usd_per_mtok=float(row["cache_write_1h_usd_per_mtok"]),
                source=row.get("source", ""),
            )
        )
    return prices


class PriceTable:
    """Dated price lookup. Pure: the date is always passed in, never `now()`."""

    def __init__(self, prices: Iterable[Price] | None = None) -> None:
        self.prices = list(prices) if prices is not None else load_prices()

    def find(
        self, model: str | None, when: date, service_tier: str | None = None
    ) -> Price | None:
        """The row in force for `model` on `when`, or None if unpriced.

        Returning None rather than a fallback rate is deliberate. A run older
        than the earliest confirmed price is genuinely unpriced: we do not
        know what the rate was on that date, and guessing would be a lie in a
        figure that looks authoritative.
        """
        name = normalise_model(model)
        if name is None or name in NON_BILLABLE_MODELS:
            return None
        tier = service_tier or "standard"
        candidates = [
            p for p in self.prices
            if p.model == name and p.service_tier == tier and p.covers(when)
        ]
        if not candidates:
            return None
        # Latest effective_from wins, so appending a superseding row without
        # closing the old one still resolves to the newer price.
        return max(candidates, key=lambda p: p.effective_from)

    @property
    def models(self) -> list[str]:
        return sorted({p.model for p in self.prices})


# -------------------------------------------------------------- token bundle

@dataclass
class Tokens:
    """Token counts kept apart by how they price.

    `thinking` is deliberately excluded from every sum: it is billed *inside*
    `output`, so adding it would double-charge. It is carried for analysis.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    thinking: int = 0

    def __add__(self, other: Tokens) -> Tokens:
        return Tokens(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_write_5m + other.cache_write_5m,
            self.cache_write_1h + other.cache_write_1h,
            self.thinking + other.thinking,
        )

    @property
    def billable_total(self) -> int:
        """Every token that costs money. Excludes `thinking` by design."""
        return (
            self.input + self.output + self.cache_read
            + self.cache_write_5m + self.cache_write_1h
        )

    @classmethod
    def from_obj(cls, obj: Any) -> Tokens:
        """Read counts off a record, node or database row.

        Accepts a mapping or anything carrying the attribute names, so one
        function serves transcript records, tree nodes and sqlite rows.
        """
        def get(key: str) -> int:
            value = obj.get(key, 0) if isinstance(obj, Mapping) else getattr(obj, key, 0)
            return int(value or 0)

        write_5m, write_1h = get("cache_write_5m_tokens"), get("cache_write_1h_tokens")
        creation = get("cache_creation_tokens")
        # A source carrying only the total -- an OTel span, or a record stored
        # before the TTL split was captured -- is charged at the 5m rate. It
        # is the cheaper write, so an unknown TTL cannot silently inflate a
        # cost figure.
        if not write_5m and not write_1h and creation:
            write_5m = creation
        return cls(
            input=get("input_tokens"),
            output=get("output_tokens"),
            cache_read=get("cache_read_tokens"),
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
            thinking=get("thinking_tokens"),
        )


# The five priced token classes, in the order they are reported. Cache reads
# and the two write TTLs are separate classes because they price differently
# -- 0.1x, 1.25x and 2x base input respectively.
TOKEN_CLASSES: tuple[str, ...] = (
    "input", "output", "cache_read", "cache_write_5m", "cache_write_1h",
)


def price_breakdown(tokens: Tokens, price: Price) -> dict[str, float]:
    """USD per token class for one bundle of tokens at one price row.

    **This is the only place in the codebase where a rate is applied to a
    token count.** `price_tokens` is its sum, and everything else -- cost
    attribution, the spend report, the cache economics -- goes through one of
    those two. A second pricing path is how the two silently diverge, so
    there is a test asserting this sums to `price_tokens` for every row in
    the real table.

    Each class is charged at its own rate; classes are never summed together
    first. `thinking` is not charged -- it is already inside output.
    """
    return {
        "input": tokens.input * price.input_usd_per_mtok / MTOK,
        "output": tokens.output * price.output_usd_per_mtok / MTOK,
        "cache_read": tokens.cache_read * price.cache_read_usd_per_mtok / MTOK,
        "cache_write_5m": (
            tokens.cache_write_5m * price.cache_write_5m_usd_per_mtok / MTOK
        ),
        "cache_write_1h": (
            tokens.cache_write_1h * price.cache_write_1h_usd_per_mtok / MTOK
        ),
    }


def price_tokens(tokens: Tokens, price: Price) -> float:
    """USD for one bundle of tokens at one price row."""
    return sum(price_breakdown(tokens, price).values())


# ------------------------------------------------------------- attribution

@dataclass
class NodeCost:
    """Cost of one tree node: its own, and inclusive of its descendants.

    `self_usd` is None when the node had token-bearing records that could not
    be priced, and 0.0 when it genuinely had no billable tokens -- a tool
    node, say. An unpriced run and a free run are different facts and must not
    render identically.
    """

    node_id: str
    kind: str
    label: str
    parent_node_id: str | None
    depth: int = 0
    self_tokens: Tokens = field(default_factory=Tokens)
    total_tokens: Tokens = field(default_factory=Tokens)
    self_usd: float | None = 0.0
    total_usd: float | None = 0.0
    self_unpriced_records: int = 0
    unpriced_records: int = 0
    self_priced_records: int = 0
    priced_records: int = 0
    models: tuple[str, ...] = ()

    @property
    def is_fully_priced(self) -> bool:
        return self.unpriced_records == 0


@dataclass
class RunCost:
    """Attributed cost for one session, plus what could not be priced."""

    session_id: str
    priced_at: date
    nodes: dict[str, NodeCost]
    roots: tuple[str, ...] = ()
    total_usd: float | None = None
    total_tokens: Tokens = field(default_factory=Tokens)
    unpriced_records: int = 0
    unpriced_models: tuple[str, ...] = ()
    non_billable_records: int = 0
    unattributed_records: int = 0
    warnings: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[NodeCost]:
        """Nodes of one kind, most expensive first, unpriced last."""
        return sorted(
            (n for n in self.nodes.values() if n.kind == kind),
            key=lambda n: (n.total_usd is None, -(n.total_usd or 0.0)),
        )

    @property
    def is_fully_priced(self) -> bool:
        return self.unpriced_records == 0


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def attribute_cost(
    nodes: Sequence[Any],
    records: Sequence[Any],
    record_scope: Mapping[str, str],
    prices: PriceTable,
    priced_at: date,
    session_id: str = "",
) -> RunCost:
    """Attribute cost across a run tree.

    `record_scope` maps a record uuid to the node whose self tokens it
    contributed to. Phase 2's tree walk already decides that, so this reuses
    the decision rather than re-deriving it from `agent_id` -- which would get
    parent-transcript records wrong, since those belong to a turn node.

    Records are priced individually and then summed onto their node, not
    priced once at a node-level model: a node's records can span models, and
    an Opus record and a Haiku record in the same subagent cost differently.

    Rolling up the tree is what makes "$1.90 of it was one Explore subagent"
    computable -- a subagent reports everything beneath it.
    """
    out: dict[str, NodeCost] = {}
    for node in nodes:
        node_id = _field(node, "node_id")
        out[node_id] = NodeCost(
            node_id=node_id,
            kind=_field(node, "kind", "") or "",
            label=_field(node, "label", "") or "",
            parent_node_id=_field(node, "parent_node_id"),
            depth=int(_field(node, "depth", 0) or 0),
            self_tokens=Tokens.from_obj(node),
        )

    self_usd: dict[str, float] = {}
    self_unpriced: dict[str, int] = {}
    priced_counts: dict[str, int] = {}
    node_models: dict[str, set[str]] = {}
    unpriced_models: set[str] = set()
    unpriced = non_billable = unattributed = 0
    warnings: list[str] = []

    for record in records:
        tokens = Tokens.from_obj(record)
        if not tokens.billable_total:
            continue  # tool results and prompts carry no tokens

        raw_model = _field(record, "model")
        name = normalise_model(raw_model)
        if name in NON_BILLABLE_MODELS:
            # Not a real API call. Excluded entirely rather than priced at
            # zero, because zero would claim we costed it.
            non_billable += 1
            continue

        node_id = record_scope.get(_field(record, "uuid") or "")
        if node_id is None or node_id not in out:
            unattributed += 1
            continue

        price = prices.find(raw_model, priced_at, _field(record, "service_tier"))
        if price is None:
            unpriced += 1
            self_unpriced[node_id] = self_unpriced.get(node_id, 0) + 1
            if name:
                unpriced_models.add(name)
            continue

        self_usd[node_id] = self_usd.get(node_id, 0.0) + price_tokens(tokens, price)
        priced_counts[node_id] = priced_counts.get(node_id, 0) + 1
        node_models.setdefault(node_id, set()).add(name or "")

    for node_id, cost in out.items():
        cost.self_unpriced_records = self_unpriced.get(node_id, 0)
        cost.self_priced_records = priced_counts.get(node_id, 0)
        cost.models = tuple(sorted(node_models.get(node_id, ())))
        if node_id in self_usd:
            cost.self_usd = self_usd[node_id]
        elif cost.self_unpriced_records:
            cost.self_usd = None  # had billable tokens, no price for them
        else:
            cost.self_usd = 0.0

    if unpriced_models:
        warnings.append("unpriced models: " + ", ".join(sorted(unpriced_models)))
    if non_billable:
        warnings.append(
            f"{non_billable} record(s) on a non-billable model, excluded from cost"
        )
    if unattributed:
        warnings.append(f"{unattributed} token-bearing record(s) matched no tree node")

    run = RunCost(
        session_id=session_id,
        priced_at=priced_at,
        nodes=out,
        unpriced_records=unpriced,
        unpriced_models=tuple(sorted(unpriced_models)),
        non_billable_records=non_billable,
        unattributed_records=unattributed,
        warnings=warnings,
    )
    _rollup(run)
    return run


def _rollup(run: RunCost) -> None:
    """Sum each node's descendants into its inclusive totals.

    A subtree with some unpriced records reports the priced part as a number
    and carries the unpriced count alongside, so the figure reads as "at
    least this much, with N records unpriced". A subtree where records existed
    but *none* could be priced reports None, never 0.0.

    The distinction that matters: a node with no billable tokens anywhere
    beneath it is genuinely free and reports 0.0 -- a tool node, say. A node
    whose only records were unpriced is not free, and must not render as
    though it were. An early version of this got that wrong, and a run whose
    every record was unpriced totalled $0.00 because the session node itself
    had no tokens of its own.
    """
    children: dict[str | None, list[NodeCost]] = {}
    for node in run.nodes.values():
        children.setdefault(node.parent_node_id, []).append(node)

    roots = [n for n in run.nodes.values() if n.parent_node_id not in run.nodes]

    def walk(node: NodeCost, seen: frozenset[str]) -> None:
        if node.node_id in seen:
            return  # a malformed parent chain must not recurse forever
        seen = seen | {node.node_id}
        node.total_tokens = node.self_tokens
        priced = node.self_usd or 0.0
        node.unpriced_records = node.self_unpriced_records
        node.priced_records = node.self_priced_records

        for child in children.get(node.node_id, ()):
            walk(child, seen)
            node.total_tokens = node.total_tokens + child.total_tokens
            node.unpriced_records += child.unpriced_records
            node.priced_records += child.priced_records
            priced += child.total_usd or 0.0

        # None only when there was something to price and none of it could
        # be priced. Nothing to price at all is a known zero.
        blind = node.unpriced_records > 0 and node.priced_records == 0
        node.total_usd = None if blind else priced

    for root in roots:
        walk(root, frozenset())

    run.roots = tuple(r.node_id for r in roots)
    run.total_tokens = Tokens()
    total = 0.0
    priced_records = 0
    for root in roots:
        run.total_tokens = run.total_tokens + root.total_tokens
        total += root.total_usd or 0.0
        priced_records += root.priced_records
    blind = run.unpriced_records > 0 and priced_records == 0
    run.total_usd = None if blind else total


# --------------------------------------------------------- reconciliation
#
# Three layers, each labelled with what it actually proves. See docs/spec.md,
# "The three reconciliation layers".


@dataclass
class Reconciliation:
    """The result of one reconciliation layer."""

    layer: str
    proves: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def check_attribution_invariant(run: RunCost, records: Sequence[Any],
                                record_scope: Mapping[str, str]) -> Reconciliation:
    """Layer 1: the attributed tokens equal the source tokens.

    **This is the correctness test.** Pure arithmetic over the tree: if the
    sum of every node's self tokens does not equal the sum over the source
    records, something is double-counted, dropped or mis-scoped. It caught a
    real double-count in Phase 2 and it is cheap enough to run on every parse.

    Deliberately about tokens, not dollars -- it must hold whatever the price
    table says, and it holds for a run whose model we cannot price at all.
    """
    attributed = Tokens()
    for node in run.nodes.values():
        attributed = attributed + node.self_tokens

    source = Tokens()
    counted = 0
    for record in records:
        if record_scope.get(_field(record, "uuid") or "") is None:
            continue  # not attributed to any node; reported separately
        source = source + Tokens.from_obj(record)
        counted += 1

    fields = ("input", "output", "cache_read", "cache_write_5m",
              "cache_write_1h", "thinking")
    diffs = {
        f: getattr(attributed, f) - getattr(source, f)
        for f in fields
        if getattr(attributed, f) != getattr(source, f)
    }
    notes = []
    if diffs:
        notes.append("attributed minus source, by category: " + repr(diffs))
    if run.unattributed_records:
        notes.append(
            f"{run.unattributed_records} token-bearing record(s) matched no node"
        )
    return Reconciliation(
        layer="attribution invariant",
        proves="tokens are neither double-counted nor dropped by the attribution",
        ok=not diffs,
        detail={
            "records_attributed": counted,
            "attributed_billable": attributed.billable_total,
            "source_billable": source.billable_total,
            "diffs": diffs,
        },
        notes=notes,
    )


def check_cross_source(records: Sequence[Any],
                       spans: Sequence[Any]) -> Reconciliation:
    """Layer 2: transcript tokens agree with OTel span tokens.

    Joined on `request_id` / `requestId`, which is the only key the two share
    -- llm_request spans carry no tool_use_id and no agent identity.

    Independent evidence that the token *inputs* are right, from two pipelines
    that share no code. Not a completeness check: export can start mid-session,
    so transcript-only rows are normal and are reported rather than failed.
    """
    by_request: dict[str, Tokens] = {}
    for record in records:
        rid = _field(record, "request_id")
        if rid:
            by_request[rid] = Tokens.from_obj(record)

    span_tokens: dict[str, Tokens] = {}
    for span in spans:
        attributes = _field(span, "attributes") or {}
        if isinstance(attributes, str):
            attributes = json.loads(attributes or "{}")
        rid = attributes.get("request_id")
        if not rid:
            continue
        span_tokens[rid] = Tokens.from_obj({
            "input_tokens": attributes.get("input_tokens", 0),
            "output_tokens": attributes.get("output_tokens", 0),
            "cache_read_tokens": attributes.get("cache_read_tokens", 0),
            "cache_creation_tokens": attributes.get("cache_creation_tokens", 0),
        })

    matched = sorted(set(by_request) & set(span_tokens))
    disagreeing = []
    for rid in matched:
        a, b = by_request[rid], span_tokens[rid]
        # Spans carry only the cache-creation total, so compare the total
        # rather than the TTL split, which spans do not report.
        if (a.input, a.output, a.cache_read,
                a.cache_write_5m + a.cache_write_1h) != (
                b.input, b.output, b.cache_read,
                b.cache_write_5m + b.cache_write_1h):
            disagreeing.append(rid)

    notes = []
    if not matched:
        notes.append("no overlap on request_id -- nothing to compare")
    transcript_only = len(set(by_request) - set(span_tokens))
    if transcript_only:
        notes.append(
            f"{transcript_only} call(s) in the transcript with no span "
            "(normal when export started mid-session)"
        )
    return Reconciliation(
        layer="cross-source agreement",
        proves="the token inputs agree across two independent pipelines",
        ok=bool(matched) and not disagreeing,
        detail={
            "matched": len(matched),
            "disagreeing": len(disagreeing),
            "disagreeing_request_ids": disagreeing[:10],
            "transcript_only": transcript_only,
            "span_only": len(set(span_tokens) - set(by_request)),
        },
        notes=notes,
    )


def check_against_counter(
    run: RunCost,
    counter_usd: Mapping[str, float],
    tolerance: float = 0.02,
) -> Reconciliation:
    """Layer 3: our computed cost agrees with Claude Code's own counter.

    **The counter is not ground truth.** `claude_code.cost.usage` is Claude
    Code's client-side estimate, computed from a price table bundled in the
    CLI. It is not a billing figure. What agreement proves is that *our* price
    table matches *theirs*, which usefully catches ours going stale after a
    price change -- and nothing more. On disagreement either table could be
    the wrong one.

    Two known residuals make exact agreement impossible, so both are reported
    rather than absorbed:

    - `query_source: auxiliary` spend (small Haiku calls for things like title
      generation) is billed by the counter but appears nowhere in the
      transcript, so our figure runs *under*.
    - The counter names models with a context-window suffix the transcript
      omits, so a long-context run may be priced here at base rates.
    """
    counter_total = sum(counter_usd.values())
    ours = run.total_usd
    notes = [
        ("counter is Claude Code's client-side estimate from a bundled price "
         "table, not a billing figure; this compares price tables, not truth"),
    ]
    if ours is None:
        notes.append("nothing in this run could be priced -- no comparison possible")
        return Reconciliation(
            layer="agreement with Claude Code's estimate",
            proves="our price table matches the one bundled in the CLI",
            ok=False,
            detail={"ours_usd": None, "counter_usd": counter_total},
            notes=notes,
        )

    delta = ours - counter_total
    relative = abs(delta) / counter_total if counter_total else None
    if run.unpriced_records:
        notes.append(
            f"{run.unpriced_records} record(s) unpriced here, so our figure is "
            "a floor rather than a total"
        )
    if delta < 0:
        notes.append(
            "ours is lower, which is the expected direction: auxiliary "
            "model calls are billed by the counter but absent from transcripts"
        )
    return Reconciliation(
        layer="agreement with Claude Code's estimate",
        proves="our price table matches the one bundled in the CLI",
        ok=relative is not None and relative <= tolerance,
        detail={
            "ours_usd": round(ours, 6),
            "counter_usd": round(counter_total, 6),
            "delta_usd": round(delta, 6),
            "relative": round(relative, 6) if relative is not None else None,
            "tolerance": tolerance,
            "counter_by_model": {k: round(v, 6) for k, v in counter_usd.items()},
        },
        notes=notes,
    )
