# TraceLSM 实现计划与 Baseline 对比总结

## 1. 实现目标

当前阶段不要做完整的 Agent trace database，而是先实现一个聚焦的原型：

```text
LSM-based Agent Trace Store
= 高吞吐事件写入
+ span/trace 状态物化
+ 单 trace 快速加载
+ span tree 还原
+ trace-aware compaction
+ hot/cold 分层
```

核心查询目标是：

```text
get_trace_tree(trace_id)
```

也就是给定一个 `trace_id`，快速加载：

- trace summary
- 所有 span
- span parent-child 树
- 相关 events
- token/cost/error 等 summary

---

## 2. 论文与系统主线

主线不是和所有数据库硬拼，而是：

> Agent trace 是一种新的混合 workload。传统 LSM-tree 写入强，但不理解 trace 的生命周期和树状结构。通过 trace-aware layout、semantic compaction 和 hot/cold separation，可以在保持高写入吞吐的同时显著降低单 trace 加载延迟和读放大。

因此实验主线是：

```text
LSM 内部不同设计 tradeoff
```

外围再加入 `ClickHouse`、`Elasticsearch/OpenSearch`、`Jaeger/Tempo` 等系统，说明其他架构只能解决一部分问题，不能自然覆盖完整 Agent trace workload。

---

## 3. 核心 Baseline

### 3.1 Plain LSM

最基础版本：

```text
trace/span/event 直接写入 LSM
无特殊 key layout
无 semantic compaction
查询时从 LSM 读取并组装
```

用途：

> 证明普通 LSM/KV 不理解 trace 语义，会产生读放大和查询开销。

---

### 3.2 Time-first LSM

Key layout：

```text
E#{timestamp_ns}#{trace_id}#{event_id} -> event
```

优点：

- 时间范围扫描友好
- 类似日志系统写法

缺点：

- 同一个 trace 的 event 分散
- `get_trace_tree(trace_id)` 需要额外索引或大量扫描

用途：

> 代表日志式/time-series layout。

---

### 3.3 Trace-first LSM

Key layout：

```text
E#{trace_id}#{timestamp_ns}#{event_id} -> event
S#{trace_id}#{span_id} -> span
```

优点：

- 单 trace 加载较快
- range scan 友好

缺点：

- 时间范围查询较弱
- 如果没有 compaction/materialization，仍然需要从 events 重建状态

用途：

> 证明只做 trace_id 聚集还不够，还需要 trace-aware compaction。

---

### 3.4 Append-only LSM

写入模式：

```text
只 append event
不直接 update span/trace state
查询时 replay events
```

优点：

- 写入吞吐高
- 写路径简单

缺点：

- 查询时需要重放事件
- `get_trace_tree` 延迟高
- running/completed trace 状态查询成本高

用途：

> 代表写入最优但查询较差的设计。

---

### 3.5 Update-heavy LSM

写入模式：

```text
insert span status=running
update span status=success/error, end_time=...
update trace total_tokens/total_cost/status
```

优点：

- 查询当前状态简单
- 物化视图天然存在

缺点：

- LSM update 本质是写新版本
- 旧版本等待 compaction 清理
- 写放大、读放大、compaction 压力上升

用途：

> 代表 OLTP 式状态更新设计。

---

## 4. Proposed：TraceLSM

TraceLSM 的核心思想：

```text
running trace 阶段：写入 hot LSM
completed trace 阶段：freeze + semantic compaction
查询阶段：优先读 compacted trace segment
```

### 4.1 Hot Layer

用于正在运行的 trace：

```text
append event
insert span
维护轻量 span state
支持运行中 trace 查询
```

### 4.2 Freeze

当收到 `trace_finished` 后：

```text
freeze trace
收集该 trace 的 spans/events
进行 semantic compaction
生成 completed trace segment
```

### 4.3 Semantic Compaction

不同于普通 LSM compaction，它需要理解 trace 语义：

```text
span_started + span_finished -> span final state
llm_stream_chunk* -> output_summary / output_ref
child spans -> parent aggregate
all spans/events -> trace summary
```

### 4.4 Cold Segment

完成后的 trace 被组织成查询友好的 segment：

```text
Trace Segment
├─ trace summary
├─ span table
├─ parent-child index
├─ event blocks
├─ token/cost/error summary
└─ payload refs
```

目标：

- 降低 `get_trace_tree(trace_id)` 读放大
- 减少跨 SSTable/level 读取
- 让 completed trace 基本只读、可压缩、可归档

---

## 5. 外围系统对比

外围系统不是论文主线，而是回答：

> 为什么不用现成系统？

### 5.1 ClickHouse

代表 OLAP/列存系统。

强项：

- 时间范围扫描
- token/cost/latency 聚合
- group by 分析

弱项：

- running trace 高频小更新
- 单 trace tree 低延迟加载
- trace lifecycle materialization
- 大 payload 与 metadata 混合存储

作用：

> 说明 OLAP 很适合分析，但不是为 Agent trace reconstruction 和 lifecycle 优化的。

