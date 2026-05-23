# TraceLSM：Object-Backed Agent Trace Storage 设计方案

> MVP 落地状态（2026-05-23）：本方案已包含两条路径：离线验证工具
> `tools/tracelsm_object_store.py` 支持 `local` 与腾讯 `cos` 后端，并输出稳定
> 的 `object_manifest.jsonl`；C++ 热路径新增 `tracelsm_object` layout，在
> RocksDB 之上执行大 inline payload 外置和 compact value 写入。
> `SemanticCompactor`、`TraceSegmentBuilder`、`HybridCache` 以及查询路径的
> 自动 hydration 暂未实现，留作下一阶段。具体使用方式见
> `docs/tracelsm_cos_mvp_usage.md` 和 `docs/tracelsm_cpp_mvp_usage.md`。

## 1. 一句话概述

TraceLSM 是一个面向 Agent observability workload 的 trace-aware 存储引擎：

```text
本地 LSM 承接高频 trace/span/event 写入和热查询；
对象存储承接大 payload、冷数据和 compacted trace segment；
semantic compaction 根据 trace 生命周期把写优化的 delta layout 转换为读优化的 trace segment layout。
```

它的核心不是简单的 “LSM + object storage”，而是：

```text
利用 Agent trace 的 running -> completed 生命周期，做 trace/span 语义级 compaction。
```

## 2. 背景与问题

Agent observability 数据和传统 tracing/log/OLAP 数据不同：

| 特征 | 传统 RPC trace | Agent trace |
|---|---|---|
| 生命周期 | 通常短 | 可能 long-running |
| 写入模式 | 多数一次性写完 | 高频 append/update |
| payload | 小 metadata 为主 | prompt/completion/tool output/retrieved docs 很大 |
| 查询 | trace lookup / service dependency | trace tree、span detail、filter、search、thread reconstruction |
| 语义 | span tree | agent/chain/llm/tool/retriever/code 等语义 |
| 完成后状态 | 普通历史 trace | 可 materialize 为稳定 execution record |

当前通用系统的缺点：

- RocksDB/普通 LSM：底层性能强，但不知道 trace 生命周期和 semantic compaction。
- ClickHouse：适合分析过滤，但对 low-latency trace tree、freshness、逐条 append/update 不够自然。
- Elasticsearch/OpenSearch：全文搜索强，但高频结构化写入、trace tree locality 和存储成本不理想。
- Jaeger/Tempo：适合传统 tracing，但不专门处理 large payload、multi-turn agent thread、long-running trace。

## 3. 设计目标

TraceLSM 目标：

1. **低延迟写入**：支持 Agent 单次对话几十到几百条 span/event/update 写入。
2. **写后快速可见**：running trace 的新 span/event/update 能快速被 `get_trace_tree` / `get_span` 看到。
3. **trace tree 查询友好**：`get_trace_tree(trace_id)` 是一级核心路径。
4. **span/run lookup 友好**：支持 `get_span(span_id)` / `get_span(trace_id, span_id)`。
5. **filter/search/thread 查询可用**：支持 kind/model/tool/status/latency filter、text search、thread reconstruction。
6. **大 payload 低成本存储**：prompt/completion/tool output/retrieved docs 外置到 object storage。
7. **冷热分层**：hot trace 在本地，cold completed trace 在对象存储。
8. **semantic compaction**：completed trace 从 delta rows 转换为 compacted trace segment。

非目标：

- 不把 ClickHouse/Elastic 的全量分析能力全部重做。
- 不追求复杂 SQL。
- 第一阶段不做分布式强一致事务。

## 4. 数据模型

### 4.1 Trace

```json
{
  "trace_id": "trc_x",
  "thread_id": "thr_y",
  "session_id": "sess_z",
  "user_id": "user_a",
  "root_span_id": "spn_root",
  "start_ns": 1790000000000,
  "end_ns": null,
  "status": "running|success|error",
  "metadata": {
    "task_type": "research|coding|support|data_analysis",
    "long_running": true
  }
}
```

### 4.2 Span

