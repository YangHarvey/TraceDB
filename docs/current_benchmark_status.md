# TraceDB Benchmark 当前状态总结

## 1. 当前结论

本轮工作已经把 TraceDB benchmark 从单一 toy `get_trace_tree(trace_id)` 扩展为更接近 Agent observability 的 workload：

- 更真实的数据生成器：thread、long-running trace、大 payload、长尾分布。
- 更完整的 workload：`get_span`、`filter_spans`、`search_text`、`query_running_traces`、`get_thread`、`freshness_probe`。
- RocksDB runner：支持新操作、二级索引、per-op metrics、多 layout。
- ClickHouse baseline：支持 bulk load 后通过持久 HTTP replay 查询 workload。
- 外部系统导出：支持 Elasticsearch/OpenSearch、ClickHouse、Tempo/Jaeger 输入格式。

当前性能结论：

- RocksDB `trace_first_with_indexes` 在本地 direct API / hot-cache 条件下表现为微秒级。
- ClickHouse 在持久 HTTP 条件下表现为毫秒级，过滤/点查/thread/session 查询大多为 `2-4ms`，trace tree 约 `5ms`，文本扫描搜索约几十毫秒。
- 当前 100GB 级实验在这台机器上不建议直接跑，主要受磁盘和 generator 内存模型限制。

## 2. 已完成的主要代码改动

### 2.1 Synthetic generator

文件：

```text
agent-trace-storage-notes/synthetic-agent-trace-generator/generate_agent_traces.py
```

新增能力：

- profile：`smoke` / `dev` / `standard` / `large` / `write_heavy` / `long_running` / `payload_heavy` / `deep_tree`
- thread/conversation：`thread_id`、`conversation_turn`、`previous_trace_id`
- long-running lifecycle：open span、heartbeat、progress update、late event
- payload：`payloads.jsonl`，支持 `inline` / `refs` / `mixed`
- 长尾分布：span count、event count、payload size
- workload mix：可通过 `--workload-mix` 配置查询比例

新增/扩展 operation：

```text
query_trace_tree
get_span
filter_spans
search_text
query_running_traces
query_hot_trace_tree
get_thread
list_traces_by_session
freshness_probe
```

### 2.2 RocksDB runner

文件：

```text
tools/rocksdb_trace_bench.cc
```

新增能力：

- 支持新 query ops。
- 输出 per-op latency、P50/P95/P99、matched rows、returned bytes、scan bytes。
- 输出 freshness、index write amplification、storage amplification。
- 增加索引 keyspace：span id、kind、model、tool、latency、thread、session、word inverted index。
- 支持 layout：

```text
trace_first
trace_first_with_indexes
append_only
update_heavy
time_first
tracelsm_segment
```

### 2.3 ClickHouse benchmark runner

文件：

```text
tools/clickhouse_trace_bench.py
```

能力：

- 创建 ClickHouse benchmark tables。
- 导入 `traces.jsonl` / `spans.jsonl` / `events.jsonl`。
- replay `operations.jsonl` 中的 query ops。
- 输出与 RocksDB 类似的 per-op latency JSON。
- 默认使用 benchmark HTTP 用户 `bench` + 持久 HTTP 连接，避免 `docker exec clickhouse-client` 的进程启动开销。

当前 ClickHouse 是分析型 baseline：

```text
bulk load data -> replay query ops
```

它不逐条 replay 写入，所以 ClickHouse 的 freshness 指标不等价于 RocksDB 的同步写后立即可见。

### 2.4 外部系统导出

文件：

```text
tools/external_baseline_export.py
```

输出：

```text
elasticsearch_bulk.ndjson
clickhouse_spans.tsv
tempo_otel_spans.jsonl
```

## 3. 重要文档

已新增/更新：

```text
docs/agent_observability_benchmark_plan.md
docs/agent_observability_benchmark_usage.md
docs/smithdb_benchmark_notes.md
docs/rocksdb_vs_smithdb_benchmark_summary.md
docs/clickhouse_baseline_setup.md
docs/current_benchmark_status.md
```

## 4. 10k trace 实验结果

结果目录：

```text
bench-results/rocksdb-standard-20260523-0028
```

数据规模：

| Metric | Value |
|---|---:|
| traces | `10,000` |
| spans | `212,435` |
| events | `1,533,055` |
| operations | `2,007,544` |
| RocksDB SST | `1.95GB` |

### 4.1 RocksDB 10k

layout：`trace_first_with_indexes`

