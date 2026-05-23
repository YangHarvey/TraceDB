#!/usr/bin/env python3
"""Synthetic Agent Trace Generator.

Generates JSONL files for storage-system experiments around Agent observability:
- traces.jsonl: materialized trace summaries
- spans.jsonl: materialized span rows with parent/child structure
- events.jsonl: append-only events
- operations.jsonl: replayable write/query workload
- payloads.jsonl: optional large inline/ref payload corpus

The generator intentionally uses only Python standard library.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import string
import uuid
from collections import Counter
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
TASK_TYPES = ["research", "coding", "support", "data_analysis", "browser_task"]
SEARCH_TERMS = ["timeout", "rate_limit", "retrieval", "tool_output", "reasoning", "checkpoint", "browser", "sql", "python", "memory"]

DEFAULTS: Dict[str, Any] = {
    "num_traces": 1000,
    "avg_spans": 18.0,
    "max_spans_per_trace": 250,
    "max_depth": 4,
    "avg_events_per_span": 4.0,
    "time_window_seconds": 3600,
    "long_running_ratio": 0.05,
    "long_running_min_seconds": 60,
    "long_running_max_seconds": 7200,
    "open_span_ratio": 0.08,
    "progress_event_ratio": 0.15,
    "late_event_ratio": 0.03,
    "hot_trace_query_ratio": 0.10,
    "error_ratio": 0.03,
    "retry_ratio": 0.08,
    "large_payload_ratio": 0.03,
    "streaming_event_ratio": 0.35,
    "payload_min_bytes": 2048,
    "payload_max_bytes": 16384,
    "payload_mode": "mixed",
    "multimodal_ref_ratio": 0.01,
    "query_ratio": 0.35,
    "span_count_dist": "poisson",
    "event_count_dist": "poisson",
    "payload_size_dist": "lognormal",
    "tail_latency_ratio": 0.04,
    "burst_ratio": 0.05,
    "deep_trace_ratio": 0.02,
    "thread_reuse_ratio": 0.68,
    "workload_mix": "query_trace_tree=25,get_span=20,filter_spans=15,search_text=10,query_running_traces=10,get_thread=10,list_traces_by_session=5,freshness_probe=5",
}

PROFILES: Dict[str, Dict[str, Any]] = {
    "smoke": {"num_traces": 200, "avg_spans": 14.0, "query_ratio": 0.55},
    "dev": {"num_traces": 1000, "avg_spans": 18.0, "query_ratio": 0.45},
    "standard": {"num_traces": 10_000, "avg_spans": 22.0, "span_count_dist": "lognormal", "query_ratio": 0.45},
    "large": {"num_traces": 100_000, "avg_spans": 24.0, "span_count_dist": "lognormal", "query_ratio": 0.25},
    "write_heavy": {
        "num_traces": 50_000,
        "avg_spans": 28.0,
        "avg_events_per_span": 10.0,
        "query_ratio": 0.12,
        "burst_ratio": 0.35,
        "streaming_event_ratio": 0.80,
        "workload_mix": "query_trace_tree=25,get_span=25,filter_spans=15,query_running_traces=20,freshness_probe=15",
    },
    "long_running": {
        "num_traces": 10_000,
        "long_running_ratio": 0.25,
        "open_span_ratio": 0.25,
        "progress_event_ratio": 0.45,
        "late_event_ratio": 0.15,
        "hot_trace_query_ratio": 0.35,
        "query_ratio": 0.50,
    },
    "payload_heavy": {
        "num_traces": 10_000,
        "large_payload_ratio": 0.35,
        "payload_mode": "refs",
        "payload_min_bytes": 16_384,
        "payload_max_bytes": 262_144,
        "payload_size_dist": "pareto",
        "query_ratio": 0.30,
    },
    "deep_tree": {
        "num_traces": 10_000,
        "avg_spans": 40.0,
        "max_depth": 9,
        "deep_trace_ratio": 0.30,
        "span_count_dist": "lognormal",
        "query_ratio": 0.40,
    },
}


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
    thread_id: str
    conversation_turn: int
    previous_trace_id: Optional[str]
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
    return 1_790_000_000 * NS_PER_SEC


def random_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def rand_text(rng: random.Random, min_bytes: int, max_bytes: int, keywords: Optional[List[str]] = None) -> str:
    size = max(1, rng.randint(min_bytes, max_bytes))
    words: List[str] = []
    alphabet = string.ascii_lowercase
    if keywords:
        words.extend(keywords)
    while sum(len(w) + 1 for w in words) < size:
        if rng.random() < 0.03:
            words.append(rng.choice(SEARCH_TERMS))
        else:
            wlen = rng.randint(3, 10)
            words.append("".join(rng.choice(alphabet) for _ in range(wlen)))
    rng.shuffle(words)
    return " ".join(words)[:size]


def sample_poisson_like(rng: random.Random, avg: float, minimum: int = 0) -> int:
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


def sample_count(rng: random.Random, avg: float, dist: str, minimum: int, maximum: int) -> int:
    if dist == "lognormal":
        sigma = 0.85
        mu = math.log(max(1.0, avg)) - (sigma * sigma / 2.0)
        value = int(rng.lognormvariate(mu, sigma))
    elif dist == "pareto":
        value = int(max(1.0, avg * (rng.paretovariate(1.8) / 2.2)))
    else:
        value = sample_poisson_like(rng, avg, minimum)
    return max(minimum, min(maximum, value))


def sample_payload_size(rng: random.Random, args: argparse.Namespace) -> int:
    if args.payload_size_dist == "pareto":
        raw = int(args.payload_min_bytes * rng.paretovariate(1.5))
    elif args.payload_size_dist == "lognormal":
        midpoint = max(args.payload_min_bytes, (args.payload_min_bytes + args.payload_max_bytes) // 2)
        sigma = 1.0
        mu = math.log(midpoint) - (sigma * sigma / 2.0)
        raw = int(rng.lognormvariate(mu, sigma))
    else:
        raw = rng.randint(args.payload_min_bytes, args.payload_max_bytes)
    return max(args.payload_min_bytes, min(args.payload_max_bytes, raw))


def choose_kind(rng: random.Random, depth: int) -> str:
    if depth == 0:
        return "agent"
    return rng.choices(SPAN_KINDS, weights=[1, 3, 7, 5, 3, 2, 2], k=1)[0]


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
        attrs.update({"tool_name": rng.choice(TOOLS), "tool_args_bytes": rng.randint(20, 4096)})
    elif kind == "retriever":
        attrs.update({"retriever": rng.choice(RETRIEVERS), "documents": rng.randint(1, 30)})
    elif kind == "http":
        attrs.update({"http_method": rng.choice(["GET", "POST"]), "http_status_code": 500 if error else rng.choice([200, 200, 201, 204])})
    elif kind == "code":
        attrs.update({"language": rng.choice(["python", "sql", "bash"]), "sandbox_ms": rng.randint(10, 3000)})
    if error:
        attrs["error_type"] = rng.choice(ERRORS)
    return attrs


def payload_record(rng: random.Random, trace_id: str, span_id: str, kind: str, args: argparse.Namespace, keywords: Optional[List[str]] = None) -> Dict[str, Any]:
    size = sample_payload_size(rng, args)
    payload_ref = f"obj://payloads/{trace_id}/{span_id}/{kind}/{random_id('pay')}"
    text = rand_text(rng, size, size, keywords=keywords or [kind])
    return {"payload_ref": payload_ref, "trace_id": trace_id, "span_id": span_id, "kind": kind, "bytes": len(text.encode("utf-8")), "text": text}


def attach_payload(
    rng: random.Random,
    trace_id: str,
    span_id: str,
    kind: str,
    args: argparse.Namespace,
    payloads: List[Dict[str, Any]],
    keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    row = payload_record(rng, trace_id, span_id, kind, args, keywords)
    payloads.append(row)
    mode = args.payload_mode
    if mode == "mixed":
        mode = "inline" if rng.random() < 0.5 else "refs"
    if mode == "inline":
        return {"text": row["text"], "bytes": row["bytes"], "payload_kind": kind}
    return {"payload_ref": row["payload_ref"], "bytes": row["bytes"], "payload_kind": kind}


def make_events(rng: random.Random, span: Span, args: argparse.Namespace, is_long_running: bool, payloads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
        if rng.random() < args.large_payload_ratio:
            add("llm_prompt", span.start_ns + NS_PER_MS, attach_payload(rng, span.trace_id, span.span_id, "prompt", args, payloads, ["reasoning", "memory"]))
        first_token_ns = span.start_ns + rng.randint(50, max(51, min(3000, span.latency_ms or 3000))) * NS_PER_MS
        add("llm_first_token", first_token_ns, {"ttft_ms": max(1, (first_token_ns - span.start_ns) // NS_PER_MS)})
        chunk_count = max(1, sample_count(rng, args.avg_events_per_span * args.streaming_event_ratio, args.event_count_dist, 1, 200))
        for i in range(chunk_count):
            ts = rng.randint(first_token_ns, span.end_ns) if span.end_ns and span.end_ns > first_token_ns else first_token_ns
            if rng.random() < args.large_payload_ratio:
                payload = attach_payload(rng, span.trace_id, span.span_id, "completion", args, payloads, ["reasoning", rng.choice(SEARCH_TERMS)])
                payload["chunk_index"] = i
                add("llm_stream_chunk", ts, payload)
            else:
                add("llm_stream_chunk", ts, {"chunk_index": i, "text": rand_text(rng, 40, 300, [rng.choice(SEARCH_TERMS)])})

    if span.kind == "tool":
        add("tool_call_started", span.start_ns + rng.randint(1, 20) * NS_PER_MS, {"tool_name": span.attributes.get("tool_name")})
        if rng.random() < args.large_payload_ratio:
            add("tool_call_finished", (span.end_ns or span.start_ns) + NS_PER_MS, attach_payload(rng, span.trace_id, span.span_id, "tool_output", args, payloads, ["tool_output", str(span.attributes.get("tool_name", "tool"))]))

    if span.kind == "retriever":
        docs = [{"doc_id": random_id("doc"), "score": round(rng.random(), 4), "keyword": rng.choice(SEARCH_TERMS)} for _ in range(int(span.attributes.get("documents", 1)))]
        payload: Dict[str, Any] = {"documents": docs}
        if rng.random() < args.large_payload_ratio:
            payload.update(attach_payload(rng, span.trace_id, span.span_id, "retrieved_doc", args, payloads, ["retrieval", "memory"]))
        add("retriever_documents", span.start_ns + rng.randint(1, max(2, span.latency_ms or 2)) * NS_PER_MS, payload)

    if rng.random() < args.retry_ratio:
        add("retry_scheduled", span.start_ns + rng.randint(1, max(2, span.latency_ms or 2)) * NS_PER_MS, {"retry_after_ms": rng.randint(50, 2000), "reason": rng.choice(ERRORS)})

    extra = sample_count(rng, args.avg_events_per_span, args.event_count_dist, 0, 200)
    for _ in range(extra):
        event_type = rng.choice(["log", "custom_progress", "checkpoint_saved", "agent_state_snapshot", "human_feedback_received"])
        ts = rng.randint(span.start_ns, span.end_ns or (span.start_ns + 1000 * NS_PER_MS))
        add(event_type, ts, {"message": rand_text(rng, 20, 180, [rng.choice(SEARCH_TERMS)])})

    if is_long_running and rng.random() < args.progress_event_ratio:
        horizon = span.end_ns or (span.start_ns + rng.randint(args.long_running_min_seconds, args.long_running_max_seconds) * NS_PER_SEC)
        heartbeat_count = max(1, min(12, int((horizon - span.start_ns) // max(1, 60 * NS_PER_SEC))))
        for i in range(heartbeat_count):
            ts = min(horizon, span.start_ns + (i + 1) * max(1, (horizon - span.start_ns) // (heartbeat_count + 1)))
            add("trace_heartbeat", ts, {"progress_pct": int(100 * (i + 1) / (heartbeat_count + 1)), "message": "checkpoint heartbeat running"})
            if rng.random() < 0.5:
                add("progress_update", ts + NS_PER_MS, {"step": i + 1, "status": "running", "message": "agent progress checkpoint"})

    if span.status == "error":
        if rng.random() < args.large_payload_ratio:
            add("span_failed", span.end_ns or span.start_ns, {"error_type": span.attributes.get("error_type", "unknown"), **attach_payload(rng, span.trace_id, span.span_id, "error_stack", args, payloads, ["timeout", "error"])})
        else:
            add("span_failed", span.end_ns or span.start_ns, {"error_type": span.attributes.get("error_type", "unknown"), "message": "timeout stack trace error"})
    elif span.end_ns is not None:
        add("span_finished", span.end_ns, {"latency_ms": span.latency_ms})

    events.sort(key=lambda e: (e["timestamp_ns"], e["event_id"]))
    return events


def select_thread(rng: random.Random, args: argparse.Namespace, active_threads: List[Dict[str, Any]], trace_index: int) -> Dict[str, Any]:
    if active_threads and rng.random() < args.thread_reuse_ratio:
        thread = rng.choice(active_threads)
    else:
        thread = {
            "thread_id": random_id("thr"),
            "session_id": f"sess_{rng.randint(1, max(1, args.num_traces // 20))}",
            "user_id": f"user_{rng.randint(1, max(1, args.num_traces // 10))}",
            "next_turn": 0,
            "last_trace_id": None,
            "task_type": rng.choice(TASK_TYPES),
        }
        active_threads.append(thread)
    return thread


def generate_trace(
    rng: random.Random,
    args: argparse.Namespace,
    trace_index: int,
    active_threads: List[Dict[str, Any]],
) -> Tuple[Trace, List[Span], List[Dict[str, Any]], List[Dict[str, Any]]]:
    trace_id = random_id("trc")
    thread = select_thread(rng, args, active_threads, trace_index)
    conversation_turn = int(thread["next_turn"])
    previous_trace_id = thread["last_trace_id"]
    thread["next_turn"] = conversation_turn + 1
    thread["last_trace_id"] = trace_id

    start_ns = nowish_base_ns() + rng.randint(0, args.time_window_seconds * NS_PER_SEC)
    if rng.random() < args.burst_ratio:
        start_ns = nowish_base_ns() + (trace_index // 50) * NS_PER_SEC + rng.randint(0, 200 * NS_PER_MS)

    is_long_running = rng.random() < args.long_running_ratio
    trace_status = "running" if is_long_running and rng.random() < 0.35 else "success"
    max_depth = args.max_depth + (rng.randint(2, 5) if rng.random() < args.deep_trace_ratio else 0)
    target_spans = sample_count(rng, args.avg_spans, args.span_count_dist, 1, args.max_spans_per_trace)

    spans: List[Span] = []
    payloads: List[Dict[str, Any]] = []

    root_id = random_id("spn")
    if is_long_running:
        root_latency_ms = rng.randint(args.long_running_min_seconds, args.long_running_max_seconds) * 1000
    else:
        root_latency_ms = latency_for_kind(rng, "agent", False, args)
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
        attributes={
            "agent_name": rng.choice(["research_agent", "coding_agent", "data_agent", "browser_agent"]),
            "turn_index": conversation_turn,
            "thread_id": thread["thread_id"],
            "task_type": thread["task_type"],
        },
    )
    spans.append(root)

    for _ in range(target_spans - 1):
        depth = rng.randint(1, max_depth)
        parent_candidates = [s for s in spans if tree_depth(s, spans) < depth]
        parent = rng.choice(parent_candidates or spans)
        parent_depth = tree_depth(parent, spans)
        kind = choose_kind(rng, parent_depth + 1)
        error = rng.random() < args.error_ratio
        status = "error" if error else "success"
        if is_long_running and rng.random() < args.open_span_ratio:
            status = "running"
        latency_ms = latency_for_kind(rng, kind, is_long_running, args)
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
            row = payload_record(rng, trace_id, span.span_id, "input", args, ["prompt", "memory"])
            payloads.append(row)
            span.input_ref = row["payload_ref"] if args.payload_mode != "inline" else None
            if args.payload_mode == "inline":
                span.attributes["input_preview"] = row["text"]
        if rng.random() < args.large_payload_ratio:
            row = payload_record(rng, trace_id, span.span_id, "output", args, ["completion", "tool_output"])
            payloads.append(row)
            span.output_ref = row["payload_ref"] if args.payload_mode != "inline" else None
            if args.payload_mode == "inline":
                span.attributes["output_preview"] = row["text"]
        if rng.random() < args.multimodal_ref_ratio:
            span.attributes["multimodal_refs"] = [f"obj://images/{span.span_id}/{random_id('img')}.png"]
        parent.children.append(span.span_id)
        spans.append(span)

    spans.sort(key=lambda s: (s.start_ns, s.span_id))
    all_events: List[Dict[str, Any]] = []
    for span in spans:
        all_events.extend(make_events(rng, span, args, is_long_running, payloads))
    all_events.sort(key=lambda e: (e["timestamp_ns"], e["event_id"]))

    if trace_status != "running" and rng.random() < args.late_event_ratio:
        late_ts = (root_end_ns or start_ns) + rng.randint(1, 60) * NS_PER_SEC
        all_events.append(
            {
                "event_id": random_id("evt"),
                "trace_id": trace_id,
                "span_id": root_id,
                "timestamp_ns": late_ts,
                "event_type": "late_event",
                "payload": {"message": "late arriving tool_output checkpoint", "arrival_delay_ms": (late_ts - (root_end_ns or start_ns)) // NS_PER_MS},
            }
        )
        all_events.sort(key=lambda e: (e["timestamp_ns"], e["event_id"]))

    total_tokens = sum(int(s.attributes.get("total_tokens", 0)) for s in spans)
    total_cost = sum(float(s.attributes.get("cost_usd", 0.0)) for s in spans)
    if any(s.status == "error" for s in spans):
        trace_status = "error" if trace_status != "running" else "running"
    trace_end_ns = None if trace_status == "running" else max((s.end_ns or s.start_ns) for s in spans)
    trace = Trace(
        trace_id=trace_id,
        session_id=str(thread["session_id"]),
        user_id=str(thread["user_id"]),
        thread_id=str(thread["thread_id"]),
        conversation_turn=conversation_turn,
        previous_trace_id=previous_trace_id,
        root_span_id=root_id,
        start_ns=start_ns,
        end_ns=trace_end_ns,
        status=trace_status,
        total_tokens=total_tokens,
        total_cost_usd=round(total_cost, 6),
        span_count=len(spans),
        event_count=len(all_events),
        metadata={
            "synthetic": True,
            "workload": "agent_trace",
            "profile": args.profile,
            "thread_id": thread["thread_id"],
            "conversation_turn": conversation_turn,
            "trace_index_in_thread": conversation_turn,
            "previous_trace_id": previous_trace_id,
            "task_type": thread["task_type"],
            "long_running": is_long_running,
        },
    )
    return trace, spans, all_events, payloads


def tree_depth(span: Span, spans: List[Span]) -> int:
    by_id = {s.span_id: s for s in spans}
    depth = 0
    cur = span
    while cur.parent_span_id and cur.parent_span_id in by_id:
        depth += 1
        cur = by_id[cur.parent_span_id]
    return depth


def latency_for_kind(rng: random.Random, kind: str, long_running: bool, args: argparse.Namespace) -> int:
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
    if long_running and rng.random() < 0.30:
        high *= 10
    if rng.random() < args.tail_latency_ratio:
        high *= 5
    return rng.randint(low, high)


def trace_to_json(t: Trace) -> Dict[str, Any]:
    return {
        "trace_id": t.trace_id,
        "session_id": t.session_id,
        "user_id": t.user_id,
        "thread_id": t.thread_id,
        "conversation_turn": t.conversation_turn,
        "previous_trace_id": t.previous_trace_id,
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


def parse_workload_mix(mix: str) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for item in mix.split(","):
        if not item.strip():
            continue
        if "=" not in item:
            result[item.strip()] = 1.0
            continue
        key, value = item.split("=", 1)
        result[key.strip()] = max(0.0, float(value))
    total = sum(result.values())
    if total <= 0:
        return {"query_trace_tree": 1.0}
    return {k: v / total for k, v in result.items() if v > 0}


def choose_query_op(rng: random.Random, weights: Dict[str, float]) -> str:
    ops = list(weights.keys())
    return rng.choices(ops, weights=[weights[o] for o in ops], k=1)[0]


def make_query_op(rng: random.Random, op: str, ts: int, trace: Trace, spans: List[Span]) -> Dict[str, Any]:
    span = rng.choice(spans)
    if op == "get_span":
        return {"op": "get_span", "timestamp_ns": ts, "trace_id": trace.trace_id, "span_id": span.span_id}
    if op == "filter_spans":
        filter_kind = rng.choice(["kind", "model", "tool_name", "status", "latency"])
        filt: Dict[str, Any] = {}
        if filter_kind == "kind":
            filt["kind"] = rng.choice(SPAN_KINDS)
        elif filter_kind == "model":
            filt["kind"] = "llm"
            filt["model"] = rng.choice(LLM_MODELS)
        elif filter_kind == "tool_name":
            filt["kind"] = "tool"
            filt["tool_name"] = rng.choice(TOOLS)
        elif filter_kind == "status":
            filt["status"] = rng.choice(["error", "running", "success"])
        else:
            filt["kind"] = rng.choice(SPAN_KINDS)
            filt["min_latency_ms"] = rng.choice([500, 1000, 5000, 10000])
        return {"op": "filter_spans", "timestamp_ns": ts, "filter": filt, "limit": 100}
    if op == "search_text":
        return {"op": "search_text", "timestamp_ns": ts, "query": rng.choice(SEARCH_TERMS), "target": rng.choice(["spans", "events", "all"]), "limit": 100}
    if op == "query_running_traces":
        return {"op": "query_running_traces", "timestamp_ns": ts, "limit": 100}
    if op == "get_thread":
        return {"op": "get_thread", "timestamp_ns": ts, "thread_id": trace.thread_id, "limit": 100}
    if op == "list_traces_by_session":
        return {"op": "list_traces_by_session", "timestamp_ns": ts, "session_id": trace.session_id, "limit": 100}
    if op == "query_hot_trace_tree":
        return {"op": "query_hot_trace_tree", "timestamp_ns": ts, "trace_id": trace.trace_id}
    if op == "freshness_probe":
        return {"op": "freshness_probe", "timestamp_ns": ts, "query": "get_span", "trace_id": trace.trace_id, "span_id": span.span_id}
    return {"op": "query_trace_tree", "timestamp_ns": ts, "trace_id": trace.trace_id}


def make_operations(
    rng: random.Random,
    traces: List[Trace],
    spans_by_trace: Dict[str, List[Span]],
    events_by_trace: Dict[str, List[Dict[str, Any]]],
    args: argparse.Namespace,
) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []
    weights = parse_workload_mix(args.workload_mix)
    required_queries = ["query_trace_tree", "get_span", "filter_spans", "search_text", "query_running_traces", "get_thread", "list_traces_by_session", "freshness_probe"]

    for trace_index, trace in enumerate(traces):
        trace_row = trace_to_json(trace)
        ops.append({"op": "insert_trace", "timestamp_ns": trace.start_ns, "record": {**trace_row, "end_ns": None, "status": "running"}})
        spans = spans_by_trace[trace.trace_id]
        for span in spans:
            insert_record = span_to_json(span)
            insert_record["end_ns"] = None
            insert_record["status"] = "running"
            insert_record["latency_ms"] = None
            ops.append({"op": "insert_span", "timestamp_ns": span.start_ns, "record": insert_record})
            if rng.random() < args.hot_trace_query_ratio:
                ops.append({"op": "freshness_probe", "timestamp_ns": span.start_ns + 1, "query": "get_span", "trace_id": trace.trace_id, "span_id": span.span_id})
            progress_horizon = span.end_ns or (span.start_ns + rng.randint(30, max(31, args.long_running_min_seconds)) * NS_PER_SEC)
            if span.status == "running" and rng.random() < args.progress_event_ratio:
                for pct in (25, 50, 75):
                    ts = span.start_ns + max(1, (progress_horizon - span.start_ns) * pct // 100)
                    ops.append(
                        {
                            "op": "update_span",
                            "timestamp_ns": ts,
                            "key": {"span_id": span.span_id, "trace_id": span.trace_id},
                            "patch": {"status": "running", "progress_pct": pct, "attributes": {"progress_pct": pct, **span.attributes}},
                        }
                    )
            if span.end_ns is not None:
                ops.append(
                    {
                        "op": "update_span",
                        "timestamp_ns": span.end_ns,
                        "key": {"span_id": span.span_id, "trace_id": span.trace_id},
                        "patch": {"end_ns": span.end_ns, "status": span.status, "latency_ms": span.latency_ms, "kind": span.kind, "attributes": span.attributes},
                    }
                )
        for event in events_by_trace[trace.trace_id]:
            ops.append({"op": "append_event", "timestamp_ns": event["timestamp_ns"], "record": event})
        if trace.end_ns is not None:
            ops.append(
                {
                    "op": "update_trace",
                    "timestamp_ns": trace.end_ns,
                    "key": {"trace_id": trace.trace_id, "start_ns": trace.start_ns},
                    "patch": {
                        "start_ns": trace.start_ns,
                        "end_ns": trace.end_ns,
                        "status": trace.status,
                        "total_tokens": trace.total_tokens,
                        "total_cost_usd": trace.total_cost_usd,
                        "span_count": trace.span_count,
                        "event_count": trace.event_count,
                    },
                }
            )

        duration = max(1, (trace.end_ns or trace.start_ns + 60 * NS_PER_SEC) - trace.start_ns)
        query_count = sample_poisson_like(rng, args.query_ratio, 0)
        if args.query_ratio > 0 and rng.random() < args.query_ratio:
            query_count += 1
        for _ in range(query_count):
            qts = trace.start_ns + rng.randint(0, duration)
            op = choose_query_op(rng, weights)
            ops.append(make_query_op(rng, op, qts, trace, spans))
        if trace.status == "running" and rng.random() < args.hot_trace_query_ratio:
            ops.append({"op": "query_hot_trace_tree", "timestamp_ns": trace.start_ns + rng.randint(1, duration), "trace_id": trace.trace_id})

        if trace_index < len(required_queries):
            qop = required_queries[trace_index]
            ops.append(make_query_op(rng, qop, trace.start_ns + trace_index + 2, trace, spans))

    ops.sort(key=lambda o: (o["timestamp_ns"], o["op"]))
    return ops


def make_otel_span_json(span: Span) -> Dict[str, Any]:
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
    parser.add_argument("--profile", choices=sorted(PROFILES.keys()), default="dev")
    parser.add_argument("--seed", type=int, default=42)
    for key, value in DEFAULTS.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            parser.add_argument(flag, action="store_true", default=None)
        elif isinstance(value, int):
            parser.add_argument(flag, type=int, default=None)
        elif isinstance(value, float):
            parser.add_argument(flag, type=float, default=None)
        else:
            parser.add_argument(flag, default=None)
    parser.add_argument("--otel-json", action="store_true", help="Also write otel_spans.jsonl")
    args = parser.parse_args()

    merged = dict(DEFAULTS)
    merged.update(PROFILES[args.profile])
    for key in DEFAULTS:
        value = getattr(args, key)
        if value is not None:
            merged[key] = value
    for key, value in merged.items():
        setattr(args, key, value)
    if args.payload_mode not in {"inline", "refs", "mixed"}:
        raise SystemExit("--payload-mode must be inline, refs, or mixed")
    for key in ("span_count_dist", "event_count_dist"):
        if getattr(args, key) not in {"poisson", "lognormal", "pareto"}:
            raise SystemExit(f"--{key.replace('_', '-')} must be poisson, lognormal, or pareto")
    if args.payload_size_dist not in {"fixed", "lognormal", "pareto"}:
        raise SystemExit("--payload-size-dist must be fixed, lognormal, or pareto")
    return args


def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    traces: List[Trace] = []
    spans_by_trace: Dict[str, List[Span]] = {}
    events_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    payload_rows: List[Dict[str, Any]] = []
    active_threads: List[Dict[str, Any]] = []

    for i in range(args.num_traces):
        trace, spans, events, payloads = generate_trace(rng, args, i, active_threads)
        traces.append(trace)
        spans_by_trace[trace.trace_id] = spans
        events_by_trace[trace.trace_id] = events
        payload_rows.extend(payloads)

    trace_rows = [trace_to_json(t) for t in traces]
    span_rows = [span_to_json(s) for t in traces for s in spans_by_trace[t.trace_id]]
    event_rows = [e for t in traces for e in events_by_trace[t.trace_id]]
    operations = make_operations(rng, traces, spans_by_trace, events_by_trace, args)
    op_counts = Counter(str(o["op"]) for o in operations)

    counts = {
        "traces": write_jsonl(out / "traces.jsonl", trace_rows),
        "spans": write_jsonl(out / "spans.jsonl", span_rows),
        "events": write_jsonl(out / "events.jsonl", event_rows),
        "payloads": write_jsonl(out / "payloads.jsonl", payload_rows),
        "operations": write_jsonl(out / "operations.jsonl", operations),
    }
    if args.otel_json:
        counts["otel_spans"] = write_jsonl(out / "otel_spans.jsonl", (make_otel_span_json(s) for t in traces for s in spans_by_trace[t.trace_id]))

    summary = {
        "args": vars(args),
        "counts": counts,
        "op_counts": dict(sorted(op_counts.items())),
        "long_running_traces": sum(1 for t in traces if t.metadata.get("long_running")),
        "running_traces": sum(1 for t in traces if t.status == "running"),
        "threads": len({t.thread_id for t in traces}),
        "payload_bytes": sum(int(p.get("bytes", 0)) for p in payload_rows),
        "output_files": sorted(str(p.name) for p in out.iterdir() if p.is_file()),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
