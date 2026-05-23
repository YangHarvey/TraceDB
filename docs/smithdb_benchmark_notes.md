# SmithDB Benchmark 公开信息与 TraceDB Benchmark 设计启发

## 1. 背景

SmithDB 是 LangChain / LangSmith 为 Agent observability 构建的数据层。公开材料显示，它不是用通用数据库 benchmark 来描述性能，而是围绕 LangSmith 的真实 Agent trace 观测场景评估：trace tree 加载、单个 run 访问、过滤、全文搜索、长运行 trace 摄入可见性等。

本文档总结 SmithDB 公开信息，并将其转化为 TraceDB 后续 benchmark 和性能比较的设计依据。

## 2. 公开信息结论

目前没有找到 SmithDB 公开发布的完整可复现 benchmark，包括：

- benchmark 代码
- 数据集
- 硬件配置
- 并发设置
- 数据规模
- 对比系统配置
- 完整实验脚本

公开资料更多是产品/系统层面的性能声明。因此，后续 TraceDB 不应假设存在一个标准 SmithDB benchmark，而应提炼 SmithDB 关注的 workload，构建我们自己的 Agent Observability Benchmark。

## 3. SmithDB 公开性能指标

公开资料中反复出现的 SmithDB 指标如下：

| Workload | 指标 |
|---|---:|
| Trace tree load | P50 约 `92ms` |
| Single run load | P50 约 `71ms` |
| Full-text search | P50 约 `400ms` |
| Run filtering | P50 约 `82ms` |
| 核心 LangSmith 体验 | 约 `12x` / `15x` faster |
| Ingestion 可见性 | median write latency 亚秒级 |

其中 ingestion latency 的口径尤其重要：

```text
从 trace 数据被接收到，到被持久化，并且可以被查询到的时间。
```

这和单纯写入吞吐不同，更贴近 observability 系统的用户体验。

## 4. Run 的含义与 TraceDB 映射

在 LangSmith / SmithDB 语境中，`run` 表示一次可观测的执行单元，例如：

- agent run
- chain run
- llm run
- tool run
- retriever run

它和 OpenTelemetry / tracing 系统里的 `span` 非常接近。

因此在 TraceDB 中可以这样映射：

| SmithDB / LangSmith | TraceDB 当前模型 |
|---|---|
| trace | trace |
| run | span |
| child run | child span |
| run tree | span tree |
| single run load | single span lookup |
| run filtering | span filtering |

后续 benchmark 中不需要单独引入 `run` 表，可以把 SmithDB 的 run workload 翻译为 span workload。

## 5. SmithDB 关注的核心 Workload

### 5.1 Trace tree load

加载一个完整 Agent trace 的嵌套执行树。

对应 TraceDB：

```text
get_trace_tree(trace_id)
```

需要读取：

- trace summary
- all spans
- parent-child tree
- selected events
- token / cost / error summary
- payload refs

这是 Agent observability UI 的核心路径。

### 5.2 Single run load

快速访问某一个 run / span。

对应 TraceDB：

```text
get_span(trace_id, span_id)
get_span_by_id(span_id)
```

这个 workload 用来测试单个执行节点详情页的访问延迟，例如打开某次 LLM 调用、tool 调用或错误节点。

### 5.3 Run filtering

按状态、时间、类型、metadata 或 JSON 属性过滤 run。

对应 TraceDB：

```text
filter_spans(...)
```

典型过滤条件：

```text
status = error
kind = llm
kind = tool
model = gpt-4.1
tool_name = web_search
latency_ms > 5000
attributes.tenant_id = tenant_x
start_ns between [t1, t2]
```

这个 workload 用来评估二级索引、JSON path 过滤和扫描放大。

### 5.4 Full-text search

在 trace / run 内容中搜索文本。

搜索对象可能包括：

- user input
- prompt
- completion
- tool output
- retriever documents
- error message
- event payload

对应 TraceDB 后续能力：

```text
search_trace_text(keyword)
search_span_payload(keyword)
```

当前 TraceDB 尚未覆盖该 benchmark。后续可以先实现简单 baseline，再考虑外部 Elasticsearch / OpenSearch 对比。

