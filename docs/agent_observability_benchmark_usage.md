# Agent Observability Benchmark 使用说明

## 1. 生成数据

```bash
python3 agent-trace-storage-notes/synthetic-agent-trace-generator/generate_agent_traces.py \
  --profile smoke \
  --out bench-results/smoke \
  --otel-json
```

内置 profile：

- `smoke`：快速冒烟，200 traces。
- `dev`：本地调试，1k traces。
- `standard`：主实验，10k traces。
- `large`：大规模，100k traces。
- `write_heavy`：高频写入和 streaming event。
- `long_running`：长生命周期 trace、heartbeat、progress、open span。
- `payload_heavy`：大 payload / object ref 压力。
- `deep_tree`：深层嵌套 trace tree。

输出文件：

```text
traces.jsonl
spans.jsonl
events.jsonl
payloads.jsonl
operations.jsonl
otel_spans.jsonl
summary.json
```

## 2. Workload 覆盖

`operations.jsonl` 现在覆盖：

- `insert_trace`
- `insert_span`
- `append_event`
- `update_span`
- `update_trace`
- `query_trace_tree`
- `get_span`
- `filter_spans`
- `search_text`
- `query_running_traces`
- `get_thread`
- `list_traces_by_session`
- `freshness_probe`

可用 `--workload-mix` 调整查询 mix，例如：

```bash
--workload-mix 'query_trace_tree=20,get_span=30,filter_spans=20,search_text=10,get_thread=10,freshness_probe=10'
```

## 3. RocksDB Runner

构建：

```bash
make all
```

运行单 layout：

```bash
build/rocksdb_trace_bench \
  --ops bench-results/smoke/operations.jsonl \
  --db bench-results/smoke/rocksdb-trace-first-indexed-db \
  --layout trace_first_with_indexes \
  > bench-results/smoke/rocksdb_trace_first_with_indexes.json
```

支持 layout：

- `trace_first`
- `trace_first_with_indexes`
- `append_only`
- `update_heavy`
- `time_first`
- `tracelsm_segment`

输出 JSON 包含全局指标和 `ops` 分操作指标：

- `p50_us` / `p95_us` / `p99_us`
- `scanned_keys`
- `scanned_bytes`
- `matched_rows`
- `returned_bytes`
- `read_amplification`
- `index_writes`
- `index_write_amplification`
- `storage_amplification`
- `ingest_to_visible_p50_us/p95_us/p99_us`

## 4. 一键 Benchmark

```bash
make bench-smoke
make bench-dev
make bench-standard
make bench-write-heavy
make bench-long-running
make bench-payload-heavy
make bench-deep-tree
```

多 layout 对比：

```bash
make bench-layouts PROFILE=smoke
make bench-layouts PROFILE=standard
```

## 5. 外部系统基线导出

先生成某个 profile，再导出外部系统输入：

```bash
make external-baseline PROFILE=smoke
```

或直接运行：

```bash
python3 tools/external_baseline_export.py \
  --input bench-results/smoke \
  --out bench-results/smoke/external-baselines
```

输出：

- `elasticsearch_bulk.ndjson`：用于 Elasticsearch/OpenSearch full-text baseline。
- `clickhouse_spans.tsv`：用于 ClickHouse filter/aggregate baseline。
- `tempo_otel_spans.jsonl`：用于 Tempo/Jaeger 类 tracing baseline 导入实验。
