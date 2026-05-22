# TraceLSM：基于 LSM-tree 的 Agent Trace 存储研究想法

## 1. 核心判断

这个方向有论文潜力，但不能只做成：

```text
用 LSM-tree / RocksDB 存 trace、span、event
```

那样更像工程实现，研究贡献不够清晰。

更有论文价值的切入点是：

> Agent trace 是一种新的存储 workload，它同时具有高频追加、少量状态更新、树状结构还原、长生命周期、冷热分层、大 payload、半结构化索引和实时可见性需求。传统 LSM-tree 虽然适合高写入，但并不理解 trace 的生命周期和结构，因此会遇到读放大、写放大、compaction 无语义、查询低效等问题。

因此论文应聚焦在：

```text
Agent trace workload 如何改变 LSM-tree 的 layout、compaction 和 materialization 设计
```

---

## 2. 推荐论文方向

### 方向名称

可以暂定为：

```text
TraceLSM: An LSM-based Storage Engine for Agent Execution Traces
```

更聚焦的题目可以是：

```text
Trace-aware Compaction for LSM-based Agent Observability Stores
```

其中第二个更像系统论文，因为它强调一个明确机制：

> trace-aware compaction

---

## 3. 核心研究问题

传统 LSM-tree 对写入友好，但 Agent trace 有特殊结构：

```text
session / thread
  trace
    span tree
      events
```

一次 trace 不是普通日志，也不是普通 KV，而是一个长时间运行、不断追加和补全状态的执行过程。

核心问题包括：

1. 如何高吞吐写入 `trace/span/event`？
2. 如何快速加载单个 trace 的完整 span tree？
3. 如何处理 `span_started -> span_finished` 这种状态补全？
4. 如何降低 completed trace 的读放大？
5. 如何把 running trace 和 completed trace 分层管理？
6. 如何在不牺牲太多写入吞吐的情况下维护必要索引？
7. 如何处理 prompt、completion、tool output 等大字段？

---

## 4. 为什么普通 LSM-tree 不够

### 4.1 Key layout 难选

如果按 `trace_id` 排序：

```text
trace_id + timestamp + event_id -> event
```

优点：

- 单 trace 加载快
- span tree 还原方便

缺点：

- 全局时间范围查询弱
- 最近错误查询弱
- ingestion 顺序性不一定好

如果按 `timestamp` 排序：

```text
timestamp + trace_id + event_id -> event
```

优点：

- 时间范围查询快
- 写入更接近顺序

缺点：

- 单 trace 的 event 分散
- trace 详情页加载慢

因此需要研究 trace-aware / hybrid layout。

---

### 4.2 Update 在 LSM 中不是免费

Agent trace 中，`span` 和 `trace` 经常需要后续补字段：

```text
end_time
status
latency_ms
total_tokens
total_cost
error
output_ref
```

在 LSM 中，update 本质是追加新版本：

```text
PUT span_1 status=running
PUT span_1 status=success
```

旧版本需要等 compaction 清理。

因此会带来：

- 写放大
- 读放大
- compaction 压力
- 旧版本堆积
- tombstone 堆积

---

### 4.3 单 trace 加载有读放大

典型查询是：

```text
get_trace_tree(trace_id)
```

它需要读取：

- trace summary
- 所有 spans
- 所有 events
- error 信息
- token/cost 信息
- 部分 input/output 引用

如果这些记录分散在多个 SSTable 和多个 level 中，就会导致读放大。

---

### 4.4 Span tree 不是 LSM 的天然模型

Span 是树状结构：

```text
span_id
parent_span_id
trace_id
```

LSM 擅长：

```text
point lookup
range scan
```

但不天然支持：

```text
递归找 children
按 parent_span_id 组树
```

因此可能需要额外索引：

```text
trace_id + parent_span_id + span_id -> span_ref
```

但这会增加写放大。

---

### 4.5 二级索引维护成本高

Agent trace 查询通常需要：

```text
status
kind
model
tool_name
error_type
latency_ms
timestamp
session_id
user_id
```

如果为每个维度建立索引，每写入一个 span/event，就可能要多写多个 keyspace。