### 5.5 Long-running trace ingestion

Agent trace 不是一次性写完，而是持续追加和补全：

```text
insert trace(status=running)
insert span(status=running)
append event
update span(status=success/error)
append more event
update trace(status=success/error)
```

需要关注：

- running trace 是否实时可见
- late-arriving events 如何处理
- open span / open trace 的索引维护成本
- trace 长时间运行时查询是否退化

### 5.6 Ingestion freshness

SmithDB 强调亚秒级 median write latency，即写入后很快可查询。

TraceDB 应新增指标：

```text
ingest_to_query_visible_latency
```

含义：

```text
某条 trace/span/event/update 被写入后，到 get_trace_tree / get_span / filter_spans 能看到它之间的时间。
```

这比单独测 `writes/sec` 更贴近 observability 场景。

### 5.7 Thread / conversation reconstruction

Agent 场景中，一个用户会话可能包含多个 trace，形成多轮 conversation 或 background task thread。

对应 TraceDB：

```text
list_traces_by_session(session_id)
get_thread(thread_id)
reconstruct_conversation(thread_id)
```

当前 synthetic generator 已有 `session_id`，后续可以扩展：

```text
thread_id
conversation_turn
trace_index_in_thread
```

## 6. TraceDB 当前 Benchmark 缺口

当前 TraceDB 已覆盖：

```text
insert_trace
insert_span
append_event
update_span
update_trace
query_trace_tree
query_failed_spans
```

当前 RocksDB baseline 重点测：

```text
get_trace_tree(trace_id)
scanned_keys
scanned_bytes
query_avg_us / p50 / p95 / p99
write throughput
```

相对于 SmithDB 关注点，仍缺少：

1. 单 span / run lookup
2. span filtering
3. JSON attribute filtering
4. full-text / payload search
5. ingestion freshness
6. long-running hot trace 查询
7. thread / session reconstruction
8. mixed UI workload
9. 大 payload 对读写放大的影响
10. filter/search 索引维护带来的写放大

## 7. 建议的 Agent Observability Benchmark

后续可以把 TraceDB benchmark 扩展为以下几类操作。

### 7.1 Core Trace Workload

```text
put_trace(trace)
put_span(span)
append_event(event)
update_span(span_id, patch)
update_trace(trace_id, patch)
get_trace(trace_id)
get_trace_tree(trace_id)
list_spans(trace_id)
list_events(trace_id)
```

目标：验证 trace-first layout 与 `get_trace_tree` 基础性能。

### 7.2 Run / Span Lookup Workload

```text
get_span(trace_id, span_id)
get_span_by_id(span_id)
get_span_events(trace_id, span_id)
```

目标：对应 SmithDB 的 single run load。

### 7.3 Filtering Workload

```text
filter_spans_by_status(status, time_range)
filter_spans_by_kind(kind, time_range)
filter_spans_by_model(model, time_range)
filter_spans_by_tool(tool_name, time_range)
filter_spans_by_latency(kind, min_latency_ms)
filter_spans_by_attribute(json_path, value)
```

目标：对应 SmithDB 的 run filtering 和 JSON key-path filtering。

### 7.4 Search Workload

```text
search_events(keyword)
search_span_payload(keyword)
search_trace_content(keyword)
```

目标：对应 SmithDB 的 full-text search。

第一阶段可以先实现三种 baseline：

1. 无索引扫描
2. 简单倒排索引
3. 外部 Elasticsearch / OpenSearch

### 7.5 Hot Trace Workload

```text
query_running_traces()
get_trace_tree(running_trace_id)
append_event_to_running_trace()
update_open_span()
```

目标：测试 long-running trace 的实时可见性和查询退化。

### 7.6 Thread Reconstruction Workload

```text
list_traces_by_session(session_id)
get_thread(thread_id)
reconstruct_conversation(thread_id)
```

目标：测试多轮 Agent conversation / background task 的重建能力。

### 7.7 Mixed UI Workload

模拟 LangSmith UI 操作比例，例如：

```text
40% get_trace_tree
20% get_span / single run load
15% filter_spans
10% search_trace_text
10% list_thread_traces
5% query_running_traces
```