| Metric | Value |
|---|---:|
| replay elapsed | `44.19s` |
| end-to-end ops/sec | `45,425` |
| write-path ops/sec | `425,717` |
| query P50/P95/P99 | `1 / 62 / 117µs` |
| freshness P50/P95/P99 | `1 / 1 / 7µs` |
| index write amplification | `5.46x` |
| storage amplification | `2.44x` |

Representative per-op latency：

| Operation | P50 | P95 | P99 |
|---|---:|---:|---:|
| `get_span` | `3µs` | `34µs` | `43µs` |
| `filter_spans` | `33µs` | `50µs` | `61µs` |
| `query_trace_tree` | `60µs` | `119µs` | `159µs` |
| `search_text` | `36µs` | `58µs` | `68µs` |

### 4.2 ClickHouse 10k

runner：持久 HTTP，完整 query replay。

| Metric | Value |
|---|---:|
| queries | `30,290` |
| elapsed | `88.24s` |
| queries/sec | `343.29` |
| query avg | `2.72ms` |
| query P50/P95/P99 | `2.04 / 5.41 / 17.80ms` |

Representative per-op latency：

| Operation | P50 | P95 | P99 |
|---|---:|---:|---:|
| `get_span` | `2.02ms` | `2.30ms` | `3.07ms` |
| `filter_spans` | `2.04ms` | `2.50ms` | `3.05ms` |
| `query_trace_tree` | `5.26ms` | `6.14ms` | `7.10ms` |
| `search_text` | `16.62ms` | `22.76ms` | `23.88ms` |

## 5. 100k trace / 10GB 级实验结果

结果目录：

```text
bench-results/rocksdb-clickhouse-100k-10g-20260523-1222
```

生成参数：

```text
profile=large
num_traces=100000
avg_spans=12
avg_events_per_span=2
max_spans_per_trace=100
query_ratio=0.15
large_payload_ratio=0.02
payload_mode=refs
streaming_event_ratio=0.25
long_running_ratio=0.05
```

数据规模：

| Metric | Value |
|---|---:|
| traces | `100,000` |
| spans | `1,146,489` |
| events | `5,757,742` |
| payloads | `62,983` |
| operations | `8,390,893` |
| query ops | `145,228` |
| payload bytes | `460.96MB` |
| raw/result dir | `13GB` |
| RocksDB dir | `6.4GB` |
| RocksDB SST | `7.55GB` |

Generator：

| Metric | Value |
|---|---:|
| wall time | `1625.73s` |
| max RSS | `9.94GB` |

### 5.1 RocksDB 100k

| Metric | Value |
|---|---:|
| replay elapsed | `175.65s` |
| end-to-end ops/sec | `47,770.8` |
| write-path ops/sec | `366,051` |
| logical writes | `8,245,665` |
| RocksDB writes including indexes | `45,644,281` |
| index write amplification | `4.54x` |
| storage amplification | `2.33x` |
| query P50/P95/P99 | `1 / 69 / 137µs` |
| freshness P50/P95/P99 | `1 / 1 / 2µs` |
| errors | `0` |

Per-op：

| Operation | Count | P50 | P95 | P99 |
|---|---:|---:|---:|---:|
| `get_span` | `6,127` | `7µs` | `65µs` | `83µs` |
| `filter_spans` | `4,595` | `44µs` | `74µs` | `88µs` |
| `query_trace_tree` | `7,523` | `98µs` | `179µs` | `236µs` |
| `search_text` | `2,954` | `49µs` | `86µs` | `101µs` |
| `query_running_traces` | `2,975` | `32µs` | `83µs` | `138µs` |
| `get_thread` | `3,028` | `34µs` | `64µs` | `80µs` |
| `list_traces_by_session` | `1,468` | `39µs` | `72µs` | `85µs` |

### 5.2 ClickHouse 100k

ClickHouse table sizes：

| Table | Rows | Disk |
|---|---:|---:|
| `traces` | `100,000` | `17.55MiB` |
| `traces_by_thread` | `100,000` | `13.70MiB` |
| `traces_by_session` | `100,000` | `14.19MiB` |
| `spans_by_trace` | `1,146,489` | `99.23MiB` |
| `spans_by_kind` | `1,146,489` | `117.21MiB` |
| `events_by_trace` | `5,757,742` | `607.75MiB` |

Import/setup：

| Metric | Value |
|---|---:|
| setup elapsed | `50.49s` |

Query replay：