---

### 5.2 Elasticsearch / OpenSearch

代表日志搜索系统。

强项：

- 全文搜索
- JSON 字段过滤
- 日志式查询

弱项：

- span tree reconstruction
- 高频状态更新
- 存储放大
- 结构化 trace segment 管理

作用：

> 说明搜索系统能解决查文本，但不能自然解决 trace tree 和生命周期问题。

---

### 5.3 Jaeger / Tempo

代表传统 tracing 系统。

强项：

- distributed tracing
- service/RPC span 查询
- trace UI 生态

弱项：

- LLM prompt/completion 大 payload
- streaming token/chunk
- token/cost 聚合
- tool/retriever/code span 语义
- long-running Agent trace

作用：

> 说明传统 tracing 系统接近这个问题，但 Agent trace 有额外语义和存储压力。

---

## 6. 第一阶段实现范围

建议先实现最小闭环。

### 6.1 数据输入

使用已有 synthetic workload：

```text
agent-trace-storage-notes/synthetic-agent-trace-generator/sample-small/
```

重点文件：

```text
operations.jsonl   # replay insert/update/query workload
events.jsonl       # append-only event log
spans.jsonl        # materialized span ground truth
traces.jsonl       # materialized trace ground truth
```

---

### 6.2 先实现的 API

```text
put_trace(trace)
put_span(span)
append_event(event)
update_span(span_id, patch)
update_trace(trace_id, patch)
get_trace(trace_id)
list_spans(trace_id)
list_events(trace_id)
get_trace_tree(trace_id)
```

---

### 6.3 第一阶段 Keyspace

```text
traces:
  T#{trace_id} -> trace_summary

spans_by_trace:
  S#{trace_id}#{span_id} -> span_record

events_by_trace:
  E#{trace_id}#{timestamp_ns}#{event_id} -> event_record

children_by_parent:
  C#{trace_id}#{parent_span_id}#{span_id} -> span_ref

running_traces:
  R#{start_time}#{trace_id} -> empty

error_spans:
  X#{timestamp_ns}#{trace_id}#{span_id} -> span_ref
```

---

## 7. 实验指标

核心指标：

```text
write throughput
write amplification
read amplification
compaction overhead
get_trace_tree P50/P95/P99
running trace query latency
completed trace query latency
span tree reconstruction latency
storage amplification
```

第一阶段最重要的是：

```text
get_trace_tree(trace_id) latency
```

因为这是 Agent observability UI 的核心查询。

---

## 8. 实现顺序建议

### Step 1：实现 Plain LSM / KV 接口

先用一个简单 KV backend 抽象：

```text
put(key, value)
get(key)
scan(prefix)
delete(key)
```

后续可以替换成 RocksDB、自己写的 LSM、或者其他 KV。

---

### Step 2：实现 Trace-first Layout

先支持：

```text
T#{trace_id}
S#{trace_id}#{span_id}
E#{trace_id}#{timestamp}#{event_id}
```

跑通：

```text
get_trace_tree(trace_id)
```

---

### Step 3：实现 workload replay

读取 `operations.jsonl`，执行：

```text
insert_trace
insert_span
append_event
update_span
update_trace
query_trace_tree
```

记录：

```text
写入吞吐
查询延迟
扫描 key 数量
读取 bytes
```

---

### Step 4：实现 Append-only Baseline

只写 `events.jsonl`，查询时 replay events 生成 span/trace 状态。

用于比较：

```text
写入吞吐 vs 查询延迟
```

---

### Step 5：实现 Update-heavy Baseline

按 `operations.jsonl` 中的 update 操作直接更新 span/trace state。

用于观察：

```text
update 对 LSM 版本堆积和 compaction 的影响
```

---

### Step 6：实现 TraceLSM Semantic Compaction

当 trace completed：

```text
读取该 trace 所有 spans/events
生成 compacted trace segment
写入 G#{trace_id} -> segment_ref
```

查询 completed trace 时优先读 segment。

---

### Step 7：加入外围系统对比

在核心原型稳定后，再导入到：

```text
ClickHouse
Elasticsearch/OpenSearch
Jaeger/Tempo，可选
```

外围对比不要喧宾夺主，只证明：

```text
这些系统各自擅长一部分，但不是为这个综合 workload 优化的。
```

---

## 9. 当前实现优先级

最推荐当前优先级：

```text
P0: workload replay + Trace-first LSM + get_trace_tree
P1: Append-only baseline + Update-heavy baseline
P2: Trace-aware compaction + completed trace segment
P3: Time-first layout baseline
P4: ClickHouse / Elasticsearch 外围对比
P5: Jaeger / Tempo 对比
```

---

## 10. 一句话总结

实现上先不要追求大而全，而是围绕一个核心闭环：

> 在 LSM 上写入 Agent trace/span/event，并证明 trace-aware compaction 能显著优化 `get_trace_tree(trace_id)`，同时保持接近 append-only 的写入吞吐。