目标：评估真实 observability UI 交互下的端到端性能。

## 8. 建议指标

### 8.1 查询延迟

```text
P50 / P95 / P99 latency
```

分别统计：

- trace tree load
- single span lookup
- span filtering
- full-text search
- thread reconstruction
- running trace query

### 8.2 写入与摄入

```text
write_ops_per_sec
event_append_ops_per_sec
span_update_ops_per_sec
ingest_to_query_visible_latency P50/P95/P99
```

### 8.3 放大指标

```text
scanned_keys
scanned_bytes
read_amplification
write_amplification
index_write_amplification
storage_amplification
```

### 8.4 Compaction 指标

```text
compaction_time
compaction_cpu_time
bytes_compacted
semantic_compaction_latency
completed_trace_segment_size
```

### 8.5 结果规模指标

```text
matched_spans
matched_events
returned_bytes
payload_bytes_read
payload_refs_resolved
```

## 9. Workload Profile 建议

### 9.1 Smoke

用于开发冒烟测试：

```text
num_traces = 200
avg_spans = 15
avg_events_per_span = 4
query_ratio = 0.2
```

### 9.2 Standard Agent Observability

用于主要 benchmark：

```text
num_traces = 10,000
avg_spans = 20
avg_events_per_span = 5
long_running_ratio = 0.05
error_ratio = 0.03
query_ratio = 0.2
```

### 9.3 Deep Trace

测试大型嵌套 trace tree：

```text
avg_spans = 80
max_depth = 8
avg_events_per_span = 8
```

### 9.4 Write-heavy Background Agents

模拟 background agents 集中产生大量 trace：

```text
num_traces = 50,000+
avg_spans = 30
avg_events_per_span = 8
streaming_event_ratio = 0.6
query_ratio = 0.05
```

### 9.5 Long-running Hot Traces

模拟长时间运行任务：

```text
long_running_ratio = 0.25
open_span_ratio = high
hot_trace_query_ratio = high
partial_update_interval = configurable
```

### 9.6 Payload-heavy

测试 prompt / completion / tool output 大对象压力：

```text
large_payload_ratio = 0.2
payload_min_bytes = 16KB
payload_max_bytes = 1MB+
```

## 10. Baseline 对比建议

后续性能比较可以围绕以下 baseline 展开：

| Baseline | 目的 |
|---|---|
| Plain LSM | 证明普通 KV/LSM 不理解 trace 语义 |
| Time-first LSM | 代表日志式时间优先 layout |
| Trace-first LSM | 代表单 trace 查询友好的基础 layout |
| Append-only LSM | 代表写入优先、查询时 replay 的设计 |
| Update-heavy LSM | 代表直接维护 span/trace 状态的设计 |
| TraceLSM | trace-aware layout + hot/cold + semantic compaction |
| Elasticsearch / OpenSearch | 对比 full-text search |
| ClickHouse | 对比过滤和聚合分析 |
| Jaeger / Tempo | 对比传统 tracing 系统 |

## 11. 对当前实现的直接建议

短期建议按下面顺序强化 benchmark：

```text
P0: 保持 get_trace_tree，补 get_span / single run load
P1: 补 status/kind/model/tool/latency filtering
P2: 补 ingestion freshness 指标
P3: 扩展 generator 支持 thread_id 与更强 long-running workload
P4: 补 full-text / payload search baseline
P5: 形成 mixed UI workload
```

最小闭环可以先实现：

```text
get_trace_tree(trace_id)
get_span(trace_id, span_id)
filter_spans(status/kind/model/tool/latency)
ingest_to_query_visible_latency
```

这样就能更贴近 SmithDB 公开强调的核心 workload。

## 12. 一句话总结

SmithDB 没有公开完整 benchmark，但它清楚表明 Agent observability 数据库的性能核心不是单纯写入吞吐，而是：

```text
快速加载 trace tree，快速访问单个 run/span，支持 run/span 过滤和全文搜索，并让长时间运行、分片到达的 trace 在写入后亚秒级可查询。
```

TraceDB 后续 benchmark 应围绕这些交互构建，而不是只停留在 `get_trace_tree(trace_id)` 单一查询上。