```json
{
  "span_id": "spn_x",
  "trace_id": "trc_x",
  "parent_span_id": "spn_parent",
  "kind": "agent|chain|llm|tool|retriever|code|http",
  "name": "generate_answer",
  "start_ns": 1790000000000,
  "end_ns": null,
  "status": "running|success|error",
  "latency_ms": null,
  "attributes": {
    "model": "gpt-4.1",
    "tool_name": "web_search",
    "tenant_id": "tenant_1"
  },
  "input_ref": "obj://...",
  "output_ref": "obj://..."
}
```

### 4.3 Event

```json
{
  "event_id": "evt_x",
  "trace_id": "trc_x",
  "span_id": "spn_x",
  "timestamp_ns": 1790000000000,
  "event_type": "llm_stream_chunk|tool_call_started|progress_update|trace_heartbeat",
  "payload": {
    "payload_ref": "obj://...",
    "bytes": 65536
  }
}
```

### 4.4 Payload Object

```json
{
  "payload_ref": "obj://bucket/tenant/day/trace_id/span_id/payload_id",
  "trace_id": "trc_x",
  "span_id": "spn_x",
  "kind": "prompt|completion|tool_output|retrieved_doc|error_stack",
  "bytes": 65536,
  "digest": "sha256:...",
  "storage_state": "local|uploading|remote|local+remote"
}
```

## 5. Key Layout

### 5.1 Primary trace-first layout

```text
T#{trace_id}                                      -> trace summary/current state
S#{trace_id}#{span_id}                            -> span current state or span delta
E#{trace_id}#{timestamp_ns}#{event_id}             -> event row
C#{trace_id}#{parent_span_id}#{span_id}            -> child ref
```

### 5.2 Secondary indexes

```text
B#{span_id}                                       -> trace_id
K#{kind}#{timestamp_ns}#{trace_id}#{span_id}       -> kind index
M#{model}#{timestamp_ns}#{trace_id}#{span_id}      -> model index
U#{tool_name}#{timestamp_ns}#{trace_id}#{span_id}  -> tool index
L#{kind}#{latency_bucket}#{timestamp_ns}#...       -> latency index
R#{start_ns}#{trace_id}                            -> running trace index
D#{thread_id}#{conversation_turn}#{trace_id}       -> thread index
S2#{session_id}#{start_ns}#{trace_id}              -> session index
W#{token}#{timestamp_ns}#{trace_id}#{span_id}#{event_id} -> simple text index
```

### 5.3 Compacted trace segment index

```text
G#{trace_id}                                      -> trace segment manifest/ref
GS#{trace_id}#{span_id}                           -> optional span offset inside segment
```

## 6. 写入路径

### 6.1 普通 metadata 写入

```text
insert_trace / insert_span / append_event / update_span
  -> WAL
  -> MemTable
  -> L0/L1 trace-first SST
  -> update secondary indexes
```

### 6.2 大 payload 写入

一条写入会分裂成两部分：

```text
small metadata -> local LSM
large payload  -> local payload cache -> async batch upload object storage
```

写入流程：

```text
1. 接收 span/event。
2. 如果 payload 小，inline 进入 row。
3. 如果 payload 大：
   a. 写 local payload cache。
   b. 生成 payload_ref / digest / bytes。
   c. metadata row 写入 LSM。
   d. 后台 batch upload 到 object storage。
   e. upload 完成后更新 payload storage_state。
```

### 6.3 Ack 策略

需要支持两种策略：

| 策略 | 含义 | 优点 | 缺点 |
|---|---|---|---|
| fast ack | WAL + local payload cache 成功即返回 | 低延迟 | 依赖本地盘可靠性 |
| durable ack | payload object storage 上传完成后返回 | 持久性强 | 延迟受 object storage 影响 |

第一阶段建议实现：

```text
metadata: local WAL ack
large payload: local durable cache ack + async remote upload
```

## 7. LSM Level 设计

```text
MemTable / WAL
  recent writes, running trace updates

L0
  write-optimized delta files
  recent span/event/update

L1
  trace_id sorted delta SST
  supports fresh get_trace_tree

L2
  merged span/event state
  fewer versions

L3 / Cold
  completed trace semantic segments
  remote object storage backed
```