这会导致：

```text
查询快，但写放大高
写入快，但查询慢
```

论文可以研究这个 tradeoff。

---

### 4.6 大 payload 不适合直接进入 LSM

Agent trace 中有很多大字段：

```text
prompt
completion
tool result
retrieved documents
网页正文
代码输出
错误栈
附件
```

如果直接放进 LSM value，会导致：

- SSTable 膨胀
- compaction 搬运大对象
- block cache 被污染
- metadata 查询读放大
- 写放大严重

更合理的方式是：

```text
LSM 存 metadata + object_ref
Blob/Object Store 存大 payload
```

---

### 4.7 Streaming event 会放大写入

LLM 输出是流式的：

```text
token_1
token_2
token_3
...
```

如果每个 token 都写成 event，会产生大量小 KV：

- WAL 压力
- memtable 压力
- flush 频繁
- L0 文件增加
- compaction 压力增加

更合理的是：

```text
按 chunk 聚合
每 N 个 token flush
每 100ms flush
span 结束时写 summary
```

---

## 5. 最有潜力的论文点：Trace-aware Compaction

普通 LSM compaction 做的是：

```text
合并 SSTable
清理旧版本
清理 tombstone
```

但 Agent trace 需要的是语义化合并：

```text
span_started + span_finished -> span final state
多个 llm_stream_chunk -> output summary / output_ref
多个 child span -> parent aggregate
多个 event -> trace summary
completed trace -> compacted trace segment
```

这可以形成一个明确研究贡献：

> Trace-aware compaction understands trace lifecycle and span tree semantics, and compacts completed traces into query-efficient segments while preserving high ingest throughput.

---

## 6. 可能的系统设计

### 6.1 Hot layer

用于 running trace：

```text
Hot LSM
- append event
- insert span
- update running span state
- support real-time visibility
```

特点：

- 写入频繁
- 查询实时
- 数据仍在变化

---

### 6.2 Freeze 阶段

当 trace 完成时：

```text
trace_finished event 到达
-> freeze trace
-> 收集该 trace 的 spans/events
-> 进行 semantic compaction
```

---

### 6.3 Cold segment

完成后的 trace 可以 compact 成连续 segment：

```text
Trace Segment
├─ trace summary
├─ span table
├─ parent-child index
├─ event blocks
├─ token/cost summary
└─ payload refs
```

特点：

- 基本只读
- 适合压缩
- 单 trace 加载快
- 减少跨 SSTable 读放大

---

## 7. MVP 范围

不建议一开始做完整 SmithDB。

更合理的 MVP：

```text
LSM-based Agent Trace Store
= append-only event store
+ span materialized view
+ trace_id range scan
+ parent_span_id tree index
+ trace-aware compaction
+ hot/cold separation
```

先支持这些查询：

```text
get_trace(trace_id)
get_trace_tree(trace_id)
list_spans(trace_id)
list_events(trace_id)
get_children(trace_id, parent_span_id)
query_running_traces()
query_error_spans_by_time()
```

暂时弱化：

```text
全文搜索
复杂 JSON path 查询
大规模 OLAP 聚合
跨 trace join
```

---

## 8. 可能的 Keyspace 设计

### Hot LSM

```text
traces:
  T#{trace_id} -> trace_summary

spans_by_trace:
  S#{trace_id}#{span_id} -> span_state

events_by_trace:
  E#{trace_id}#{timestamp_ns}#{event_id} -> event

children_by_parent:
  C#{trace_id}#{parent_span_id}#{span_id} -> span_ref

running_traces:
  R#{start_time}#{trace_id} -> empty

error_spans:
  X#{timestamp_ns}#{trace_id}#{span_id} -> span_ref
```

### Cold Segment

```text
segment_by_trace:
  G#{trace_id} -> trace_segment_ref
```

其中 `trace_segment` 可以放在本地文件或对象存储中。

---

## 9. Baseline 设计

论文实验至少需要比较：

### Baseline 1：Plain LSM

```text
trace/span/event 直接 KV 存储
无 trace-aware compaction
查询时从 LSM 读取并组装
```

