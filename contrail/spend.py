"""Aggregate spend across every session in the store.

The rest of Contrail answers "what happened in this run". This answers "where
does my money actually go", which turned out to be the more useful question --
the Phase 6 measurement produced that answer by hand, and this is it as a
command.

Two deliberate departures from the conventions elsewhere, both because this
report answers a different *kind* of question:

**One price basis by default, not each session's own date.** "What did this
run cost" is a historical fact and keeps its own date (`contrail cost`).
"Where does my spending concentrate" is a comparison, and a comparison needs
one basis: shares computed from several price bases are not comparable to each
other. On the corpus this was built against, pricing at own dates would have
computed the headline shares from 9% of the tokens while presenting them as
the whole picture. So the default is a uniform basis, labelled as one, with
`own_date=True` for the strict per-session view.

**Token classes are reported by share of spend, not share of tokens.** A
cache-read token and an output token are not comparable quantities -- reads
are a tenth of base input, output is five times it. By token count this corpus
is 95% cache reads, which tells you nothing; by spend it is 45% reads and 41%
one-hour cache writes, which tells you where to look.

Everything here is a pure function over rows passed in, and every dollar comes
from `cost.price_breakdown` -- the single place a rate is applied to a token
count. Nothing in this module knows a rate.
"""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .cost import (
    NON_BILLABLE_MODELS,
    TOKEN_CLASSES,
    PriceTable,
    Tokens,
    normalise_model,
    price_breakdown,
)

# A gap longer than this expires a five-minute cache, forcing a fresh write.
FIVE_MINUTES_S = 300
ONE_HOUR_S = 3600

# Gaps longer than this are treated as "came back the next day" rather than a
# pause inside a working session, and are excluded from the cadence figures.
SESSION_BREAK_S = 6 * 3600

BASIS_UNIFORM = "uniform"
BASIS_OWN_DATE = "own-date"


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ------------------------------------------------------------------ shapes

@dataclass
class Bucket:
    """One row of a breakdown: a key, its tokens, and its spend."""

    key: str
    label: str = ""
    tokens: int = 0
    usd: float = 0.0
    priced_records: int = 0
    unpriced_records: int = 0

    @property
    def is_priced(self) -> bool:
        return self.priced_records > 0


@dataclass
class CacheEconomics:
    """Whether prompt caching is paying for itself, as a model.

    A write costs 1.25x (5-minute TTL) or 2x (1-hour) base input; a read costs
    0.1x. So a cache charges a premium up front and returns a discount on
    every subsequent read. This compares the two.

    The counterfactual is "the same conversation with no caching, re-sending
    the same context as uncached input every turn". That is roughly what would
    happen, but it is a model and not a measurement -- notably it holds token
    counts fixed, and an uncached agent might well be driven differently.
    """

    write_premium_usd: float = 0.0
    read_saving_usd: float = 0.0
    actual_usd: float = 0.0
    tokens_5m: int = 0
    tokens_1h: int = 0
    # Repricing the 1h writes at the 5m rate, net of the re-writes that the
    # shorter TTL would have forced. None when there is nothing to compare.
    ttl_switch_usd: float | None = None
    ttl_switch_expiry_rate: float | None = None

    @property
    def net_usd(self) -> float:
        return self.read_saving_usd - self.write_premium_usd

    @property
    def return_ratio(self) -> float | None:
        """Dollars returned on reads per dollar of write premium."""
        if self.write_premium_usd <= 0:
            return None
        return self.read_saving_usd / self.write_premium_usd

    @property
    def uncached_usd(self) -> float:
        """What the same tokens would have cost with no caching at all."""
        return self.actual_usd + self.net_usd

    @property
    def pays_for_itself(self) -> bool:
        return self.net_usd > 0


@dataclass
class Cadence:
    """How fast requests follow each other, which is what decides the TTL.

    A five-minute cache survives a gap shorter than five minutes. If almost
    every gap is shorter than that, the one-hour TTL is buying insurance
    against a case that rarely happens -- at 2x base input instead of 1.25x.
    """

    gaps: int = 0
    median_s: float = 0.0
    over_5m: int = 0
    over_1h: int = 0

    @property
    def expiry_rate(self) -> float | None:
        """Fraction of gaps that would expire a five-minute cache."""
        return (self.over_5m / self.gaps) if self.gaps else None


