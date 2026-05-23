#!/usr/bin/env python3
"""ClickHouse benchmark runner for Agent observability workloads.

This runner imports generated JSONL data into ClickHouse MergeTree tables and
replays the query portion of operations.jsonl. It is a ClickHouse-specific
filter/analytics baseline, not a write-ingestion equivalent of the RocksDB
runner: write operations are counted and skipped after bulk import.
"""

from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


SPAN_COLUMNS = [
    "trace_id",
    "span_id",
    "parent_span_id",
    "kind",
    "name",
    "status",
    "start_ns",
    "end_ns",
    "latency_ms",
    "model",
    "tool_name",
    "tenant_id",
    "environment",
    "attributes_text",
]

TRACE_COLUMNS = [
    "trace_id",
    "session_id",
    "user_id",
    "thread_id",
    "conversation_turn",
    "previous_trace_id",
    "root_span_id",
    "start_ns",
    "end_ns",
    "status",
    "span_count",
    "event_count",
    "metadata_text",
]

EVENT_COLUMNS = ["event_id", "trace_id", "span_id", "timestamp_ns", "event_type", "payload_text"]

WRITE_OPS = {"insert_trace", "insert_span", "append_event", "update_span", "update_trace"}
QUERY_OPS = {
    "query_trace_tree",
    "query_hot_trace_tree",
    "query_failed_spans",
    "get_span",
    "filter_spans",
    "search_text",
    "query_running_traces",
    "get_thread",
    "list_traces_by_session",
    "freshness_probe",
}


