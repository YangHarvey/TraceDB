#!/usr/bin/env python3
"""Synthetic Agent Trace Generator.

Generates JSONL files for storage-system experiments around Agent observability:
- traces.jsonl: materialized trace summaries
- spans.jsonl: materialized span rows with parent/child structure
- events.jsonl: append-only events
- operations.jsonl: replayable write/query workload

The generator intentionally uses only Python standard library.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import string
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

NS_PER_MS = 1_000_000
NS_PER_SEC = 1_000_000_000

SPAN_KINDS = ["agent", "chain", "llm", "tool", "retriever", "code", "http"]
LLM_MODELS = ["gpt-4.1-mini", "gpt-4.1", "claude-3.7-sonnet", "qwen-max", "deepseek-v3"]
TOOLS = ["web_search", "read_file", "sql_query", "python_repl", "vector_search", "browser_open"]
RETRIEVERS = ["kb_search", "paper_search", "code_search", "doc_chunk_search"]
ERRORS = ["timeout", "rate_limit", "tool_not_found", "invalid_json", "context_overflow", "http_500"]
EVENT_TYPES = [
    "span_started",
    "span_finished",
    "span_failed",
    "llm_first_token",
    "llm_stream_chunk",
    "tool_call_started",
    "tool_call_finished",
    "retriever_documents",
    "retry_scheduled",
    "log",
]


@dataclass
class Span:
    span_id: str
    trace_id: str
    parent_span_id: Optional[str]
    name: str
    kind: str
    start_ns: int
    end_ns: Optional[int]
    status: str
    latency_ms: Optional[int]
    attributes: Dict[str, Any]
    input_ref: Optional[str] = None
    output_ref: Optional[str] = None
    children: List[str] = field(default_factory=list)


@dataclass
class Trace:
    trace_id: str
    session_id: str
    user_id: str
    root_span_id: str
    start_ns: int
    end_ns: Optional[int]
    status: str
    total_tokens: int
    total_cost_usd: float
    span_count: int
    event_count: int
    metadata: Dict[str, Any]


def nowish_base_ns() -> int:
    # Fixed-ish epoch for reproducible workloads; not wall-clock dependent.
    return 1_790_000_000 * NS_PER_SEC


def random_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def rand_text(rng: random.Random, min_bytes: int, max_bytes: int) -> str:
    size = rng.randint(min_bytes, max_bytes)
    words = []
    alphabet = string.ascii_lowercase
    while sum(len(w) + 1 for w in words) < size:
        wlen = rng.randint(3, 10)
        words.append("".join(rng.choice(alphabet) for _ in range(wlen)))
    return " ".join(words)[:size]


def sample_poisson_like(rng: random.Random, avg: float, minimum: int = 0) -> int:
    # Knuth poisson is slow for large lambda; this approximation is enough for workload generation.
    if avg <= 0:
        return minimum
    if avg < 30:
        lmbda = math.exp(-avg)
        k = 0
        p = 1.0
        while p > lmbda:
            k += 1
            p *= rng.random()
        return max(minimum, k - 1)
    return max(minimum, int(rng.gauss(avg, math.sqrt(avg))))


def choose_kind(rng: random.Random, depth: int) -> str:
    if depth == 0:
        return "agent"
    return rng.choices(
        SPAN_KINDS,
        weights=[1, 3, 7, 5, 3, 2, 2],
        k=1,
    )[0]


def make_span_name(rng: random.Random, kind: str) -> str:
    if kind == "llm":
        return rng.choice(["generate_answer", "summarize_context", "plan_next_step", "judge_result"])
    if kind == "tool":
        return f"tool:{rng.choice(TOOLS)}"
    if kind == "retriever":
        return f"retrieve:{rng.choice(RETRIEVERS)}"
    if kind == "code":
        return "execute_code"
    if kind == "http":
        return "http_request"
    if kind == "chain":
        return rng.choice(["rag_chain", "planner_chain", "reflection_chain", "router_chain"])
    return "agent_run"


def make_attributes(rng: random.Random, kind: str, error: bool) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {
        "tenant_id": f"tenant_{rng.randint(1, 20)}",
        "environment": rng.choice(["prod", "staging", "dev"]),
    }
    if kind == "llm":
        prompt_tokens = rng.randint(100, 8_000)
        completion_tokens = rng.randint(20, 2_000)
        attrs.update(
            {
                "model": rng.choice(LLM_MODELS),
                "temperature": round(rng.uniform(0.0, 1.0), 2),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost_usd": round((prompt_tokens + completion_tokens) * rng.uniform(0.0000002, 0.00001), 6),
            }
        )
    elif kind == "tool":
        attrs.update({"tool_name": rng.choice(TOOLS), "tool_args_bytes": rng.randint(20, 2048)})
    elif kind == "retriever":
        attrs.update({"retriever": rng.choice(RETRIEVERS), "documents": rng.randint(1, 20)})
    elif kind == "http":
        attrs.update({"http_method": rng.choice(["GET", "POST"]), "http_status_code": 500 if error else rng.choice([200, 200, 201, 204])})
    elif kind == "code":
        attrs.update({"language": rng.choice(["python", "sql", "bash"]), "sandbox_ms": rng.randint(10, 3000)})
    if error:
        attrs["error_type"] = rng.choice(ERRORS)
    return attrs


def make_events(
    rng: random.Random,
    span: Span,
    avg_events: float,
    streaming_event_ratio: float,
    retry_ratio: float,
    large_payload_ratio: float,
    payload_min_bytes: int,
    payload_max_bytes: int,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    def add(event_type: str, ts_ns: int, payload: Dict[str, Any]) -> None:
        events.append(
            {
                "event_id": random_id("evt"),
                "trace_id": span.trace_id,
                "span_id": span.span_id,
                "timestamp_ns": ts_ns,
                "event_type": event_type,
                "payload": payload,
            }
        )

    add("span_started", span.start_ns, {"name": span.name, "kind": span.kind})

    if span.kind == "llm":
        first_token_ns = span.start_ns + rng.randint(50, max(51, min(3000, span.latency_ms or 3000))) * NS_PER_MS
        add("llm_first_token", first_token_ns, {"ttft_ms": max(1, (first_token_ns - span.start_ns) // NS_PER_MS)})
        chunk_count = max(1, sample_poisson_like(rng, avg_events * streaming_event_ratio, 1))
        for i in range(chunk_count):
            if span.end_ns and span.end_ns > first_token_ns:
                ts = rng.randint(first_token_ns, span.end_ns)
            else:
                ts = first_token_ns
            payload_size = rng.randint(40, 300)
            if rng.random() < large_payload_ratio:
                payload_size = rng.randint(payload_min_bytes, payload_max_bytes)
            add("llm_stream_chunk", ts, {"chunk_index": i, "text": rand_text(rng, payload_size, payload_size)})

    if span.kind == "tool":
        add("tool_call_started", span.start_ns + rng.randint(1, 20) * NS_PER_MS, {"tool_name": span.attributes.get("tool_name")})

    if span.kind == "retriever":
        add(
            "retriever_documents",
            span.start_ns + rng.randint(1, max(2, span.latency_ms or 2)) * NS_PER_MS,
            {"documents": [{"doc_id": random_id("doc"), "score": round(rng.random(), 4)} for _ in range(int(span.attributes.get("documents", 1)))]},
        )

    if rng.random() < retry_ratio:
        add("retry_scheduled", span.start_ns + rng.randint(1, max(2, span.latency_ms or 2)) * NS_PER_MS, {"retry_after_ms": rng.randint(50, 2000)})

    extra = sample_poisson_like(rng, avg_events, 0)
    for _ in range(extra):
        event_type = rng.choice(["log", "custom_progress", "checkpoint"])
        ts = rng.randint(span.start_ns, span.end_ns or (span.start_ns + 1000 * NS_PER_MS))
        add(event_type, ts, {"message": rand_text(rng, 20, 160)})

    if span.status == "error":
        add("span_failed", span.end_ns or span.start_ns, {"error_type": span.attributes.get("error_type", "unknown")})
    elif span.end_ns is not None:
        add("span_finished", span.end_ns, {"latency_ms": span.latency_ms})

    events.sort(key=lambda e: (e["timestamp_ns"], e["event_id"]))
    return events


def generate_trace(rng: random.Random, args: argparse.Namespace, trace_index: int) -> Tuple[Trace, List[Span], List[Dict[str, Any]]]:
    trace_id = random_id("trc")
    start_ns = nowish_base_ns() + rng.randint(0, args.time_window_seconds * NS_PER_SEC)
    session_id = f"sess_{rng.randint(1, max(1, args.num_traces // 20))}"
    user_id = f"user_{rng.randint(1, max(1, args.num_traces // 10))}"
    is_long_running = rng.random() < args.long_running_ratio
    trace_status = "running" if is_long_running and rng.random() < 0.35 else "success"

    target_spans = max(1, sample_poisson_like(rng, args.avg_spans, 1))
    spans: List[Span] = []
    active_by_depth: List[Span] = []

    root_id = random_id("spn")
    root_latency_ms = rng.randint(1_000, 120_000 if is_long_running else 20_000)
    root_end_ns = None if trace_status == "running" else start_ns + root_latency_ms * NS_PER_MS
    root = Span(
        span_id=root_id,
        trace_id=trace_id,
        parent_span_id=None,
        name="agent_run",
        kind="agent",
        start_ns=start_ns,
        end_ns=root_end_ns,
        status="running" if root_end_ns is None else trace_status,
        latency_ms=None if root_end_ns is None else root_latency_ms,
        attributes={"agent_name": rng.choice(["research_agent", "coding_agent", "data_agent", "browser_agent"]), "turn_index": trace_index},
    )
    spans.append(root)
    active_by_depth.append(root)

    for _ in range(target_spans - 1):
        depth = rng.randint(1, args.max_depth)
        parent_candidates = [s for s in spans if tree_depth(s, spans) < depth]
        parent = rng.choice(parent_candidates or spans)
        parent_depth = tree_depth(parent, spans)
        kind = choose_kind(rng, parent_depth + 1)
        error = rng.random() < args.error_ratio
        status = "error" if error else "success"
        if trace_status == "running" and rng.random() < 0.08:
            status = "running"
        latency_ms = latency_for_kind(rng, kind, is_long_running)
        offset_ms = rng.randint(0, max(1, root_latency_ms - 1))
        span_start_ns = start_ns + offset_ms * NS_PER_MS
        span_end_ns = None if status == "running" else span_start_ns + latency_ms * NS_PER_MS
        if root_end_ns is not None and span_end_ns is not None:
            span_end_ns = min(span_end_ns, root_end_ns)
            latency_ms = max(1, (span_end_ns - span_start_ns) // NS_PER_MS)
        span = Span(
            span_id=random_id("spn"),
            trace_id=trace_id,
            parent_span_id=parent.span_id,
            name=make_span_name(rng, kind),
            kind=kind,
            start_ns=span_start_ns,
            end_ns=span_end_ns,
            status=status,
            latency_ms=None if span_end_ns is None else int(latency_ms),
            attributes=make_attributes(rng, kind, error),
        )
        if rng.random() < args.large_payload_ratio:
            span.input_ref = f"obj://inputs/{span.span_id}"
        if rng.random() < args.large_payload_ratio:
            span.output_ref = f"obj://outputs/{span.span_id}"
        parent.children.append(span.span_id)
        spans.append(span)

    spans.sort(key=lambda s: (s.start_ns, s.span_id))
    all_events: List[Dict[str, Any]] = []
    for span in spans:
        all_events.extend(
            make_events(
                rng,
                span,
                args.avg_events_per_span,
                args.streaming_event_ratio,
                args.retry_ratio,
                args.large_payload_ratio,
                args.payload_min_bytes,
                args.payload_max_bytes,
            )
        )
    all_events.sort(key=lambda e: (e["timestamp_ns"], e["event_id"]))

    total_tokens = sum(int(s.attributes.get("total_tokens", 0)) for s in spans)
    total_cost = sum(float(s.attributes.get("cost_usd", 0.0)) for s in spans)
    if any(s.status == "error" for s in spans):
        trace_status = "error" if trace_status != "running" else "running"
    trace_end_ns = None if trace_status == "running" else max((s.end_ns or s.start_ns) for s in spans)
    trace = Trace(
        trace_id=trace_id,
        session_id=session_id,
        user_id=user_id,
        root_span_id=root_id,
        start_ns=start_ns,
        end_ns=trace_end_ns,
        status=trace_status,
        total_tokens=total_tokens,
        total_cost_usd=round(total_cost, 6),
        span_count=len(spans),
        event_count=len(all_events),
        metadata={"synthetic": True, "workload": "agent_trace", "long_running": is_long_running},
    )
    return trace, spans, all_events


def tree_depth(span: Span, spans: List[Span]) -> int:
    by_id = {s.span_id: s for s in spans}
    depth = 0
    cur = span
    while cur.parent_span_id and cur.parent_span_id in by_id:
        depth += 1
        cur = by_id[cur.parent_span_id]
    return depth


def latency_for_kind(rng: random.Random, kind: str, long_running: bool) -> int:
    ranges = {
        "agent": (500, 120_000),
        "chain": (100, 20_000),
        "llm": (300, 30_000),
        "tool": (50, 8_000),
        "retriever": (30, 3_000),
        "code": (20, 10_000),
        "http": (10, 5_000),
    }
    low, high = ranges.get(kind, (20, 5_000))
    if long_running and rng.random() < 0.1:
        high *= 5
    return rng.randint(low, high)


def trace_to_json(t: Trace) -> Dict[str, Any]:
    return {
        "trace_id": t.trace_id,
        "session_id": t.session_id,
        "user_id": t.user_id,
        "root_span_id": t.root_span_id,
        "start_ns": t.start_ns,
        "end_ns": t.end_ns,
        "status": t.status,
        "total_tokens": t.total_tokens,
        "total_cost_usd": t.total_cost_usd,
        "span_count": t.span_count,
        "event_count": t.event_count,
        "metadata": t.metadata,
    }


def span_to_json(s: Span) -> Dict[str, Any]:
    return {
        "span_id": s.span_id,
        "trace_id": s.trace_id,
        "parent_span_id": s.parent_span_id,
        "name": s.name,
        "kind": s.kind,
        "start_ns": s.start_ns,
        "end_ns": s.end_ns,
        "status": s.status,
        "latency_ms": s.latency_ms,
        "attributes": s.attributes,
        "input_ref": s.input_ref,
        "output_ref": s.output_ref,
        "children": s.children,
    }


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def make_operations(
    rng: random.Random,
    traces: List[Trace],
    spans_by_trace: Dict[str, List[Span]],
    events_by_trace: Dict[str, List[Dict[str, Any]]],
    query_ratio: float,
) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []
    for trace in traces:
        ops.append({"op": "insert_trace", "timestamp_ns": trace.start_ns, "record": {**trace_to_json(trace), "end_ns": None, "status": "running"}})
        for span in spans_by_trace[trace.trace_id]:
            insert_record = span_to_json(span)
            insert_record["end_ns"] = None
            insert_record["status"] = "running"
            insert_record["latency_ms"] = None
            ops.append({"op": "insert_span", "timestamp_ns": span.start_ns, "record": insert_record})
            if span.end_ns is not None:
                ops.append(
                    {
                        "op": "update_span",
                        "timestamp_ns": span.end_ns,
                        "key": {"span_id": span.span_id, "trace_id": span.trace_id},
                        "patch": {"end_ns": span.end_ns, "status": span.status, "latency_ms": span.latency_ms, "attributes": span.attributes},
                    }
                )
        for event in events_by_trace[trace.trace_id]:
            ops.append({"op": "append_event", "timestamp_ns": event["timestamp_ns"], "record": event})
        if trace.end_ns is not None:
            ops.append(
                {
                    "op": "update_trace",
                    "timestamp_ns": trace.end_ns,
                    "key": {"trace_id": trace.trace_id},
                    "patch": {
                        "end_ns": trace.end_ns,
                        "status": trace.status,
                        "total_tokens": trace.total_tokens,
                        "total_cost_usd": trace.total_cost_usd,
                        "span_count": trace.span_count,
                        "event_count": trace.event_count,
                    },
                }
            )
        if rng.random() < query_ratio:
            qts = trace.start_ns + rng.randint(0, max(1, (trace.end_ns or trace.start_ns + 60 * NS_PER_SEC) - trace.start_ns))
            ops.append({"op": "query_trace_tree", "timestamp_ns": qts, "trace_id": trace.trace_id})
        if rng.random() < query_ratio / 2:
            ops.append({"op": "query_failed_spans", "timestamp_ns": trace.start_ns + 1, "start_ns": trace.start_ns - 3600 * NS_PER_SEC, "status": "error"})
    ops.sort(key=lambda o: (o["timestamp_ns"], o["op"]))
    return ops


def make_otel_span_json(span: Span) -> Dict[str, Any]:
    # Simple OTLP-ish JSON; intentionally not protobuf JSON exact, but close enough for mapping tests.
    return {
        "traceId": span.trace_id.replace("trc_", "")[:32].ljust(32, "0"),
        "spanId": span.span_id.replace("spn_", "")[:16].ljust(16, "0"),
        "parentSpanId": None if span.parent_span_id is None else span.parent_span_id.replace("spn_", "")[:16].ljust(16, "0"),
        "name": span.name,
        "kind": span.kind,
        "startTimeUnixNano": str(span.start_ns),
        "endTimeUnixNano": None if span.end_ns is None else str(span.end_ns),
        "status": {"code": "ERROR" if span.status == "error" else "OK" if span.status == "success" else "UNSET"},
        "attributes": {**span.attributes, "agent.span.kind": span.kind},
        "events": [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic Agent trace workload JSONL files.")
    parser.add_argument("--out", default="./synthetic-agent-traces", help="Output directory")
    parser.add_argument("--num-traces", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--avg-spans", type=float, default=18)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--avg-events-per-span", type=float, default=4)
    parser.add_argument("--time-window-seconds", type=int, default=3600)
    parser.add_argument("--long-running-ratio", type=float, default=0.05)
    parser.add_argument("--error-ratio", type=float, default=0.03)
    parser.add_argument("--retry-ratio", type=float, default=0.08)
    parser.add_argument("--large-payload-ratio", type=float, default=0.03)
    parser.add_argument("--streaming-event-ratio", type=float, default=0.35)
    parser.add_argument("--payload-min-bytes", type=int, default=2048)
    parser.add_argument("--payload-max-bytes", type=int, default=16384)
    parser.add_argument("--query-ratio", type=float, default=0.20, help="Probability of query ops per trace in operations.jsonl")
    parser.add_argument("--otel-json", action="store_true", help="Also write otel_spans.jsonl")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    traces: List[Trace] = []
    spans_by_trace: Dict[str, List[Span]] = {}
    events_by_trace: Dict[str, List[Dict[str, Any]]] = {}

    for i in range(args.num_traces):
        trace, spans, events = generate_trace(rng, args, i)
        traces.append(trace)
        spans_by_trace[trace.trace_id] = spans
        events_by_trace[trace.trace_id] = events

    trace_rows = [trace_to_json(t) for t in traces]
    span_rows = [span_to_json(s) for t in traces for s in spans_by_trace[t.trace_id]]
    event_rows = [e for t in traces for e in events_by_trace[t.trace_id]]
    operations = make_operations(rng, traces, spans_by_trace, events_by_trace, args.query_ratio)

    counts = {
        "traces": write_jsonl(out / "traces.jsonl", trace_rows),
        "spans": write_jsonl(out / "spans.jsonl", span_rows),
        "events": write_jsonl(out / "events.jsonl", event_rows),
        "operations": write_jsonl(out / "operations.jsonl", operations),
    }
    if args.otel_json:
        counts["otel_spans"] = write_jsonl(out / "otel_spans.jsonl", (make_otel_span_json(s) for t in traces for s in spans_by_trace[t.trace_id]))

    summary = {
        "args": vars(args),
        "counts": counts,
        "output_files": sorted(str(p.name) for p in out.iterdir() if p.is_file()),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
