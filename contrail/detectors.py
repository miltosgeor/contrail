"""Detectors over a reconstructed run tree.

This is Gap 3 from docs/spec.md. Four detectors, each a pure function over
data passed in -- no filesystem, no store, no clock -- so they are testable
against fixtures and reproducible. CLAUDE.md requires that, and it is what
keeps this part engineering rather than scripting.

Every finding carries the evidence for itself. A detector that says "this
looks redundant" without saying why cannot be argued with, and these rules
are wrong often enough that being arguable matters. The first repeat rule
tried here -- "was there an intervening write to the same target?" -- was
wrong on 8 of the 9 cases it flagged, because files are also rewritten by
background processes and by shell commands that leave no write in the trace.

What the detectors do and do not claim:

- `detect_redundant_repeats` compares *result* hashes, so it is right about
  redundancy whatever caused it. Validated against a hand-labelled sample of
  22 repeat groups: 6 positive, 15 negative, 1 undecidable. That is a small
  sample and the numbers are quoted with it.
- `detect_cost_concentration` is arithmetic over the Phase 3 attribution and
  needs no ground truth. Its thresholds are tuned to one corpus, and both are
  named parameters rather than constants for that reason.
- `detect_outcome_divergence` requires structured, comparable outputs. It
  does not generalise to arbitrary runs.
- `detect_unhandled_errors` reports a *shape*, never a verdict. Some errors
  are informative and moving on is correct behaviour; a detector that calls
  those a failure is wrong. It is a structural proxy, labelled one, and its
  precision was measured at 3 of 4 hand-labelled findings after excluding two
  benign error classes, up from 3 of 14 before. A sample of four proves very
  little, so it stays low confidence.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# --- detector names ---------------------------------------------------------
REDUNDANT_REPEATS = "redundant_repeats"
COST_CONCENTRATION = "cost_concentration"
OUTCOME_DIVERGENCE = "outcome_divergence"
UNHANDLED_ERRORS = "unhandled_errors"

# --- finding subtypes -------------------------------------------------------
# Genuine redundancy and unproductive polling are different phenomena with
# different remedies, so they are reported apart even though both fire.
SUBTYPE_REDUNDANT = "redundant"          # identical result, nothing was waiting
SUBTYPE_POLLING = "unproductive_polling"  # identical result, waiting on a task
SUBTYPE_WRITE_REDUNDANT = "redundant_write"

# --- confidence -------------------------------------------------------------
# Stated per finding, because these rules are not equally trustworthy.
HIGH = "high"        # follows from recorded data with no inference
LOW = "low"          # a structural proxy; read the evidence before acting

WRITER_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# Error classes where not following up is the correct behaviour, so reporting
# them as unhandled would be wrong. Both are harness-generated templates
# classified at parse time -- see `classify_error` in transcript.py.
#
#   user_declined   the user said no at the permission prompt. There is
#                   nothing for the agent to handle.
#   file_not_found  a probe for a file that does not exist. Not touching it
#                   again is exactly right.
#
# These two accounted for 10 of the 14 findings in the first hand-labelled
# sample. Excluding them is what takes precision from 3/14 to 3/4.
BENIGN_ERROR_CLASSES = frozenset({"user_declined", "file_not_found"})


@dataclass
class Finding:
    """One thing a detector noticed, with the evidence for it."""

    detector: str
    summary: str
    node_id: str | None = None
    session_id: str | None = None
    subtype: str | None = None
    confidence: str = HIGH
    # Ids so a caller can navigate to what this is about, never content.
    record_uuids: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - convenience only
        where = f" [{self.node_id}]" if self.node_id else ""
        return f"{self.detector}: {self.summary}{where}"


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ------------------------------------------------------- redundant repeats

def detect_redundant_repeats(
    records: Sequence[Any],
    record_scope: Mapping[str, str],
    session_id: str = "",
) -> list[Finding]:
    """Tool calls repeated within one node that produced the same result.

    The rule is result equality, not intent. Two calls with the same
    normalised signature *and* the same result hash gained nothing the second
    time, whatever the cause -- which is what makes this robust to files being
    rewritten by processes that leave no trace of the write. A differing
    result hash means something changed, and is not reported.

    Repeated writes take a separate branch. An identical edit applied twice
    should fail the second time, so `is_error` is the discriminator there,
    not result equality: the real corpus contained ten repeated writes and
    every one was a retry after a failure, not a loop.

    A repeat with no result on disk is skipped rather than guessed at, and
    counted in `undecidable_groups` on every finding so the omission is
    visible.
    """
    # A call and its result are two separate records joined on tool_use_id:
    # the call is a tool_use block on an assistant record, the result a
    # tool_result block on a user record, and `result_hash` lives on the
    # latter. Reading it off the call gives None for everything.
    results = {
        _field(r, "tool_use_id"): r
        for r in records if _field(r, "is_tool_result")
    }
    launched_tasks = {
        _field(r, "background_task_id")
        for r in results.values() if _field(r, "background_task_id")
    }

    calls: dict[tuple[str, str], list[Any]] = {}
    for record in records:
        signature = _field(record, "tool_signature")
        if _field(record, "is_tool_result") or not signature:
            continue
        node = record_scope.get(_field(record, "uuid") or "")
        if node is None:
            continue
        calls.setdefault((node, signature), []).append(record)

    findings: list[Finding] = []
    undecidable = 0

    for (node, signature), group in sorted(calls.items()):
        if len(group) < 2:
            continue
        outcomes = [results.get(_field(r, "tool_use_id")) for r in group]
        hashes = [
            _field(o, "result_hash") if o is not None else None for o in outcomes
        ]
        if any(h is None for h in hashes):
            undecidable += 1
            continue

        tool = _field(group[0], "tool_name") or "?"
        errors = [
            bool(_field(o, "is_error")) if o is not None else False
            for o in outcomes
        ]
        uuids = tuple(_field(r, "uuid") for r in group)
        common = {
            "tool": tool,
            "calls": len(group),
            "distinct_result_hashes": len(set(hashes)),
            "signature": signature,
            "labelled_sample": "22 groups: 6 positive, 15 negative, 1 undecidable",
        }

        if tool in WRITER_TOOLS:
            if any(errors):
                continue  # a retry after a failure is not redundant work
            if len(set(hashes)) > 1:
                continue
            findings.append(Finding(
                detector=REDUNDANT_REPEATS,
                subtype=SUBTYPE_WRITE_REDUNDANT,
                summary=(
                    f"{tool} applied {len(group)}x with the same arguments, all "
                    "succeeding with an identical result"
                ),
                node_id=node, session_id=session_id, record_uuids=uuids,
                evidence={**common, "is_error": errors,
                          "note": "an identical edit applied twice would normally "
                                  "fail the second time; these did not"},
            ))
            continue

        if len(set(hashes)) > 1:
            continue  # the result changed, so the repeat was productive

        task = next(
            (_field(r, "background_task_id") for r in group
             if _field(r, "background_task_id")),
            None,
        )
        polling = bool(task and task in launched_tasks)
        findings.append(Finding(
            detector=REDUNDANT_REPEATS,
            subtype=SUBTYPE_POLLING if polling else SUBTYPE_REDUNDANT,
            summary=(
                f"{tool} called {len(group)}x, each returning identical output "
                + ("while waiting on a background task" if polling
                   else "with nothing changing in between")
            ),
            node_id=node, session_id=session_id, record_uuids=uuids,
            evidence={**common,
                      "background_task_id": task,
                      "note": ("polling a background task that produced no new "
                               "output; the wait was reasonable, the reads "
                               "returned nothing new")
                      if polling else
                      ("identical arguments and identical result: the repeat "
                       "gained no information")},
        ))

    for finding in findings:
        finding.evidence["undecidable_groups"] = undecidable
    return findings


# ------------------------------------------------------ cost concentration
#
# The two thresholds below are the defaults, and they come from looking at
# this corpus -- not from any principle. A tuned constant presented as a rule
# is exactly what the divergence negative result taught us to avoid, so both
# are named parameters and both are reported in every finding's evidence.

DEFAULT_SHARE_THRESHOLD = 0.5      # a child holding this much of its parent
DEFAULT_MIN_TOKENS = 50_000        # below this, concentration is not worth saying
DEFAULT_MIN_SIBLINGS = 3           # fewer than this and a large share is arithmetic
DEFAULT_MIN_TIMES_MEDIAN = 2.0     # how far above the typical sibling it must sit
DESCEND_SHARE = 0.9                # follow an only-child holding this much


def _most_specific(node: Any, children: Mapping[str | None, list[Any]]) -> tuple[Any, list[str]]:
    """Follow a single dominant child down to the node that really holds the cost.

    Concentration is *decided* by comparing siblings, but the node worth
    naming is often deeper: an `Agent` tool call has exactly one child, the
    subagent that did the work, so stopping at the tool call reports the
    mechanism instead of the culprit. "One Explore subagent burned the
    budget" is the useful sentence, and this is what makes the finding say it.

    Only descends through an only-child that holds essentially all of its
    parent, so nothing is attributed to a node that did not cause it.
    """
    chain: list[str] = []
    current = node
    while True:
        kids = children.get(current.node_id, ())
        if len(kids) != 1:
            return current, chain
        child = kids[0]
        parent_tokens = current.total_tokens.billable_total
        if not parent_tokens:
            return current, chain
        if child.total_tokens.billable_total / parent_tokens < DESCEND_SHARE:
            return current, chain
        chain.append(f"{current.kind}:{current.label[:30]}")
        current = child


def detect_cost_concentration(
    run_cost: Any,
    share_threshold: float = DEFAULT_SHARE_THRESHOLD,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    min_siblings: int = DEFAULT_MIN_SIBLINGS,
    min_times_median: float = DEFAULT_MIN_TIMES_MEDIAN,
    session_id: str = "",
) -> list[Finding]:
    """Nodes holding a disproportionate share of their parent's tokens.

    Ranks on tokens, which are the stored truth, and reports dollars through
    whatever price the caller already resolved -- no price lookup happens
    here. Needs no ground truth beyond arithmetic.

    Share alone is not concentration. With two children, one holding 50% or
    more is close to arithmetically inevitable -- on the real corpus, share
    alone produced 11 findings of which 7 were two-sibling workflows whose
    "dominant" child was only 1.01x to 1.91x its single sibling. Those are
    not insights, and they buried the findings that were. So a node must also
    sit clear of the *typical* sibling, among enough siblings for "typical"
    to mean anything.

    - `share_threshold`  fraction of the parent's inclusive tokens
    - `min_tokens`       absolute floor, so 90% of almost nothing never fires
    - `min_siblings`     below this, a large share carries no information
    - `min_times_median` how far above the median sibling it must sit

    **Every default is calibrated against one corpus, not derived from a
    principle**, and each is reported in the finding's evidence for that
    reason. Tune them per project; do not read them as rules.
    """
    nodes = run_cost.nodes
    children: dict[str | None, list[Any]] = {}
    for node in nodes.values():
        children.setdefault(node.parent_node_id, []).append(node)

    findings: list[Finding] = []
    for parent_id, siblings in sorted(children.items(), key=lambda kv: str(kv[0])):
        parent = nodes.get(parent_id) if parent_id else None
        parent_tokens = (
            parent.total_tokens.billable_total if parent is not None
            else sum(s.total_tokens.billable_total for s in siblings)
        )
        if parent_tokens < min_tokens or len(siblings) < min_siblings:
            continue

        shares = [
            (s, s.total_tokens.billable_total / parent_tokens)
            for s in siblings if parent_tokens
        ]
        sibling_totals = [s.total_tokens.billable_total for s in siblings]
        median = statistics.median(sibling_totals) if sibling_totals else 0

        for node, share in shares:
            tokens = node.total_tokens.billable_total
            if share < share_threshold or tokens < min_tokens:
                continue
            # A median of zero means most siblings did essentially nothing,
            # which is concentration by any reading -- treat it as unbounded
            # rather than skipping it for being undefined.
            times_median = (tokens / median) if median else float("inf")
            if times_median < min_times_median:
                continue
            culprit, via = _most_specific(node, children)
            enclosing = f"{parent.kind} {parent.label[:28]!r}" if parent else "run"
            findings.append(Finding(
                detector=COST_CONCENTRATION,
                summary=(
                    f"{culprit.kind} {culprit.label[:50]!r} holds {share:.0%} of "
                    f"{enclosing} ({tokens:,} of {parent_tokens:,} tokens)"
                ),
                node_id=culprit.node_id, session_id=session_id,
                evidence={
                    "attributed_via": via,
                    "flagged_node": node.node_id,
                    "share_of_parent": round(share, 4),
                    "tokens": tokens,
                    "parent_tokens": parent_tokens,
                    "siblings": len(siblings),
                    "sibling_median_tokens": median,
                    "times_sibling_median": (
                        round(times_median, 2) if median else "no typical sibling"
                    ),
                    "total_usd": node.total_usd,
                    "unpriced_records": node.unpriced_records,
                    "share_threshold": share_threshold,
                    "min_tokens": min_tokens,
                    "min_siblings": min_siblings,
                    "min_times_median": min_times_median,
                    "thresholds_note": (
                        "defaults calibrated against one corpus, not derived "
                        "from a principle; tune per project"
                    ),
                },
            ))
    findings.sort(key=lambda f: -f.evidence["share_of_parent"])
    return findings


# ------------------------------------------------------ outcome divergence

def detect_outcome_divergence(
    groups: Mapping[str, Sequence[tuple[str, Any]]],
    session_id: str = "",
) -> list[Finding]:
    """Agents given the same task that produced different structured outputs.

    `groups` maps a task key to `(agent_id, outcome)` pairs, where `outcome`
    is whatever comparable value the caller extracted -- a verdict field, a
    structured result. Comparison is equality on that value.

    **This requires structured, comparable outputs and does not generalise.**
    Two prose answers to the same question differ in wording without differing
    in substance, and nothing here can tell those apart. Validated against 25
    real verifier triples in which 3 split and 22 agreed; it must flag those 3
    and stay silent on the 22.

    Path divergence is deliberately not consulted. It was measured across the
    same 25 triples and does not predict outcome divergence -- see docs/spec.md
    under "Divergence: a documented negative result".
    """
    findings: list[Finding] = []
    for key, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        outcomes = [outcome for _agent, outcome in members]
        if any(o is None for o in outcomes):
            continue  # an incomplete group cannot be compared honestly
        distinct = {repr(o) for o in outcomes}
        if len(distinct) < 2:
            continue
        counts: dict[str, int] = {}
        for outcome in outcomes:
            counts[repr(outcome)] = counts.get(repr(outcome), 0) + 1
        findings.append(Finding(
            detector=OUTCOME_DIVERGENCE,
            summary=(
                f"{len(members)} agents given the same task returned "
                f"{len(distinct)} different outcomes"
            ),
            session_id=session_id,
            evidence={
                "task_key": key[:120],
                "members": [agent for agent, _ in members],
                "outcome_counts": counts,
                "requires": "structured comparable outputs; does not generalise",
            },
        ))
    return findings


# -------------------------------------------------------- unhandled errors

def detect_unhandled_errors(
    records: Sequence[Any],
    record_scope: Mapping[str, str],
    session_id: str = "",
) -> list[Finding]:
    """Failed calls after which nothing touched the same target again.

    Reports a *shape*, not a verdict, and says so in every finding: **this
    does not claim the agent ignored the error.** Plenty of errors are
    informative -- a file that does not exist, a probe that was meant to fail
    -- and continuing is then the correct behaviour. A detector that called
    those failures would be wrong more often than right.

    A structural proxy, marked `confidence=low`, and measured rather than
    assumed. Two classes of failure where continuing is correct behaviour are
    excluded -- a tool the user declined, and a probe for a file that does not
    exist -- recognised by their harness error template at parse time.

    Hand-labelling the findings on the real corpus, out of 311 `is_error`
    results:

        before excluding benign classes    3 of 14 worth attention  (21%)
        after                              3 of  4 worth attention  (75%)

    The surviving four are a write blocked by an unavailable tool, a write
    that never followed its required read, a malformed search never retried,
    and a ripgrep timeout that is genuinely ambiguous -- so 4 of 4 if the
    timeout counts. **Sample size 4.** That is a handful of findings from one
    corpus, not validation, and the rule stays `confidence=low` because 75%
    of four proves very little.

    Note what did *not* work: `toolDenialKind` marks only 9 of 249 errors, so
    declines cannot be recognised structurally. The error templates can be
    matched instead because they are harness boilerplate -- fixed strings the
    CLI emits, not prompt text or tool arguments -- so only the resulting
    class label is stored, never the message.

    Read the evidence on each finding. This is a shape worth looking at, not
    a defect list.

    Errors whose call names no target are skipped rather than guessed at --
    a failed shell command has no target to follow up on, so the proxy has
    nothing to say about it.
    """
    ordered = sorted(
        (r for r in records if not _field(r, "is_tool_result")),
        key=lambda r: (_field(r, "ts_ns") or 0, _field(r, "uuid") or ""),
    )
    results = {
        _field(r, "tool_use_id"): r
        for r in records if _field(r, "is_tool_result")
    }

    findings: list[Finding] = []
    skipped_no_target = 0
    benign = 0
    unclassified = 0

    for index, call in enumerate(ordered):
        result = results.get(_field(call, "tool_use_id"))
        if result is None or not _field(result, "is_error"):
            continue
        error_class = _field(result, "error_class")
        if error_class in BENIGN_ERROR_CLASSES:
            benign += 1
            continue

        target = _field(call, "target_hash")
        if not target:
            skipped_no_target += 1
            continue

        later = [
            r for r in ordered[index + 1:]
            if _field(r, "target_hash") == target
        ]
        if later:
            continue

        if error_class in (None, "other"):
            unclassified += 1

        node = record_scope.get(_field(call, "uuid") or "")
        tool = _field(call, "tool_name") or "?"
        findings.append(Finding(
            detector=UNHANDLED_ERRORS,
            confidence=LOW,
            summary=(
                f"{tool} failed and nothing afterwards touched the same target"
            ),
            node_id=node, session_id=session_id,
            record_uuids=(_field(call, "uuid"),),
            evidence={
                "tool": tool,
                "error_class": error_class,
                "target_hash": target,
                "later_calls_on_target": 0,
                "position": index,
                "of_calls": len(ordered),
                "reading": (
                    "descriptive, not a verdict: this does not claim the error "
                    "was ignored. Some errors are informative and moving on is "
                    "correct. A structural proxy -- read the run before acting."
                ),
                "skipped_errors_without_a_target": skipped_no_target,
                "measured_precision": (
                    "3 of 4 hand-labelled findings worth attention (75%) "
                    "after excluding benign classes, up from 3 of 14; "
                    "sample size 4, so treat as indicative only"
                ),
            },
        ))
    for finding in findings:
        finding.evidence["skipped_errors_without_a_target"] = skipped_no_target
        finding.evidence["benign_errors_excluded"] = benign
        # A rising share here means the error templates have drifted and
        # benign classes are no longer being recognised.
        finding.evidence["unclassified_errors"] = unclassified
    return findings


# ----------------------------------------------------------------- run all

def run_all(
    records: Sequence[Any],
    record_scope: Mapping[str, str],
    run_cost: Any = None,
    divergence_groups: Mapping[str, Sequence[tuple[str, Any]]] | None = None,
    session_id: str = "",
    **thresholds: Any,
) -> list[Finding]:
    """Every applicable detector, in a stable order."""
    findings = list(detect_redundant_repeats(records, record_scope, session_id))
    if run_cost is not None:
        findings += detect_cost_concentration(
            run_cost, session_id=session_id, **thresholds
        )
    if divergence_groups:
        findings += detect_outcome_divergence(divergence_groups, session_id)
    findings += detect_unhandled_errors(records, record_scope, session_id)
    return findings