| Metric | Value |
|---|---:|
| queries | `145,228` |
| elapsed | `489.47s` |
| wall time incl. import | `540.09s` |
| queries/sec | `296.70` |
| query avg | `3.19ms` |
| query P50/P95/P99 | `2.23 / 5.23 / 43.42ms` |

Per-op：

| Operation | Count | P50 | P95 | P99 |
|---|---:|---:|---:|---:|
| `get_span` | `6,127` | `2.21ms` | `2.62ms` | `3.33ms` |
| `filter_spans` | `4,595` | `2.51ms` | `3.53ms` | `3.96ms` |
| `query_trace_tree` | `7,685` | `5.24ms` | `6.11ms` | `7.23ms` |
| `search_text` | `2,954` | `43.35ms` | `57.53ms` | `60.40ms` |
| `query_running_traces` | `2,975` | `3.76ms` | `4.18ms` | `4.71ms` |
| `get_thread` | `3,028` | `2.09ms` | `2.45ms` | `3.02ms` |
| `list_traces_by_session` | `1,468` | `2.02ms` | `2.43ms` | `2.98ms` |

## 6. 当前 RocksDB vs ClickHouse 判断

在当前 benchmark 口径下：

| Workload | RocksDB P50 | ClickHouse P50 |
|---|---:|---:|
| `get_span` | `7µs` | `2.21ms` |
| `filter_spans` | `44µs` | `2.51ms` |
| `query_trace_tree` | `98µs` | `5.24ms` |
| `search_text` | `49µs` | `43.35ms` |
| `query_running_traces` | `32µs` | `3.76ms` |
| `get_thread` | `34µs` | `2.09ms` |
| `list_traces_by_session` | `39µs` | `2.02ms` |

结论：

1. **RocksDB 在 trace-aware indexed layout 下非常适合低延迟 point lookup / trace tree / freshness。**
2. **ClickHouse 的 bulk-loaded analytical baseline 查询稳定在毫秒级。**
3. **ClickHouse 压缩率非常好，列式表总占用远低于 RocksDB。**
4. **ClickHouse 当前 search_text 是 `positionCaseInsensitive` 扫描文本列，不是倒排索引，因此 search 比 RocksDB 的简单 word index 慢。**
5. **RocksDB 当前结果仍是本地 direct API / hot-cache upper bound。ClickHouse 当前是持久 HTTP query baseline。两者仍非完全 apples-to-apples。**

## 7. 100GB 实验可行性

当前机器资源：

| Resource | Value |
|---|---:|
| disk total | `98GB` |
| disk available | about `71GB` before 100k experiment, about `70GB` after cleanup estimate not guaranteed |
| memory | `61GiB` |
| swap | `0` |

不建议直接跑 100GB 的原因：

1. **磁盘不足**
   - 100GB RocksDB SST 通常需要至少 `170GB+` 总空间。
   - 若保留 raw JSON、operations、RocksDB、ClickHouse 数据，建议 `250GB+`。

2. **当前 generator 非 streaming**
   - 100k trace 已经使用约 `9.94GB` RSS。
   - 线性扩展到约 1M trace 可能需要接近 `100GB` RSS，超过当前内存。

3. **生成时间过长**
   - 100k trace 生成已约 `27min`。
   - 100GB 级可能需要数小时。

建议：

```text
先做 streaming/batched generator，再换 250GB-500GB 磁盘环境跑 100GB。
```

当前机器较安全的上限：

| Scale | Estimated RocksDB SST | Risk |
|---|---:|---|
| `300k traces` | `~22GB` | safe |
| `400k traces` | `~30GB` | possible, generator memory high |
| `500k traces` | `~38GB` | risk of memory/disk pressure |

## 8. 下一步建议

优先级：

1. 改造 generator 为 streaming/batched。
2. 实现 external merge sort for `operations.jsonl`。
3. 增加 full hydration 模式：filter/search 命中后读取完整 rows/payload refs。
4. 增加 cold-cache / warm-cache 测试。
5. 增加服务层 benchmark，使 RocksDB 与 ClickHouse 都通过 HTTP/gRPC 访问。
6. 在更大磁盘上跑 100GB+ 实验。

## 9. 一句话总结

当前结果表明：

```text
RocksDB + trace-aware indexes 在 Agent observability 的低延迟查询和 freshness 场景有明显优势；ClickHouse 在列式压缩、批量分析和稳定毫秒级过滤查询方面表现很好。真正的 100GB 对比需要先解决 generator streaming 和磁盘容量问题。
```
