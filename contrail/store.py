"""SQLite persistence.

SQLite is deliberate for Phase 1: zero setup, one file, easy to inspect with
any client. Phase 4's detectors may want DuckDB for analytical queries -- the
read helpers below are the seam where that swap happens.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import Run, Span

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    span_id               TEXT PRIMARY KEY,
    trace_id              TEXT NOT NULL,
    parent_span_id        TEXT,
    name                  TEXT NOT NULL,
    kind                  INTEGER NOT NULL DEFAULT 0,
    start_ns              INTEGER NOT NULL,
    end_ns                INTEGER NOT NULL,
    duration_ms           REAL    NOT NULL,
    status_code           INTEGER NOT NULL DEFAULT 0,
    status_message        TEXT    NOT NULL DEFAULT '',
    service_name          TEXT    NOT NULL DEFAULT '',
    session_id            TEXT,
    model                 TEXT,
    tool_name             TEXT,
    agent_type            TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    attributes            TEXT    NOT NULL DEFAULT '{}',
    ingested_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_spans_trace   ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_parent  ON spans(parent_span_id);
CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id);
CREATE INDEX IF NOT EXISTS idx_spans_start   ON spans(start_ns DESC);

-- Materialised per-trace summary, rebuilt whenever a trace gains spans.
CREATE TABLE IF NOT EXISTS runs (
    trace_id              TEXT PRIMARY KEY,
    root_name             TEXT NOT NULL,
    session_id            TEXT,
    service_name          TEXT NOT NULL DEFAULT '',
    start_ns              INTEGER NOT NULL,
    end_ns                INTEGER NOT NULL,
    duration_ms           REAL    NOT NULL,
    span_count            INTEGER NOT NULL,
    error_count           INTEGER NOT NULL,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_runs_start ON runs(start_ns DESC);
"""

SPAN_COLUMNS = (
    "span_id", "trace_id", "parent_span_id", "name", "kind",
    "start_ns", "end_ns", "duration_ms", "status_code", "status_message",
    "service_name", "session_id", "model", "tool_name", "agent_type",
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "attributes",
)


class Store:
    def __init__(self, path: str | Path = "contrail.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- write

    def add_spans(self, spans: Iterable[Span]) -> int:
        """Insert spans, then rebuild the run summary for each trace touched.

        Re-exported spans are upserted rather than duplicated: OTLP delivery
        is at-least-once, so the same span id can legitimately arrive twice.
        """
        spans = list(spans)
        if not spans:
            return 0

        placeholders = ", ".join(f":{c}" for c in SPAN_COLUMNS)
        columns = ", ".join(SPAN_COLUMNS)
        sql = f"INSERT OR REPLACE INTO spans ({columns}) VALUES ({placeholders})"

        with self.conn:
            self.conn.executemany(
                sql, [{c: s.to_row()[c] for c in SPAN_COLUMNS} for s in spans]
            )

        for trace_id in {s.trace_id for s in spans}:
            self._refresh_run(trace_id)

        return len(spans)

    def _refresh_run(self, trace_id: str) -> None:
        spans = self.spans_for(trace_id)
        if not spans:
            return
        run = Run.from_spans(spans)
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO runs (
                    trace_id, root_name, session_id, service_name,
                    start_ns, end_ns, duration_ms, span_count, error_count,
                    input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
                """,
                (
                    run.trace_id, run.root_name, run.session_id, run.service_name,
                    run.start_ns, run.end_ns, run.duration_ms,
                    run.span_count, run.error_count,
                    run.input_tokens, run.output_tokens,
                    run.cache_read_tokens, run.cache_creation_tokens,
                ),
            )

    # ----------------------------------------------------------------- read

    def spans_for(self, trace_id: str) -> list[Span]:
        rows = self.conn.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_ns", (trace_id,)
        ).fetchall()
        return [self._row_to_span(r) for r in rows]

    def runs(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY start_ns DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def run(self, trace_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        spans = self.conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
        runs = self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return {"spans": spans, "runs": runs}

    @staticmethod
    def _row_to_span(row: sqlite3.Row) -> Span:
        return Span(
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            name=row["name"],
            kind=row["kind"],
            start_ns=row["start_ns"],
            end_ns=row["end_ns"],
            status_code=row["status_code"],
            status_message=row["status_message"],
            service_name=row["service_name"],
            attributes=json.loads(row["attributes"] or "{}"),
            session_id=row["session_id"],
            model=row["model"],
            tool_name=row["tool_name"],
            agent_type=row["agent_type"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_creation_tokens=row["cache_creation_tokens"],
        )
