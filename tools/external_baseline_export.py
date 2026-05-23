#!/usr/bin/env python3
"""Export generated Agent trace JSONL into simple external-system baseline inputs.

Outputs:
- elasticsearch_bulk.ndjson: spans/events/payloads as bulk index actions for full-text tests.
- clickhouse_spans.tsv: flattened span rows for filter/aggregate tests.
- tempo_otel_spans.jsonl: OTLP-ish span rows for tracing-system import experiments.

This script intentionally uses only Python standard library and does not require the
external systems to be installed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator


def read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_es_bulk(input_dir: Path, output: Path) -> int:
    count = 0
    with output.open("w", encoding="utf-8") as f:
        for name in ("traces", "spans", "events", "payloads"):
            for row in read_jsonl(input_dir / f"{name}.jsonl"):
                doc_id = row.get("trace_id", "")
                if row.get("span_id"):
                    doc_id += ":" + str(row["span_id"])
                if row.get("event_id"):
                    doc_id += ":" + str(row["event_id"])
                if row.get("payload_ref"):
                    doc_id += ":" + str(row["payload_ref"])
                f.write(json.dumps({"index": {"_index": f"agent-{name}", "_id": doc_id}}, separators=(",", ":")) + "\n")
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                count += 1
    return count


def write_clickhouse_spans(input_dir: Path, output: Path) -> int:
    count = 0
    fields = [
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
    ]
    with output.open("w", encoding="utf-8") as f:
        f.write("\t".join(fields) + "\n")
        for row in read_jsonl(input_dir / "spans.jsonl"):
            attrs = row.get("attributes") or {}
            flat = {**row, **attrs}
            f.write("\t".join("" if flat.get(k) is None else str(flat.get(k)).replace("\t", " ").replace("\n", " ") for k in fields) + "\n")
            count += 1
    return count


def write_tempo_otel(input_dir: Path, output: Path) -> int:
    count = 0
    with output.open("w", encoding="utf-8") as f:
        for span in read_jsonl(input_dir / "spans.jsonl"):
            row = {
                "traceId": str(span["trace_id"]).replace("trc_", "")[:32].ljust(32, "0"),
                "spanId": str(span["span_id"]).replace("spn_", "")[:16].ljust(16, "0"),
                "parentSpanId": None if span.get("parent_span_id") is None else str(span["parent_span_id"]).replace("spn_", "")[:16].ljust(16, "0"),
                "name": span.get("name"),
                "kind": span.get("kind"),
                "startTimeUnixNano": str(span.get("start_ns")),
                "endTimeUnixNano": None if span.get("end_ns") is None else str(span.get("end_ns")),
                "status": {"code": "ERROR" if span.get("status") == "error" else "OK" if span.get("status") == "success" else "UNSET"},
                "attributes": {**(span.get("attributes") or {}), "agent.span.kind": span.get("kind")},
                "events": [],
            }
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Export generated benchmark data for external-system baselines.")
    parser.add_argument("--input", required=True, help="Generated workload directory containing traces/spans/events/payloads JSONL")
    parser.add_argument("--out", required=True, help="Output directory")
    args = parser.parse_args()

    input_dir = Path(args.input)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    counts = {
        "elasticsearch_bulk_docs": write_es_bulk(input_dir, out / "elasticsearch_bulk.ndjson"),
        "clickhouse_spans": write_clickhouse_spans(input_dir, out / "clickhouse_spans.tsv"),
        "tempo_otel_spans": write_tempo_otel(input_dir, out / "tempo_otel_spans.jsonl"),
    }
    (out / "summary.json").write_text(json.dumps({"input": str(input_dir), "counts": counts}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"input": str(input_dir), "counts": counts}, indent=2))


if __name__ == "__main__":
    main()