### Baseline 2：Time-first Layout

```text
timestamp + trace_id + event_id -> event
```

适合时间扫描，但单 trace 加载差。

### Baseline 3：Trace-first Layout

```text
trace_id + timestamp + event_id -> event
```

适合单 trace 加载，但时间范围查询弱。

### Baseline 4：Update-heavy Design

```text
span/trace 状态直接 update
```

测试 update 对 LSM compaction 的影响。

### Baseline 5：Append-only Design

```text
只 append event
查询时重放 event 生成 span/trace 状态
```

测试查询时 materialization 的成本。

### Proposed：TraceLSM

```text
append-only hot write
+ materialized span view
+ trace-aware compaction
+ completed trace segment
+ hot/cold separation
```

---

## 10. 评估指标

需要评估：

```text
写入吞吐
写放大
读放大
compaction 开销
单 trace 加载 P50/P95/P99
running trace 查询延迟
completed trace 查询延迟
span tree reconstruction latency
存储空间放大
索引维护成本
```

特别重要的是：

```text
get_trace_tree(trace_id) latency
```

这是 Agent observability UI 最核心的查询之一。

---

## 11. Workload 设计

可以使用本目录中的 synthetic generator：

```text
agent-trace-storage-notes/synthetic-agent-trace-generator/
```

它生成：

```text
traces.jsonl
spans.jsonl
events.jsonl
operations.jsonl
otel_spans.jsonl
```

其中：

- `operations.jsonl` 适合模拟 update-heavy / replay workload
- `events.jsonl` 适合模拟 append-only event store
- `spans.jsonl` / `traces.jsonl` 适合作为 materialized view ground truth

可调参数包括：

```text
num_traces
avg_spans
avg_events_per_span
max_depth
long_running_ratio
error_ratio
retry_ratio
large_payload_ratio
streaming_event_ratio
query_ratio
```

---

## 12. 论文风险

### 风险 1：Agent trace workload 还不标准

目前没有类似 TPC-C / TPC-H 的 Agent trace benchmark。

缓解方式：

- 使用 synthetic Agent trace generator 做主实验
- 使用 DeathStarBench / OpenTelemetry Demo 做补充实验
- 如果可能，接入 LangChain / LangGraph 应用产生真实 traces

---

### 风险 2：容易变成工程组合

如果只是：

```text
LSM + object storage + materialized view
```

创新性可能不够。

需要把论文贡献聚焦在：

```text
trace-aware compaction
trace lifecycle-aware layout
semantic materialization
completed trace segment
```

---

### 风险 3：和现有系统边界要讲清楚

需要明确对比：

| 系统 | 擅长 | 不足 |
|---|---|---|
| 日志系统 | append/search | 不理解 span tree 和 trace lifecycle |
| Jaeger/Tempo | tracing 查询 | 对 LLM/Agent 大 payload、streaming、cost/token 支持有限 |
| ClickHouse | OLAP 聚合 | 单 trace 实时加载和频繁状态更新不是强项 |
| Elasticsearch | 全文搜索 | trace tree 和高频状态更新成本高 |
| RocksDB/LSM | 高吞吐 KV 写入 | 不理解 trace 语义和 compaction 机会 |

---

## 13. 可能的论文贡献写法

可以包装成三个贡献：

1. **Workload Characterization**
   - 分析 Agent trace workload 的特点：append/update 混合、span tree、long-running trace、streaming event、大 payload、hot/cold lifecycle。

2. **Trace-aware LSM Design**
   - 提出 trace-aware key layout、hot/cold separation、completed trace segment。

3. **Semantic Compaction**
   - 在 trace 完成后将 event log 物化为 span state 和 trace segment，降低 `get_trace_tree` 读放大和延迟。

---

## 14. 一句话总结

这个方向能发论文的关键不是“用 LSM-tree 存 Agent trace”，而是：

> 证明 Agent trace 是一种新的、对 LSM-tree 不友好的混合 workload，并提出 trace-aware layout 与 semantic compaction，使 LSM 在保持高写入吞吐的同时，显著降低 completed trace 的单 trace 加载延迟和读放大。