@dataclass
class SpendReport:
    basis: str = BASIS_UNIFORM
    priced_at: date | None = None
    sessions: int = 0
    tokens: Tokens = field(default_factory=Tokens)
    total_usd: float = 0.0
    priced_records: int = 0
    unpriced_records: int = 0
    unpriced_models: tuple[str, ...] = ()
    unpriced_sessions: tuple[str, ...] = ()
    non_billable_records: int = 0
    by_class: dict[str, Bucket] = field(default_factory=dict)
    by_model: list[Bucket] = field(default_factory=list)
    by_scope: list[Bucket] = field(default_factory=list)
    by_session: list[Bucket] = field(default_factory=list)
    cache: CacheEconomics = field(default_factory=CacheEconomics)
    cadence: Cadence = field(default_factory=Cadence)

    @property
    def is_fully_priced(self) -> bool:
        return self.unpriced_records == 0

    def class_share(self, name: str) -> float | None:
        """Share of *spend* for one token class, not share of tokens."""
        bucket = self.by_class.get(name)
        if bucket is None or not self.total_usd:
            return None
        return bucket.usd / self.total_usd

    @property
    def context_share(self) -> float | None:
        """Share of spend that is re-reading and re-writing context.

        The headline: everything except output and uncached input.
        """
        if not self.total_usd:
            return None
        context = sum(
            self.by_class[name].usd
            for name in ("cache_read", "cache_write_5m", "cache_write_1h")
            if name in self.by_class
        )
        return context / self.total_usd


# -------------------------------------------------------------- aggregation

def _basis_date(
    row: Mapping[str, Any], priced_at: date | None, own_date: bool
) -> date | None:
    if not own_date:
        return priced_at
    start_ns = row.get("start_ns") or 0
    if start_ns:
        return datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc).date()
    return priced_at


def aggregate_spend(
    sessions: Sequence[Mapping[str, Any]],
    records_by_session: Mapping[str, Sequence[Any]],
    prices: PriceTable,
    *,
    priced_at: date | None = None,
    own_date: bool = False,
) -> SpendReport:
    """Aggregate spend across sessions.

    `sessions` are summary rows (`session_id`, `project_slug`, `start_ns`) and
    `records_by_session` maps a session id to its transcript records. Pure: no
    store, no filesystem, and the date is always passed in.

    With `own_date`, each session is priced at its own start date -- the strict
    convention, under which a session older than the earliest confirmed price
    contributes tokens but no dollars. By default every session is priced at
    `priced_at`, a uniform basis, so the shares are comparable to each other.
    """
    report = SpendReport(
        basis=BASIS_OWN_DATE if own_date else BASIS_UNIFORM,
        priced_at=priced_at,
        sessions=len(sessions),
    )
    report.by_class = {name: Bucket(key=name) for name in TOKEN_CLASSES}
    models: dict[str, Bucket] = {}
    scopes = {
        "main": Bucket(key="main", label="main conversation"),
        "subagent": Bucket(key="subagent", label="subagents"),
    }
    unpriced_models: set[str] = set()
    unpriced_sessions: set[str] = set()
    cache = report.cache
    gaps: list[float] = []

    for row in sessions:
        session_id = row.get("session_id") or ""
        basis = _basis_date(row, priced_at, own_date)
        records = records_by_session.get(session_id) or ()
        bucket = Bucket(
            key=session_id,
            label=row.get("project_slug") or "",
        )
        stamps: list[int] = []

        for record in records:
            tokens = Tokens.from_obj(record)
            if not tokens.billable_total:
                continue
            raw_model = _field(record, "model")
            name = normalise_model(raw_model)
            if name in NON_BILLABLE_MODELS:
                report.non_billable_records += 1
                continue

            report.tokens = report.tokens + tokens
            bucket.tokens += tokens.billable_total
            scope = scopes["subagent" if _field(record, "agent_id") else "main"]
            scope.tokens += tokens.billable_total
            model_bucket = models.setdefault(
                name or "unknown", Bucket(key=name or "unknown")
            )
            model_bucket.tokens += tokens.billable_total
            cache.tokens_5m += tokens.cache_write_5m
            cache.tokens_1h += tokens.cache_write_1h

            stamp = _field(record, "ts_ns") or 0
            if stamp:
                stamps.append(int(stamp))

            price = (
                prices.find(raw_model, basis, _field(record, "service_tier"))
                if basis is not None else None
            )
            if price is None:
                report.unpriced_records += 1
                bucket.unpriced_records += 1
                scope.unpriced_records += 1
                model_bucket.unpriced_records += 1
                if name:
                    unpriced_models.add(name)
                unpriced_sessions.add(session_id)
                continue

            parts = price_breakdown(tokens, price)
            usd = sum(parts.values())
            report.total_usd += usd
            report.priced_records += 1
            bucket.usd += usd
            bucket.priced_records += 1
            scope.usd += usd
            scope.priced_records += 1
            model_bucket.usd += usd
            model_bucket.priced_records += 1
            for class_name, amount in parts.items():
                class_bucket = report.by_class[class_name]
                class_bucket.usd += amount
                class_bucket.priced_records += 1
            report.by_class["input"].tokens += tokens.input
            report.by_class["output"].tokens += tokens.output
            report.by_class["cache_read"].tokens += tokens.cache_read
            report.by_class["cache_write_5m"].tokens += tokens.cache_write_5m
            report.by_class["cache_write_1h"].tokens += tokens.cache_write_1h

            # Cache economics, in the same pass and from the same rates.
            base = price.input_usd_per_mtok
            cache.actual_usd += usd
            cache.read_saving_usd += (
                tokens.cache_read * (base - price.cache_read_usd_per_mtok) / 1_000_000
            )
            cache.write_premium_usd += (
                tokens.cache_write_5m * (price.cache_write_5m_usd_per_mtok - base)
                + tokens.cache_write_1h * (price.cache_write_1h_usd_per_mtok - base)
            ) / 1_000_000

        if bucket.tokens:
            report.by_session.append(bucket)
        gaps.extend(_gaps(stamps))

    report.by_model = sorted(models.values(), key=lambda b: -b.tokens)
    report.by_scope = [scopes["main"], scopes["subagent"]]
    report.by_session.sort(key=lambda b: -b.tokens)
    report.unpriced_models = tuple(sorted(unpriced_models))
    report.unpriced_sessions = tuple(sorted(unpriced_sessions))
    report.cadence = _cadence(gaps)
    _ttl_counterfactual(report, sessions, records_by_session, prices,
                        priced_at, own_date)
    return report