关键点：

- LSM level 表示冷热/新旧。
- key schema 保证 trace locality。
- trace lifecycle 决定是否触发 semantic compaction。

## 8. Semantic Compaction

### 8.1 普通 LSM compaction

普通 compaction 只做：

```text
key order merge
版本清理
tombstone 清理
重复 key 合并
```

### 8.2 Trace semantic compaction

TraceLSM 的核心创新：当 trace completed 后，基于 trace/span/event 语义做重组。

输入：

```text
T#{trace_id}
S#{trace_id}#*
E#{trace_id}#*
C#{trace_id}#*
secondary index refs
payload refs
```

输出：

```text
TraceSegmentObject(trace_id)
SegmentManifest(trace_id)
updated secondary indexes
payload ref table
```

### 8.3 Trace Segment Object 格式

```text
TraceSegment
  header
  trace summary
  span table final states
  parent-child adjacency index
  event timeline
  payload ref table
  local span_id -> offset index
  optional token/error mini-index
  footer + checksum
```

目标：

- `get_trace_tree(trace_id)` 一次或少数几次 object/block read。
- `get_span(span_id)` 可通过 offset 直接定位。
- completed trace 的多版本 delta 被清理。
- 大 payload 只保留 refs，不重复存储。

### 8.4 Late event 处理

late event 可能在 completed 后到达：

策略：

1. 写入 small delta overlay。
2. 查询时读取 segment + overlay。
3. 后续 background re-segment 合并。

```text
SEG#{trace_id}          -> base completed segment
OV#{trace_id}#timestamp -> late event/update overlay
```

## 9. Hybrid Cache

### 9.1 LSM/segment cache

缓存：

- SST blocks
- trace segment objects
- index blocks
- bloom/filter blocks
- manifest

策略：

```text
trace-aware admission/eviction
running trace priority
recent completed trace priority
thread/session prefetch
```

### 9.2 Payload cache

缓存：

- 尚未上传的 payload
- 最近访问的 prompt/completion/tool output
- object storage 下载后的 payload

Eviction 约束：

```text
不能驱逐 local-only payload
可驱逐 remote-confirmed payload
large payload 根据 size-aware policy
```

## 10. Object Storage Layout

```text
obj://bucket/tenant_id/date/payloads/trace_id/span_id/payload_id
obj://bucket/tenant_id/date/segments/trace_id.segment
obj://bucket/tenant_id/date/indexes/index_name/part_id.sst
obj://bucket/tenant_id/manifests/epoch.manifest
```

对象类型：

| Object | 内容 |
|---|---|
| payload object | prompt/completion/tool output/retrieved docs |
| trace segment object | completed trace compacted segment |
| remote SST/index object | cold index / metadata |
| manifest object | trace segment/index version mapping |

## 11. 查询路径

### 11.1 `get_trace_tree(trace_id)`

```text
1. 查 local segment cache。
2. 如果 completed segment 存在：
   a. 读 segment。
   b. 合并 late overlay。
3. 否则扫描 local LSM: T/S/E/C by trace_id。
4. 根据需要 hydrate payload refs。
```

### 11.2 `get_span(span_id)`

```text
1. B#{span_id} -> trace_id。
2. 如果 trace segment exists，查 segment offset。
3. 否则查 S#{trace_id}#{span_id}。
4. optional hydrate payload。
```

### 11.3 `filter_spans(...)`

```text
1. 扫 secondary index K/M/U/L/A。
2. 获取 trace_id/span_id。
3. 对 hot/running trace 查 LSM。
4. 对 completed trace 查 segment offset。
5. optional hydrate payload。
```

### 11.4 `search_text(keyword)`

第一阶段：

```text
hot/recent: local W# token index
cold/completed: segment mini-index or external search baseline
```

后续可接：

```text
Elasticsearch/OpenSearch for full text cold search
```

### 11.5 `get_thread(thread_id)`

```text
1. D#{thread_id}#{turn}#{trace_id} -> trace list。
2. 读取 trace summaries。
3. optional prefetch trace segments。
```

## 12. Benchmark 计划

### 12.1 Workload

