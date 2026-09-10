"""SQLite persistence.

SQLite is deliberate for Phase 1: zero setup, one file, easy to inspect with
any client. Phase 4's detectors may want DuckDB for analytical queries -- the
read helpers below are the seam where that swap happens.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

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

# --- Phase 2: transcript records and reconstructed trees --------------------
#
# Kept in their own tables rather than folded into `spans`. The transcript
# path is deliberately independent of OTel ingest -- trace export is behind a
# beta flag -- and sharing a table would couple the two schemas. `spans`
# already carries `session_id`, and real spans do populate it, so the
# correlation is available to Phase 3 without joining the writes.

TRANSCRIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_records (
    uuid                  TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    agent_id              TEXT,
    parent_uuid           TEXT,
    type                  TEXT NOT NULL,
    timestamp             TEXT NOT NULL DEFAULT '',
    ts_ns                 INTEGER NOT NULL DEFAULT 0,
    is_sidechain          INTEGER NOT NULL DEFAULT 0,
    model                 TEXT,
    request_id            TEXT,
    tool_use_id           TEXT,
    tool_name             TEXT,
    tool_signature        TEXT,
    is_tool_result        INTEGER NOT NULL DEFAULT 0,
    is_error              INTEGER NOT NULL DEFAULT 0,
    result_status         TEXT,
    result_agent_id       TEXT,
    result_run_id         TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    thinking_tokens       INTEGER NOT NULL DEFAULT 0,
    service_tier          TEXT,
    node_id               TEXT,
    text_len              INTEGER NOT NULL DEFAULT 0,
    content               TEXT,
    ingested_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Materialised tree, rebuilt wholesale per session. Same discipline as the
-- `runs` table: a re-parse after new records land must not be able to leave
-- half a tree behind.
CREATE TABLE IF NOT EXISTS tree_nodes (
    node_id               TEXT NOT NULL,
    session_id            TEXT NOT NULL,
    parent_node_id        TEXT,
    kind                  TEXT NOT NULL,
    label                 TEXT NOT NULL DEFAULT '',
    depth                 INTEGER NOT NULL DEFAULT 0,
    ordinal               INTEGER NOT NULL DEFAULT 0,
    agent_id              TEXT,
    agent_type            TEXT,
    tool_use_id           TEXT,
    tool_name             TEXT,
    tool_signature        TEXT,
    record_uuid           TEXT,
    link_basis            TEXT NOT NULL DEFAULT 'none',
    status                TEXT NOT NULL DEFAULT 'ok',
    note                  TEXT NOT NULL DEFAULT '',
    start_ns              INTEGER NOT NULL DEFAULT 0,
    end_ns                INTEGER NOT NULL DEFAULT 0,
    duration_ms           REAL    NOT NULL DEFAULT 0,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    thinking_tokens       INTEGER NOT NULL DEFAULT 0,
    record_count          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, node_id)
);

-- One row per parsed session: where it came from and what could not be read.
CREATE TABLE IF NOT EXISTS transcript_sessions (
    session_id    TEXT PRIMARY KEY,
    project_slug  TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL DEFAULT '',
    record_count  INTEGER NOT NULL DEFAULT 0,
    node_count    INTEGER NOT NULL DEFAULT 0,
    agent_count   INTEGER NOT NULL DEFAULT 0,
    parse_errors  INTEGER NOT NULL DEFAULT 0,
    warnings      TEXT NOT NULL DEFAULT '[]',
    start_ns      INTEGER NOT NULL DEFAULT 0,
    end_ns        INTEGER NOT NULL DEFAULT 0,
    parsed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Indexes are created after _migrate() so that an index can safely
# reference a column a migration has just added.
TRANSCRIPT_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tr_session   ON transcript_records(session_id);
CREATE INDEX IF NOT EXISTS idx_tr_agent     ON transcript_records(agent_id);
CREATE INDEX IF NOT EXISTS idx_tr_tooluse   ON transcript_records(tool_use_id);
CREATE INDEX IF NOT EXISTS idx_tr_signature ON transcript_records(tool_signature);
CREATE INDEX IF NOT EXISTS idx_tr_ts        ON transcript_records(ts_ns);
CREATE INDEX IF NOT EXISTS idx_tn_session ON tree_nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_tn_parent  ON tree_nodes(parent_node_id);
CREATE INDEX IF NOT EXISTS idx_tn_kind    ON tree_nodes(kind);
"""