def _gaps(stamps: Sequence[int]) -> list[float]:
    """Seconds between consecutive requests, within a working session."""
    ordered = sorted(stamps)
    out = []
    for earlier, later in itertools.pairwise(ordered):
        seconds = (later - earlier) / 1e9
        if 0 < seconds < SESSION_BREAK_S:
            out.append(seconds)
    return out


def _cadence(gaps: Sequence[float]) -> Cadence:
    if not gaps:
        return Cadence()
    return Cadence(
        gaps=len(gaps),
        median_s=statistics.median(gaps),
        over_5m=sum(1 for g in gaps if g > FIVE_MINUTES_S),
        over_1h=sum(1 for g in gaps if g > ONE_HOUR_S),
    )


def _ttl_counterfactual(
    report: SpendReport,
    sessions: Sequence[Mapping[str, Any]],
    records_by_session: Mapping[str, Sequence[Any]],
    prices: PriceTable,
    priced_at: date | None,
    own_date: bool,
) -> None:
    """What the one-hour cache writes would have cost at the five-minute rate.

    Netted against the re-writes a shorter TTL would have forced: a gap longer
    than five minutes expires the cache, and the next request pays to write it
    again. The expiry rate is measured from the request cadence; assuming each
    expiry rewrites an average-sized prefix is the model's weakest step, and it
    is the reason this is reported as a comparison rather than a saving.
    """
    cache = report.cache
    expiry = report.cadence.expiry_rate
    if not cache.tokens_1h or expiry is None:
        return

    saving = 0.0
    for row in sessions:
        basis = _basis_date(row, priced_at, own_date)
        if basis is None:
            continue
        for record in records_by_session.get(row.get("session_id") or "") or ():
            tokens = Tokens.from_obj(record)
            if not tokens.cache_write_1h:
                continue
            price = prices.find(
                _field(record, "model"), basis, _field(record, "service_tier")
            )
            if price is None:
                continue
            cheaper = tokens.cache_write_1h * price.cache_write_5m_usd_per_mtok
            dearer = tokens.cache_write_1h * price.cache_write_1h_usd_per_mtok
            rewrites = expiry * tokens.cache_write_1h * price.cache_write_5m_usd_per_mtok
            saving += (dearer - cheaper - rewrites) / 1_000_000

    cache.ttl_switch_usd = -saving
    cache.ttl_switch_expiry_rate = expiry


# ------------------------------------------------------------- model repricing

@dataclass
class Repricing:
    """The same token counts at another model's rates. An upper bound.

    It assumes identical token counts, which would not hold: a different model
    produces different output lengths and may need more or fewer turns to
    finish the same work. Contrail can say what the tokens would have cost;
    it cannot say whether the work would have been done. So this is a ceiling
    on the saving, never a forecast.
    """

    model: str
    usd: float = 0.0
    baseline_usd: float = 0.0
    priced_records: int = 0
    unpriced_records: int = 0

    @property
    def delta_usd(self) -> float:
        return self.usd - self.baseline_usd

    @property
    def share(self) -> float | None:
        if not self.baseline_usd:
            return None
        return self.delta_usd / self.baseline_usd


def reprice_at_model(
    sessions: Sequence[Mapping[str, Any]],
    records_by_session: Mapping[str, Sequence[Any]],
    prices: PriceTable,
    model: str,
    *,
    priced_at: date | None = None,
    own_date: bool = False,
) -> Repricing:
    """Reprice every billable token at one model's rates."""
    out = Repricing(model=model)
    for row in sessions:
        basis = _basis_date(row, priced_at, own_date)
        if basis is None:
            continue
        target = prices.find(model, basis)
        for record in records_by_session.get(row.get("session_id") or "") or ():
            tokens = Tokens.from_obj(record)
            if not tokens.billable_total:
                continue
            if normalise_model(_field(record, "model")) in NON_BILLABLE_MODELS:
                continue
            actual = prices.find(
                _field(record, "model"), basis, _field(record, "service_tier")
            )
            if actual is None or target is None:
                out.unpriced_records += 1
                continue
            out.baseline_usd += sum(price_breakdown(tokens, actual).values())
            out.usd += sum(price_breakdown(tokens, target).values())
            out.priced_records += 1
    return out
