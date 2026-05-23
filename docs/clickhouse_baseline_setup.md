# ClickHouse Baseline Setup

## 当前部署

本机通过 Docker 启动了 ClickHouse：

```text
container: tracedb-clickhouse
image: clickhouse/clickhouse-server:24.8
http port: 8123
tcp port: 9000
volume: tracedb-clickhouse-data:/var/lib/clickhouse
```

验证：

```bash
docker exec tracedb-clickhouse clickhouse-client --query 'SELECT version()'
```

当前版本：

```text
24.8.14.39
```

ClickHouse 是开源列式 OLAP 数据库，核心项目采用 Apache License 2.0。

## 专用 benchmark runner

已新增 ClickHouse 专用 runner：

```text
tools/clickhouse_trace_bench.py
```

它会：

1. 创建 ClickHouse benchmark 表。
2. 从 generator 输出的 `traces.jsonl` / `spans.jsonl` / `events.jsonl` 导入数据。
3. replay `operations.jsonl` 中的查询操作。
4. 输出类似 RocksDB runner 的 per-op latency JSON。

运行示例：

```bash
python3 tools/clickhouse_trace_bench.py \
  --data-dir bench-results/rocksdb-standard-20260523-0028 \
  --database tracedb_standard \
  --mode all \
  --reset \
  --out bench-results/rocksdb-standard-20260523-0028/clickhouse_bench.json
```

也可以用 Makefile：

```bash
make clickhouse-bench PROFILE=smoke
make clickhouse-bench PROFILE=standard
```

默认 Makefile 使用：

```text
CLICKHOUSE_QUERY_TRANSPORT=http
CLICKHOUSE_MAX_QUERIES=5000
```

当前已创建 benchmark 专用 HTTP 用户：

```sql
CREATE USER IF NOT EXISTS bench IDENTIFIED WITH no_password HOST ANY;
GRANT SELECT, INSERT, CREATE, DROP, ALTER ON *.* TO bench;
```

因此默认通过持久 HTTP 连接查询：

```text
CLICKHOUSE_QUERY_TRANSPORT=http
```

这比 `docker exec ... clickhouse-client` 更公平，因为它避免了每条 query 的 Docker exec 和 client 进程启动开销。若需要回退到客户端子进程模式，可使用：

```bash
make clickhouse-bench PROFILE=standard CLICKHOUSE_QUERY_TRANSPORT=client CLICKHOUSE_MAX_QUERIES=5000
```

注意：当前 ClickHouse runner 是 **bulk-load 后 replay query ops** 的分析型 baseline。它不会逐条 replay `insert_trace` / `append_event` / `update_span` 写入，因此 freshness 指标表示 bulk-loaded 数据上的可见性查询延迟，不等价于 RocksDB 同步写后立即可见的 ingestion freshness。

## 10k trace HTTP benchmark 结果

完整结果：

```text
bench-results/rocksdb-standard-20260523-0028/clickhouse_bench_full_http.json
bench-results/rocksdb-standard-20260523-0028/clickhouse_bench_full_http.time
```

规模：`10,000` traces、`212,435` spans、`1,533,055` events。

总体：

| Metric | Value |
|---|---:|
| queries | `30,290` |
| elapsed | `88.24s` |
| queries/sec | `343.29` |
| query avg | `2.72ms` |
| query P50 | `2.04ms` |
| query P95 | `5.41ms` |
| query P99 | `17.80ms` |

分操作：

| Operation | Count | P50 | P95 | P99 |
|---|---:|---:|---:|---:|
| `get_span` | `1,831` | `2.02ms` | `2.30ms` | `3.07ms` |
| `filter_spans` | `1,319` | `2.04ms` | `2.50ms` | `3.05ms` |
| `query_trace_tree` | `2,370` | `5.26ms` | `6.14ms` | `7.10ms` |
| `search_text` | `902` | `16.62ms` | `22.76ms` | `23.88ms` |
| `query_running_traces` | `882` | `1.98ms` | `2.26ms` | `2.83ms` |
| `get_thread` | `920` | `2.13ms` | `2.44ms` | `3.17ms` |
| `list_traces_by_session` | `511` | `2.01ms` | `2.30ms` | `2.81ms` |

相比 `docker exec clickhouse-client` 模式，P50 从约 `68.9ms` 降到 `2.04ms`，主要因为消除了每条 query 的 Docker exec 和进程启动开销。

## 已导入数据

来源：

```text
bench-results/rocksdb-standard-20260523-0028
```

导出：

```bash
python3 tools/external_baseline_export.py \
  --input bench-results/rocksdb-standard-20260523-0028 \
  --out bench-results/rocksdb-standard-20260523-0028/external-baselines
```

ClickHouse 表：

```text
database: tracedb
table: spans
engine: MergeTree
order by: (kind, start_ns, trace_id, span_id)
```

行数：

```text
rows: 212435
traces: 10000
spans: 212435
```

## 常用命令

进入客户端：

```bash
docker exec -it tracedb-clickhouse clickhouse-client
```

查询 span 分布：

```sql
SELECT kind, count()
FROM tracedb.spans
GROUP BY kind
ORDER BY count() DESC;
```

过滤 LLM spans：

```sql
SELECT count()
FROM tracedb.spans
WHERE kind = 'llm';
```

按模型聚合：

```sql
SELECT model, count()
FROM tracedb.spans
WHERE kind = 'llm'
GROUP BY model
ORDER BY count() DESC;
```

停止服务：

```bash
docker stop tracedb-clickhouse
```

重新启动：

```bash
docker start tracedb-clickhouse
```