RECORD_COLUMNS = (
    "uuid", "session_id", "agent_id", "parent_uuid", "type", "timestamp",
    "ts_ns", "is_sidechain", "model", "request_id", "tool_use_id", "tool_name",
    "tool_signature", "is_tool_result", "is_error", "result_status",
    "result_agent_id", "result_run_id", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "cache_write_5m_tokens",
    "cache_write_1h_tokens", "thinking_tokens", "service_tier", "node_id",
    "text_len", "content",
)

NODE_COLUMNS = (
    "node_id", "session_id", "parent_node_id", "kind", "label", "depth",
    "ordinal", "agent_id", "agent_type", "tool_use_id", "tool_name",
    "tool_signature", "record_uuid", "link_basis", "status", "note",
    "start_ns", "end_ns", "duration_ms", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "cache_write_5m_tokens",
    "cache_write_1h_tokens", "thinking_tokens", "record_count",
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
        self.conn.executescript(TRANSCRIPT_SCHEMA)
        self._migrate()
        self.conn.executescript(TRANSCRIPT_INDEXES)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- migrate

    # `CREATE TABLE IF NOT EXISTS` will not add a column to a table that
    # already exists, so a database written by an earlier phase needs the
    # new columns added explicitly. Additive only: every one is nullable or
    # defaulted, so an old row stays valid and no data is rewritten.
    MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        ("transcript_records", "cache_write_5m_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("transcript_records", "cache_write_1h_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("transcript_records", "thinking_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("transcript_records", "service_tier", "TEXT"),
        ("transcript_records", "node_id", "TEXT"),
        ("tree_nodes", "cache_write_5m_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("tree_nodes", "cache_write_1h_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("tree_nodes", "thinking_tokens", "INTEGER NOT NULL DEFAULT 0"),
    )

    def _migrate(self) -> int:
        """Add columns introduced after a database was first created."""
        added = 0
        with self.conn:
            for table, column, decl in self.MIGRATIONS:
                existing = {
                    r["name"]
                    for r in self.conn.execute(f"PRAGMA table_info({table})")
                }
                if not existing or column in existing:
                    continue
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
                added += 1
        return added

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


    # -------------------------------------------------- transcripts (Phase 2)

    def add_transcript_records(
        self, records: Iterable[Any], record_scope: Mapping[str, str] | None = None
    ) -> int:
        """Upsert transcript records, keyed on `uuid`.

        Upserted for the same reason spans are: a session file is appended to
        while it is live, so re-parsing it re-presents records we already
        hold. `uuid` is stable across re-reads, so INSERT OR REPLACE converges
        rather than duplicating.
        """
        records = list(records)
        if not records:
            return 0

        placeholders = ", ".join(f":{c}" for c in RECORD_COLUMNS)
        columns = ", ".join(RECORD_COLUMNS)
        sql = (
            f"INSERT OR REPLACE INTO transcript_records ({columns}) "
            f"VALUES ({placeholders})"
        )
        scope = record_scope or {}
        rows = []
        for rec in records:
            row = {c: getattr(rec, c, None) for c in RECORD_COLUMNS}
            row["is_sidechain"] = int(bool(rec.is_sidechain))
            row["is_tool_result"] = int(bool(rec.is_tool_result))
            row["is_error"] = int(bool(rec.is_error))
            # Persisting which node owns each record means cost can be
            # attributed straight from the database, without re-walking the
            # transcripts to rediscover a mapping the tree build already made.
            row["node_id"] = scope.get(rec.uuid)
            rows.append(row)

        with self.conn:
            self.conn.executemany(sql, rows)
        return len(rows)

    def save_tree(self, tree: Any, *, project_slug: str = "", path: str = "") -> int:
        """Replace a session's materialised tree.

        Wholesale delete-then-insert inside one transaction. A tree is only
        meaningful as a whole -- a partial rebuild could leave a node pointing
        at a parent that no longer exists -- so this mirrors `_refresh_run`
        and stays the single place a tree is written.
        """
        placeholders = ", ".join(f":{c}" for c in NODE_COLUMNS)
        columns = ", ".join(NODE_COLUMNS)
        sql = f"INSERT INTO tree_nodes ({columns}) VALUES ({placeholders})"

        rows = []
        for ordinal, node in enumerate(tree.nodes):
            row = {c: getattr(node, c, None) for c in NODE_COLUMNS}
            row["ordinal"] = ordinal
            row["duration_ms"] = node.duration_ms
            rows.append(row)

        nodes = tree.nodes
        agent_count = sum(1 for n in nodes if n.kind == "subagent")
        start_ns = min((n.start_ns for n in nodes if n.start_ns), default=0)
        end_ns = max((n.end_ns for n in nodes), default=0)

        with self.conn:
            self.conn.execute(
                "DELETE FROM tree_nodes WHERE session_id = ?", (tree.session_id,)
            )
            if rows:
                self.conn.executemany(sql, rows)
            self.conn.execute(
                """
                INSERT OR REPLACE INTO transcript_sessions (
                    session_id, project_slug, path, record_count, node_count,
                    agent_count, parse_errors, warnings, start_ns, end_ns,
                    parsed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))
                """,
                (
                    tree.session_id,
                    project_slug,
                    path,
                    sum(n.record_count for n in nodes),
                    len(nodes),
                    agent_count,
                    tree.parse_errors,
                    json.dumps(tree.warnings),
                    start_ns,
                    end_ns,
                ),
            )
        return len(rows)

    def tree_nodes(self, session_id: str) -> list[dict[str, Any]]:
        """A session's nodes in build order, so a caller can rebuild nesting."""
        rows = self.conn.execute(
            "SELECT * FROM tree_nodes WHERE session_id = ? ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def transcript_records_for(self, session_id: str) -> list[dict[str, Any]]:
        """Every stored record for a session, in time order.

        Returned as plain rows: cost attribution reads token counts,
        `model`, `service_tier` and `node_id` off a mapping just as happily
        as off a dataclass, so nothing needs re-parsing from disk.
        """
        rows = self.conn.execute(
            "SELECT * FROM transcript_records WHERE session_id = ? ORDER BY ts_ns",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def record_scope_for(self, session_id: str) -> dict[str, str]:
        """The persisted record-uuid -> node-id map for a session."""
        rows = self.conn.execute(
            "SELECT uuid, node_id FROM transcript_records "
            "WHERE session_id = ? AND node_id IS NOT NULL",
            (session_id,),
        ).fetchall()
        return {r["uuid"]: r["node_id"] for r in rows}

    def llm_request_spans_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """LLM request spans for a session, for the cross-source check."""
        rows = self.conn.execute(
            "SELECT * FROM spans WHERE session_id = ? AND name LIKE '%llm_request%'",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def transcript_sessions(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM transcript_sessions ORDER BY start_ns DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def transcript_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM transcript_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def repeated_signatures(
        self, session_id: str, min_count: int = 2
    ) -> list[dict[str, Any]]:
        """Tool signatures that occur more than once in a session.

        Not a detector -- Phase 4 owns those, as pure functions over a tree.
        This is the read that proves the signature column is queryable, and
        it is the shape the loop detector will build on.
        """
        rows = self.conn.execute(
            """
            SELECT tool_name, tool_signature, COUNT(*) AS n
              FROM transcript_records
             WHERE session_id = ? AND tool_signature IS NOT NULL
             GROUP BY tool_signature
            HAVING n >= ?
             ORDER BY n DESC, tool_name
            """,
            (session_id, min_count),
        ).fetchall()
        return [dict(r) for r in rows]

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