def read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def tsv_value(value: Any) -> str:
    if value is None:
        return r"\N"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def sql_string(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def percentile(values: Sequence[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = (p / 100.0) * (len(ordered) - 1)
    return ordered[int(idx + 0.5)]


@dataclass
class OpStats:
    count: int = 0
    lat_us: List[int] = field(default_factory=list)
    matched_rows: int = 0
    returned_bytes: int = 0

    def add(self, latency_us: int, matched_rows: int, returned_bytes: int) -> None:
        self.count += 1
        self.lat_us.append(latency_us)
        self.matched_rows += matched_rows
        self.returned_bytes += returned_bytes

    def to_json(self) -> Dict[str, Any]:
        avg = sum(self.lat_us) / len(self.lat_us) if self.lat_us else 0.0
        return {
            "count": self.count,
            "avg_us": avg,
            "p50_us": percentile(self.lat_us, 50),
            "p95_us": percentile(self.lat_us, 95),
            "p99_us": percentile(self.lat_us, 99),
            "matched_rows": self.matched_rows,
            "returned_bytes": self.returned_bytes,
        }


class ClickHouse:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        parsed = urllib.parse.urlparse(args.http_url)
        self.http_scheme = parsed.scheme or "http"
        self.http_host = parsed.hostname or "127.0.0.1"
        self.http_port = parsed.port or (8443 if self.http_scheme == "https" else 8123)
        self.http_base_path = parsed.path if parsed.path else "/"
        self.http_conn: Optional[http.client.HTTPConnection] = None

    def client_cmd(self, query: str, use_database: bool = True) -> List[str]:
        base = ["clickhouse-client"]
        if use_database:
            base.extend(["--database", self.args.database])
        base.extend(["--query", query])
        if self.args.container:
            return ["docker", "exec", "-i", self.args.container, *base]
        return base

    def run_client(self, query: str, input_bytes: Optional[bytes] = None, use_database: bool = True) -> None:
        proc = subprocess.run(self.client_cmd(query, use_database=use_database), input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))

    def http_connection(self) -> http.client.HTTPConnection:
        if self.http_conn is None:
            if self.http_scheme == "https":
                self.http_conn = http.client.HTTPSConnection(self.http_host, self.http_port, timeout=self.args.http_timeout)
            else:
                self.http_conn = http.client.HTTPConnection(self.http_host, self.http_port, timeout=self.args.http_timeout)
        return self.http_conn

    def query_http(self, sql: str) -> bytes:
        params = {
            "database": self.args.database,
            "user": self.args.user,
            "wait_end_of_query": "1",
        }
        if self.args.password:
            params["password"] = self.args.password
        path = self.http_base_path + "?" + urllib.parse.urlencode(params)
        body = sql.encode("utf-8")
        headers = {"Content-Type": "text/plain; charset=utf-8", "Connection": "keep-alive"}
        conn = self.http_connection()
        try:
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            if response.status >= 400:
                raise RuntimeError(data.decode("utf-8", errors="replace"))
            return data
        except Exception:
            if self.http_conn is not None:
                self.http_conn.close()
                self.http_conn = None
            raise

    def scalar_int(self, sql: str) -> Tuple[int, int]:
        if self.args.query_transport == "client":
            proc = subprocess.run(self.client_cmd(sql), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
            data = proc.stdout
        else:
            data = self.query_http(sql)
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return 0, len(data)
        return int(text.split("\n", 1)[0].split("\t", 1)[0]), len(data)


def create_schema(ch: ClickHouse, reset: bool) -> None:
    db = ch.args.database
    ch.run_client(f"CREATE DATABASE IF NOT EXISTS {db}", use_database=False)
    if reset:
        for table in ["events_by_trace", "spans_by_kind", "spans_by_trace", "traces_by_session", "traces_by_thread", "traces"]:
            ch.run_client(f"DROP TABLE IF EXISTS {db}.{table}")

    ch.run_client(
        f"""
        CREATE TABLE IF NOT EXISTS {db}.traces
        (
            trace_id String,
            session_id String,
            user_id String,
            thread_id String,
            conversation_turn UInt64,
            previous_trace_id String,
            root_span_id String,
            start_ns UInt64,
            end_ns Nullable(UInt64),
            status LowCardinality(String),
            span_count UInt64,
            event_count UInt64,
            metadata_text String
        ) ENGINE = MergeTree ORDER BY trace_id
        """
    )
    ch.run_client(f"CREATE TABLE IF NOT EXISTS {db}.traces_by_thread AS {db}.traces ENGINE = MergeTree ORDER BY (thread_id, conversation_turn, trace_id)")
    ch.run_client(f"CREATE TABLE IF NOT EXISTS {db}.traces_by_session AS {db}.traces ENGINE = MergeTree ORDER BY (session_id, start_ns, trace_id)")
    span_schema = f"""
        (
            trace_id String,
            span_id String,
            parent_span_id String,
            kind LowCardinality(String),
            name String,
            status LowCardinality(String),
            start_ns UInt64,
            end_ns Nullable(UInt64),
            latency_ms Nullable(UInt64),
            model LowCardinality(String),
            tool_name LowCardinality(String),
            tenant_id LowCardinality(String),
            environment LowCardinality(String),
            attributes_text String
        )
    """
    ch.run_client(f"CREATE TABLE IF NOT EXISTS {db}.spans_by_trace {span_schema} ENGINE = MergeTree ORDER BY (trace_id, span_id)")
    ch.run_client(f"CREATE TABLE IF NOT EXISTS {db}.spans_by_kind {span_schema} ENGINE = MergeTree ORDER BY (kind, start_ns, trace_id, span_id)")
    ch.run_client(
        f"""
        CREATE TABLE IF NOT EXISTS {db}.events_by_trace
        (
            event_id String,
            trace_id String,
            span_id String,
            timestamp_ns UInt64,
            event_type LowCardinality(String),
            payload_text String
        ) ENGINE = MergeTree ORDER BY (trace_id, timestamp_ns, event_id)
        """
    )


def stream_insert(ch: ClickHouse, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    query = f"INSERT INTO {ch.args.database}.{table} ({','.join(columns)}) FORMAT TSVWithNames"
    proc = subprocess.Popen(ch.client_cmd(query), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    count = 0
    proc.stdin.write(("\t".join(columns) + "\n").encode("utf-8"))
    for row in rows:
        proc.stdin.write(("\t".join(tsv_value(v) for v in row) + "\n").encode("utf-8"))
        count += 1
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace") + stdout.decode("utf-8", errors="replace"))
    return count


def trace_rows(path: Path) -> Iterator[List[Any]]:
    for row in read_jsonl(path):
        yield [
            row.get("trace_id"),
            row.get("session_id"),
            row.get("user_id"),
            row.get("thread_id"),
            row.get("conversation_turn") or 0,
            row.get("previous_trace_id") or "",
            row.get("root_span_id"),
            row.get("start_ns") or 0,
            row.get("end_ns"),
            row.get("status") or "",
            row.get("span_count") or 0,
            row.get("event_count") or 0,
            row.get("metadata") or {},
        ]


def span_rows(path: Path) -> Iterator[List[Any]]:
    for row in read_jsonl(path):
        attrs = row.get("attributes") or {}
        yield [
            row.get("trace_id"),
            row.get("span_id"),
            row.get("parent_span_id") or "",
            row.get("kind") or "",
            row.get("name") or "",
            row.get("status") or "",
            row.get("start_ns") or 0,
            row.get("end_ns"),
            row.get("latency_ms"),
            attrs.get("model") or "",
            attrs.get("tool_name") or "",
            attrs.get("tenant_id") or "",
            attrs.get("environment") or "",
            attrs,
        ]


def event_rows(path: Path) -> Iterator[List[Any]]:
    for row in read_jsonl(path):
        yield [
            row.get("event_id"),
            row.get("trace_id"),
            row.get("span_id"),
            row.get("timestamp_ns") or 0,
            row.get("event_type") or "",
            row.get("payload") or {},
        ]


def import_data(ch: ClickHouse, data_dir: Path) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    counts["traces"] = stream_insert(ch, "traces", TRACE_COLUMNS, trace_rows(data_dir / "traces.jsonl"))
    ch.run_client(f"INSERT INTO {ch.args.database}.traces_by_thread SELECT * FROM {ch.args.database}.traces")
    ch.run_client(f"INSERT INTO {ch.args.database}.traces_by_session SELECT * FROM {ch.args.database}.traces")
    counts["traces_by_thread"] = counts["traces"]
    counts["traces_by_session"] = counts["traces"]

    counts["spans_by_trace"] = stream_insert(ch, "spans_by_trace", SPAN_COLUMNS, span_rows(data_dir / "spans.jsonl"))
    ch.run_client(f"INSERT INTO {ch.args.database}.spans_by_kind SELECT * FROM {ch.args.database}.spans_by_trace")
    counts["spans_by_kind"] = counts["spans_by_trace"]

    if (data_dir / "events.jsonl").exists() and not ch.args.skip_events:
        counts["events_by_trace"] = stream_insert(ch, "events_by_trace", EVENT_COLUMNS, event_rows(data_dir / "events.jsonl"))
    else:
        counts["events_by_trace"] = 0
    return counts


def timed_count(ch: ClickHouse, sql: str) -> Tuple[int, int, int]:
    begin = time.perf_counter_ns()
    rows, returned = ch.scalar_int(sql)
    end = time.perf_counter_ns()
    return (end - begin) // 1000, rows, returned


def filter_sql(op: Dict[str, Any]) -> str:
    filt = op.get("filter") or {}
    where: List[str] = []
    table = "spans_by_kind"
    if filt.get("kind"):
        where.append(f"kind = {sql_string(filt['kind'])}")
    if filt.get("model"):
        where.append(f"model = {sql_string(filt['model'])}")
    if filt.get("tool_name"):
        where.append(f"tool_name = {sql_string(filt['tool_name'])}")
    if filt.get("status"):
        where.append(f"status = {sql_string(filt['status'])}")
    if filt.get("min_latency_ms") is not None:
        where.append(f"latency_ms >= {int(filt['min_latency_ms'])}")
    if not where:
        where.append("1")
    limit = int(op.get("limit") or 100)
    return f"SELECT count() FROM {table} WHERE {' AND '.join(where)} LIMIT {limit}"


def run_query(ch: ClickHouse, op: Dict[str, Any]) -> Tuple[str, int, int, int]:
    name = str(op.get("op"))
    if name == "query_hot_trace_tree":
        name = "query_trace_tree"
    if name == "freshness_probe":
        trace_id = sql_string(op.get("trace_id"))
        span_id = sql_string(op.get("span_id"))
        lat, rows, returned = timed_count(ch, f"SELECT count() FROM spans_by_trace WHERE trace_id = {trace_id} AND span_id = {span_id}")
        return "freshness_probe", lat, rows, returned
    if name == "get_span":
        trace_id = sql_string(op.get("trace_id"))
        span_id = sql_string(op.get("span_id"))
        lat, rows, returned = timed_count(ch, f"SELECT count() FROM spans_by_trace WHERE trace_id = {trace_id} AND span_id = {span_id}")
        return "get_span", lat, rows, returned
    if name == "query_trace_tree":
        trace_id = sql_string(op.get("trace_id"))
        begin = time.perf_counter_ns()
        trace_rows_count, ret1 = ch.scalar_int(f"SELECT count() FROM traces WHERE trace_id = {trace_id}")
        span_rows_count, ret2 = ch.scalar_int(f"SELECT count() FROM spans_by_trace WHERE trace_id = {trace_id}")
        event_rows_count, ret3 = ch.scalar_int(f"SELECT count() FROM events_by_trace WHERE trace_id = {trace_id}")
        end = time.perf_counter_ns()
        return "query_trace_tree", (end - begin) // 1000, trace_rows_count + span_rows_count + event_rows_count, ret1 + ret2 + ret3
    if name == "query_failed_spans":
        lat, rows, returned = timed_count(ch, "SELECT count() FROM spans_by_kind WHERE status = 'error'")
        return "query_failed_spans", lat, rows, returned
    if name == "filter_spans":
        lat, rows, returned = timed_count(ch, filter_sql(op))
        return "filter_spans", lat, rows, returned
    if name == "search_text":
        query = sql_string(op.get("query") or "")
        target = op.get("target") or "all"
        if target == "spans":
            sql = f"SELECT count() FROM spans_by_kind WHERE positionCaseInsensitive(name, {query}) > 0 OR positionCaseInsensitive(attributes_text, {query}) > 0"
        elif target == "events":
            sql = f"SELECT count() FROM events_by_trace WHERE positionCaseInsensitive(payload_text, {query}) > 0 OR positionCaseInsensitive(event_type, {query}) > 0"
        else:
            sql = (
                f"SELECT sum(c) FROM ("
                f"SELECT count() AS c FROM spans_by_kind WHERE positionCaseInsensitive(name, {query}) > 0 OR positionCaseInsensitive(attributes_text, {query}) > 0 "
                f"UNION ALL SELECT count() AS c FROM events_by_trace WHERE positionCaseInsensitive(payload_text, {query}) > 0 OR positionCaseInsensitive(event_type, {query}) > 0)"
            )
        lat, rows, returned = timed_count(ch, sql)
        return "search_text", lat, rows, returned
    if name == "query_running_traces":
        limit = int(op.get("limit") or 100)
        lat, rows, returned = timed_count(ch, f"SELECT count() FROM (SELECT trace_id FROM traces WHERE status = 'running' ORDER BY start_ns DESC LIMIT {limit})")
        return "query_running_traces", lat, rows, returned
    if name == "get_thread":
        thread_id = sql_string(op.get("thread_id"))
        limit = int(op.get("limit") or 100)
        lat, rows, returned = timed_count(ch, f"SELECT count() FROM (SELECT trace_id FROM traces_by_thread WHERE thread_id = {thread_id} ORDER BY conversation_turn LIMIT {limit})")
        return "get_thread", lat, rows, returned
    if name == "list_traces_by_session":
        session_id = sql_string(op.get("session_id"))
        limit = int(op.get("limit") or 100)
        lat, rows, returned = timed_count(ch, f"SELECT count() FROM (SELECT trace_id FROM traces_by_session WHERE session_id = {session_id} ORDER BY start_ns LIMIT {limit})")
        return "list_traces_by_session", lat, rows, returned
    raise KeyError(name)


def replay_queries(ch: ClickHouse, operations_path: Path, max_ops: int, max_queries: int) -> Dict[str, Any]:
    stats: Dict[str, OpStats] = defaultdict(OpStats)
    op_counts: Counter[str] = Counter()
    skipped_writes = 0
    unsupported = 0
    lines = 0
    queries = 0
    begin = time.perf_counter()
    for op in read_jsonl(operations_path):
        if max_ops and lines >= max_ops:
            break
        lines += 1
        name = str(op.get("op"))
        op_counts[name] += 1
        if name in WRITE_OPS:
            skipped_writes += 1
            continue
        if name not in QUERY_OPS:
            unsupported += 1
            continue
        if max_queries and queries >= max_queries:
            continue
        try:
            stat_name, lat_us, matched_rows, returned_bytes = run_query(ch, op)
        except Exception as exc:  # keep replay robust and report unsupported query forms
            unsupported += 1
            if ch.args.verbose:
                print(f"query failed for {name}: {exc}", file=sys.stderr)
            continue
        stats[stat_name].add(lat_us, matched_rows, returned_bytes)
        queries += 1
    elapsed = time.perf_counter() - begin
    all_lat = [lat for s in stats.values() for lat in s.lat_us]
    return {
        "operations_path": str(operations_path),
        "lines": lines,
        "queries": queries,
        "skipped_write_ops": skipped_writes,
        "unsupported_ops": unsupported,
        "op_counts": dict(sorted(op_counts.items())),
        "elapsed_sec": elapsed,
        "queries_per_sec": queries / elapsed if elapsed > 0 else 0.0,
        "query_avg_us": sum(all_lat) / len(all_lat) if all_lat else 0.0,
        "query_p50_us": percentile(all_lat, 50),
        "query_p95_us": percentile(all_lat, 95),
        "query_p99_us": percentile(all_lat, 99),
        "ops": {name: stats[name].to_json() for name in sorted(stats)},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ClickHouse Agent observability benchmark runner")
    parser.add_argument("--data-dir", required=True, help="Generated workload directory")
    parser.add_argument("--operations", help="operations.jsonl path; defaults to <data-dir>/operations.jsonl")
    parser.add_argument("--database", default="tracedb_bench")
    parser.add_argument("--container", default="tracedb-clickhouse", help="Docker container name; empty string uses local clickhouse-client")
    parser.add_argument("--http-url", default="http://127.0.0.1:8123")
    parser.add_argument("--http-timeout", type=float, default=60.0)
    parser.add_argument("--user", default="bench", help="ClickHouse HTTP user for --query-transport http")
    parser.add_argument("--password", default="", help="ClickHouse HTTP password for --query-transport http")
    parser.add_argument("--query-transport", choices=["client", "http"], default="http", help="Use clickhouse-client subprocesses or persistent HTTP for replay queries")
    parser.add_argument("--mode", choices=["setup", "run", "all"], default="all")
    parser.add_argument("--reset", action="store_true", help="Drop benchmark tables before setup")
    parser.add_argument("--skip-events", action="store_true", help="Do not import events_by_trace")
    parser.add_argument("--max-ops", type=int, default=0)
    parser.add_argument("--max-queries", type=int, default=0)
    parser.add_argument("--out", help="Write JSON summary to this path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.container == "":
        args.container = None
    return args


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir)
    operations_path = Path(args.operations) if args.operations else data_dir / "operations.jsonl"
    ch = ClickHouse(args)
    summary: Dict[str, Any] = {
        "data_dir": str(data_dir),
        "database": args.database,
        "container": args.container,
        "http_url": args.http_url,
        "query_transport": args.query_transport,
        "user": args.user,
        "mode": args.mode,
    }

    if args.mode in {"setup", "all"}:
        setup_begin = time.perf_counter()
        create_schema(ch, reset=args.reset)
        summary["import_counts"] = import_data(ch, data_dir)
        summary["setup_elapsed_sec"] = time.perf_counter() - setup_begin

    if args.mode in {"run", "all"}:
        summary["benchmark"] = replay_queries(ch, operations_path, args.max_ops, args.max_queries)

    text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