需要覆盖：

```text
insert_trace
insert_span
append_event
update_span
update_trace
get_trace_tree
get_span
filter_spans
search_text
query_running_traces
get_thread
freshness_probe
hydrate_payload
```

### 12.2 Profiles

```text
standard_10g
large_100g
payload_heavy_100g
long_running
write_heavy
thread_heavy
search_heavy
```

### 12.3 Baselines

```text
RocksDB trace_first
RocksDB trace_first_with_indexes
ClickHouse
Elasticsearch/OpenSearch
Tempo/Jaeger
PostgreSQL JSONB optional
```

### 12.4 Metrics

```text
write throughput
query P50/P95/P99/P999
freshness latency
payload visible latency
object upload lag
read amplification
write amplification
storage amplification
cache hit ratio
object GET/PUT count
object bytes read/write
semantic compaction time
row reduction ratio
segment size
```

## 13. 真实数据与 AOBench 策略

当前领域缺少公开 Agent trace benchmark。策略：

1. 收集小规模真实/准真实 agent traces。
2. 提取聚合统计，不暴露原始 prompt/completion。
3. 用统计校准 synthetic generator。
4. 发布可复现 AOBench。
5. 做 sensitivity analysis 防止参数依赖。

需要从真实环境提取的统计：

```text
spans_per_trace CDF
events_per_span CDF
payload_bytes_by_kind CDF
trace_duration CDF
append/update ratio
long_running_ratio
thread_length CDF
query mix
freshness requirement
late_event_ratio
```

## 14. 实现路线

### Phase 1：当前 benchmark 强化

已基本完成：

- realistic generator
- RocksDB runner
- ClickHouse runner
- 10GB/100GB 级实验准备

### Phase 2：TraceLSM prototype MVP

实现：

```text
WAL + memtable
trace-first SST layout
payload local cache
object storage uploader
segment manifest
basic get_trace_tree/get_span/filter_spans
```

### Phase 3：Semantic compaction

实现：

```text
completed trace detection
trace delta scan
TraceSegmentObject builder
segment reader
late overlay
index update
```

### Phase 4：Hybrid cache

实现：

```text
segment cache
payload cache
trace-aware eviction
upload-state-aware eviction
```

### Phase 5：Evaluation

运行：

```text
10GB / 100GB / 1TB
hot/warm/cold cache
payload inline/ref/mixed
object store local/COS/S3 latency
baseline comparison
```

## 15. 论文角度

最有潜力的论文主线：

```text
TraceLSM: Semantic Compaction for Agent Observability Workloads
```

论文贡献可以是：

1. Agent observability workload characterization / AOBench。
2. Object-backed trace-aware LSM design。
3. Lifecycle-aware semantic compaction。
4. Hybrid cache for trace segments and payload objects。
5. Evaluation against RocksDB/ClickHouse/Elastic/Tempo。

需要避免的表述：

```text
我们只是把 LSM 和 object storage 组合起来。
```

应该强调：

```text
Agent trace lifecycle creates new semantic compaction opportunities.
```

## 16. Open Questions

1. completed trace 如何可靠判定？
2. late event 的 overlay 多久 re-segment？
3. payload fast ack 和 durable ack 如何配置？
4. object storage 上传失败如何恢复？
5. segment format 是否需要支持 partial read？
6. secondary index 和 segment manifest 如何保持一致？
7. search_text 是否内建倒排索引还是外接搜索系统？
8. cache eviction 如何同时考虑 trace hotness 和 payload size？
9. multi-tenant 隔离和 quota 如何做？
10. 真实 trace 统计能否合规使用？

## 17. 当前判断

这个方向有论文潜力，但核心创新必须聚焦在：

```text
trace lifecycle-aware semantic compaction
```

而不是泛泛的：

```text
LSM + object storage + cache
```

如果能结合真实/校准的 Agent trace benchmark，并在 100GB-1TB 规模证明：

- freshness 更好
- trace tree load 更快
- 大 payload 存储成本更低
- completed trace read amplification 更低
- object storage 冷数据成本更低

那么它有机会发展成 VLDB/SIGMOD 级别的系统工作。
