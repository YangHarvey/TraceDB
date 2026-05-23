# Agent Observability Benchmark 开发计划

## 1. 目标

当前 benchmark 已经能 replay synthetic `operations.jsonl`，并用 RocksDB `trace_first` layout 测 `get_trace_tree(trace_id)`。但它还偏 toy，主要问题是：

- 数据规模小，缺少大规模和长尾分布。
- trace 生命周期较简单，长运行、open span、late event 不够真实。
- 大 payload 只用少量随机文本，缺少 prompt / completion / tool output / retrieved docs 等真实形态。
- 查询 workload 单一，主要测 `get_trace_tree(trace_id)`。
- 缺少 high-frequency ingestion、freshness、filter、full-text、thread reconstruction 等 Agent observability 核心路径。
- benchmark runner 只有全局查询延迟，没有按操作类型拆分指标。

后续目标是把它升级为一个更接近 SmithDB / LangSmith 场景的 **Agent Observability Benchmark**：

```text
high-frequency writes
+ long-running trace lifecycle
+ large payload / object refs
+ trace tree load
+ single span lookup
+ span filtering
+ full-text search
+ ingestion freshness
+ hot running trace query
+ thread reconstruction
+ mixed UI workload
```

## 2. 当前实现基线

### 2.1 Generator

当前文件：

```text
agent-trace-storage-notes/synthetic-agent-trace-generator/generate_agent_traces.py
```

已有能力：

- 生成 `traces.jsonl`
- 生成 `spans.jsonl`
- 生成 `events.jsonl`
- 生成 `operations.jsonl`
- 支持 `agent / chain / llm / tool / retriever / code / http` span kind
- 支持 `long_running_ratio`
- 支持 `error_ratio`
- 支持 `retry_ratio`
- 支持 `large_payload_ratio`
- 支持 `streaming_event_ratio`
- 支持 `query_trace_tree` 和 `query_failed_spans`

主要缺口：

- 没有 `thread_id` / conversation turn。
- 没有更真实的 prompt / completion / tool output / retrieved docs。
- 大 payload 分布太简单。
- long-running trace 只是 `end_ns = null`，没有持续生命周期查询和周期性更新。
- operations 中查询类型太少。
- 缺少 high-frequency burst ingestion profile。
- 缺少 full-text search 的关键词和 ground truth。

### 2.2 RocksDB Benchmark Runner

当前文件：

```text
tools/rocksdb_trace_bench.cc
```

已有能力：

- replay `insert_trace`
- replay `insert_span`
- replay `append_event`
- replay `update_span`
- replay `update_trace`
- replay `query_trace_tree`
- replay `query_failed_spans`
- 输出全局写入吞吐、查询延迟、扫描 keys/bytes、RocksDB size

当前 key layout：

```text
T#{trace_id} -> trace/update record
S#{trace_id}#{span_id} -> span/update record
E#{trace_id}#{timestamp_ns}#{event_id} -> event record
C#{trace_id}#{parent_span_id}#{span_id} -> child ref
R#{start_time}#{trace_id} -> running trace ref
X#{timestamp_ns}#{trace_id}#{span_id} -> error span ref
```

主要缺口：

- 没有 `get_span`。
- 没有 `filter_spans`。
- 没有 full-text search。
- 没有 thread/session reconstruction。
- 没有 ingestion freshness。
- 没有按 query type 分开的 P50/P95/P99。
- 没有 matched rows / returned bytes / index write amplification。
- 只有 `trace_first` layout。

## 3. 数据真实性增强计划

### 3.1 数据规模

需要提供多档 profile，而不是只用 200 / 1000 traces。

建议内置 profile：

| Profile | 目标 | 建议规模 |
|---|---|---:|
| `smoke` | 开发冒烟 | 200 traces |
| `dev` | 本地快速调试 | 1k traces |
| `standard` | 主实验默认 | 10k traces |
| `large` | 较大规模实验 | 100k traces |
| `write_heavy` | 高频写入压力 | 50k-200k traces |
| `long_running` | 热 trace 生命周期 | 10k traces，25% long-running |
| `payload_heavy` | 大 payload 压力 | 10k-50k traces |
| `deep_tree` | 深层嵌套 trace | 10k traces，深度 8+ |

### 3.2 分布从均匀改为长尾

真实 Agent trace 不应只有平均值，应加入长尾：

- spans per trace：Poisson + Pareto / lognormal tail
- events per span：多数少量，少数 LLM span 有大量 streaming chunks
- latency：按 kind 区分，并加入 tail latency
- payload bytes：lognormal / Pareto
- thread length：多数 1-3 turn，少数几十 turn

新增参数建议：

```text
--span-count-dist poisson|lognormal|pareto
--event-count-dist poisson|lognormal|pareto
--payload-size-dist fixed|lognormal|pareto
--deep-trace-ratio
--burst-ratio
--tail-latency-ratio
```

### 3.3 长生命周期 Trace

增强 long-running 生成逻辑：

- trace 持续几分钟到数小时。
- 部分 span 长时间 open。
- 运行中不断 append event。
- 周期性 update span progress。
- 查询可能发生在 trace 未完成时。
- trace 结束后再 late-arriving event。

新增参数建议：

```text
--long-running-ratio
--long-running-min-seconds
--long-running-max-seconds
--open-span-ratio
--progress-event-ratio
--late-event-ratio
--hot-trace-query-ratio
```

新增 event 类型：

```text
progress_update
checkpoint_saved
agent_state_snapshot
human_feedback_received
trace_heartbeat
late_event
```

### 3.4 大 Payload

当前大 payload 多是随机文本，应更贴近 Agent 数据：

- user input
- system prompt
- prompt template
- LLM completion
- tool args
- tool output
- retrieved document chunks
- code output
- error stack
- multimodal refs

建议生成两种模式：

1. inline payload：直接写入 event/span value，用于测试 LSM value 膨胀。
2. object ref：LSM 只存 `obj://...`，payload 写入单独 `payloads.jsonl` 或目录文件，用于测试 payload 外置。

新增输出：

```text
payloads.jsonl
```

建议字段：

```json
{
  "payload_ref": "obj://payloads/...",
  "trace_id": "...",
  "span_id": "...",
  "kind": "prompt|completion|tool_output|retrieved_doc|error_stack",
  "bytes": 123456,
  "text": "..."
}
```

新增参数：

```text
--payload-mode inline|refs|mixed
--payload-min-bytes
--payload-max-bytes
--large-payload-ratio
--multimodal-ref-ratio
```

### 3.5 Thread / Conversation

新增 thread 维度：

```text
thread_id
conversation_turn
trace_index_in_thread
previous_trace_id
```

trace metadata 示例：

```json
{
  "thread_id": "thr_xxx",
  "conversation_turn": 7,
  "task_type": "research|coding|support|data_analysis",
  "long_running": true
}
```

这支持：

```text
list_traces_by_session(session_id)
get_thread(thread_id)
reconstruct_conversation(thread_id)
```

## 4. Workload 扩展计划

### 4.1 高频写入 workload

Agent 一次对话可能产生几十到几百条写入。benchmark 需要显式模拟 burst：

```text
insert_trace
insert_span x N
append_event x M
update_span x N
append_event streaming chunks
update_trace
```

新增 profile：

```text
write_heavy_background_agents
```

特点：

- 高 `append_event` 占比。
- LLM streaming chunks 多。
- query 比例低但持续存在。
- 多 tenant / 多 session 并发交错。
- 操作按 timestamp 排序，模拟真实 ingest interleaving。

建议 mixed ratio：

```text
70% append_event
12% insert_span
8% update_span
3% insert/update_trace
3% get_trace_tree
2% get_span
1% filter_spans
1% query_running_traces
```

### 4.2 新增 operation 类型

在 `operations.jsonl` 中新增：

```text
get_span
filter_spans
search_text
query_running_traces
list_thread_traces
get_thread
query_hot_trace_tree
freshness_probe
```

示例：

```json
{"op":"get_span","timestamp_ns":1790000000123,"trace_id":"trc_x","span_id":"spn_y"}
```

```json
{"op":"filter_spans","timestamp_ns":1790000000123,"filter":{"kind":"llm","model":"gpt-4.1","min_latency_ms":5000},"limit":100}
```

```json
{"op":"search_text","timestamp_ns":1790000000123,"query":"timeout","target":"events","limit":100}
```

```json
{"op":"get_thread","timestamp_ns":1790000000123,"thread_id":"thr_x","limit":100}
```

```json
{"op":"freshness_probe","timestamp_ns":1790000000123,"after_op_id":"op_x","query":"get_span","trace_id":"trc_x","span_id":"spn_y"}
```

### 4.3 查询类型

#### Trace tree load

```text
get_trace_tree(trace_id)
```

继续保留，并拆分：

- with events
- without events
- compacted completed trace
- running trace

#### Single span lookup

```text
get_span(trace_id, span_id)
get_span_by_id(span_id)
get_span_events(trace_id, span_id)
```

需要新增 key：

```text
B#{span_id} -> trace_id
```

或者直接生成 query 使用 `trace_id + span_id`。

#### Span filtering

第一阶段先支持固定索引：

```text
X#{timestamp_ns}#{trace_id}#{span_id}                 # error spans
K#{kind}#{timestamp_ns}#{trace_id}#{span_id}          # kind index
M#{model}#{timestamp_ns}#{trace_id}#{span_id}         # model index
U#{tool_name}#{timestamp_ns}#{trace_id}#{span_id}     # tool index
L#{kind}#{latency_bucket}#{timestamp_ns}#{trace_id}#{span_id} # latency bucket
A#{attr_key}#{attr_value}#{timestamp_ns}#{trace_id}#{span_id} # selected attributes
```

#### Full-text search

第一阶段做简单倒排索引：

```text
W#{token}#{timestamp_ns}#{trace_id}#{span_id}#{event_id} -> ref
```

只索引有限字段：

- error_type / error message
- tool_name
- span name
- selected event payload text
- synthetic prompt/completion keywords

后续再对比 Elasticsearch / OpenSearch。

#### Ingestion freshness

在同步 RocksDB baseline 中，写入后立即可见，所以 freshness 近似为 write latency + query latency。

但为了给未来异步 materialization / compaction 留口径，benchmark 中仍应记录：

```text
ingest_to_visible_us = first_successful_query_time - write_end_time
```

在当前同步 baseline 中实现为：

```text
write op 完成后立即执行 probe query，并记录 probe latency。
```

#### Hot running trace

```text
query_running_traces(limit)
get_trace_tree(running_trace_id)
```

对应 key：

```text
R#{start_time}#{trace_id}
```

#### Thread reconstruction

新增 key：

```text
D#{thread_id}#{conversation_turn}#{trace_id} -> trace_ref
S2#{session_id}#{start_ns}#{trace_id} -> trace_ref
```

查询：

```text
get_thread(thread_id)
list_traces_by_session(session_id)
```

## 5. Benchmark Runner 指标增强

### 5.1 按操作类型拆分指标

当前只有全局 query latency。需要改成：

```text
metrics_by_op[op].count
metrics_by_op[op].lat_us[]
metrics_by_op[op].scanned_keys
metrics_by_op[op].scanned_bytes
metrics_by_op[op].matched_rows
metrics_by_op[op].returned_bytes
```

最终 JSON 输出示例：

```json
{
  "ops": {
    "get_trace_tree": {"count": 1000, "p50_us": 92000, "p95_us": 180000},
    "get_span": {"count": 5000, "p50_us": 71000},
    "filter_spans": {"count": 1000, "p50_us": 82000},
    "search_text": {"count": 500, "p50_us": 400000}
  }
}
```

### 5.2 写入指标

新增：

```text
trace_insert_ops_per_sec
span_insert_ops_per_sec
event_append_ops_per_sec
span_update_ops_per_sec
trace_update_ops_per_sec
index_writes
index_writes_per_span
index_writes_per_event
```

### 5.3 放大指标

新增：

```text
read_amplification = scanned_bytes / returned_bytes
index_write_amplification = total_index_puts / logical_writes
storage_amplification = rocksdb_total_sst_files_size / logical_input_bytes
payload_bytes_read
payload_refs_resolved
```

### 5.4 Freshness 指标

```text
ingest_to_visible_p50_us
ingest_to_visible_p95_us
ingest_to_visible_p99_us
```

## 6. Baseline 与对比路线

### 6.1 第一阶段内部 baseline

优先实现 RocksDB 内部 layout 对比：

| Layout | 说明 |
|---|---|
| `trace_first` | 当前已有，单 trace 查询友好 |
| `trace_first_with_indexes` | 加 kind/model/tool/thread/text 索引 |
| `append_only` | 只写事件，查询时 replay |
| `update_heavy` | 直接维护 span/trace state |
| `time_first` | 时间优先，代表日志式 layout |
| `tracelsm_segment` | completed trace 写入 compacted segment |

### 6.2 第二阶段外部系统

| 系统 | 对比点 |
|---|---|
| Elasticsearch / OpenSearch | full-text search |
| ClickHouse | filtering / aggregate |
| Jaeger / Tempo | traditional tracing |

外部系统不是 P0，等内部 workload 和指标稳定后再加。

## 7. 分阶段开发计划

### Phase 0：文档与目标冻结

目标：明确 benchmark spec，避免边做边漂移。

产出：

- 本文档。
- operation schema 草案。
- profile 参数表。
- 指标定义。

状态：当前文档已完成第一版。

### Phase 1：Generator 数据真实性增强

目标：让数据不再 toy。

任务：

1. 增加 `--profile`：`smoke/dev/standard/large/write_heavy/long_running/payload_heavy/deep_tree`。
2. 增加 `thread_id`、`conversation_turn`、`previous_trace_id`。
3. 增加 long-running 参数：open span、heartbeat、progress、late event。
4. 增加 payload 模式：inline / refs / mixed。
5. 输出 `payloads.jsonl`。
6. 生成更真实文本：prompt/completion/tool output/retrieved docs/error stack。
7. 让 span/event/payload 数量支持长尾分布。

建议优先级：

```text
P1.1 profile + larger scale
P1.2 thread_id / conversation_turn
P1.3 long-running lifecycle
P1.4 payloads.jsonl + payload mode
P1.5 long-tail distributions
```

验收：

- `smoke` 能在数秒生成。
- `standard` 至少 10k traces。
- `write_heavy` 至少 1M+ operations。
- `payload_heavy` 能生成大 payload refs。
- `summary.json` 记录每类 op、payload bytes、long-running traces、threads 数量。

### Phase 2：Operations 扩展

目标：生成更完整的 Agent observability workload。

任务：

1. 新增 `get_span`。
2. 新增 `filter_spans`。
3. 新增 `search_text`。
4. 新增 `query_running_traces`。
5. 新增 `get_thread` / `list_traces_by_session`。
6. 新增 `freshness_probe`。
7. 支持 `--workload-mix` 或 profile 内置 mix。

验收：

- `operations.jsonl` 中 query 不再只有 `query_trace_tree`。
- 每个 query 都有可执行参数。
- `summary.json` 输出 op count 分布。

### Phase 3：RocksDB Runner 支持新操作

目标：benchmark runner 能真正执行新 workload。

任务：

1. 实现 `get_span`。
2. 实现 `filter_spans` 的基础索引与扫描。
3. 实现 `query_running_traces`。
4. 实现 `get_thread` / `list_traces_by_session`。
5. 实现简单 `search_text` 倒排索引。
6. 实现 `freshness_probe` 指标。
7. 输出 per-op metrics。

建议先实现这些 keyspace：

```text
B#{span_id} -> trace_id
K#{kind}#{timestamp_ns}#{trace_id}#{span_id} -> span_ref
M#{model}#{timestamp_ns}#{trace_id}#{span_id} -> span_ref
U#{tool_name}#{timestamp_ns}#{trace_id}#{span_id} -> span_ref
D#{thread_id}#{conversation_turn}#{trace_id} -> trace_ref
S2#{session_id}#{start_ns}#{trace_id} -> trace_ref
W#{token}#{timestamp_ns}#{trace_id}#{span_id}#{event_id} -> ref
```

验收：

- 所有新 op 都能 replay。
- JSON summary 按 op 类型输出 P50/P95/P99。
- 输出 matched rows、scanned bytes、returned bytes。

### Phase 4：Benchmark Profiles 与自动运行脚本

目标：一键跑固定实验，避免手动命令不一致。

任务：

1. 新增 benchmark profile 文档。
2. 新增 Makefile target 或脚本：

```text
make bench-smoke
make bench-standard
make bench-write-heavy
make bench-long-running
make bench-payload-heavy
```

3. 每次结果输出到独立目录：

```text
bench-results/<date-or-profile>/summary.json
bench-results/<date-or-profile>/rocksdb_trace_first.json
```

验收：

- 本地可重复跑 smoke。
- 标准 profile 命令固定。
- 结果 JSON 可直接用于画图。

### Phase 5：Layout / Baseline 对比

目标：不只测一个 `trace_first`。

任务：

1. `trace_first` 当前 baseline 整理稳定。
2. `trace_first_with_indexes`：加入 filter/search/thread 索引。
3. `append_only`：只写 event，查询时 replay。
4. `update_heavy`：写 span/trace state。
5. `time_first`：时间优先 layout。
6. `tracelsm_segment`：completed trace segment。

验收：

- 同一 workload 可以切换 layout。
- 输出相同指标。
- 能比较：写入吞吐、查询延迟、读放大、写放大、存储放大。

### Phase 6：外部系统对比

目标：回答“为什么不用现成系统”。

任务：

1. Elasticsearch / OpenSearch：full-text search。
2. ClickHouse：filter / aggregate。
3. Jaeger / Tempo：trace tree / tracing baseline。

验收：

- 至少选 1-2 个外部系统做补充。
- 不作为主线，只用于说明 tradeoff。

## 8. 推荐短期执行顺序

最建议下一步先做最小但有价值的闭环：

```text
Step 1: generator 增加 profile、thread_id、payloads、long-running lifecycle
Step 2: operations 增加 get_span、filter_spans、query_running_traces、get_thread
Step 3: rocksdb_trace_bench 支持 get_span、filter_spans、get_thread
Step 4: 输出 per-op metrics
Step 5: 跑 smoke / standard / write_heavy 三组结果
```

第一轮暂时可以不做完整 full-text，只预留 `search_text` schema，并先实现简单 token index 或扫描 baseline。

## 9. 第一轮里程碑

### M1：真实数据生成器

完成后应能运行：

```bash
python3 generate_agent_traces.py \
  --profile standard \
  --out ./sample-standard \
  --otel-json
```

输出：

```text
traces.jsonl
spans.jsonl
events.jsonl
operations.jsonl
payloads.jsonl
summary.json
```

### M2：扩展操作 replay

`operations.jsonl` 至少包含：

```text
insert_trace
insert_span
append_event
update_span
update_trace
query_trace_tree
get_span
filter_spans
query_running_traces
get_thread
freshness_probe
```

### M3：RocksDB benchmark 输出 per-op metrics

输出 JSON 至少包含：

```text
ops.query_trace_tree.p50_us
ops.get_span.p50_us
ops.filter_spans.p50_us
ops.query_running_traces.p50_us
ops.get_thread.p50_us
ingest_to_visible_p50_us
scanned_keys_by_op
scanned_bytes_by_op
index_writes
rocksdb_total_sst_files_size
```

## 10. 风险与取舍

### 10.1 不要一开始就实现复杂搜索

全文搜索容易把主线带偏。第一阶段只需要：

- schema 有 `search_text`
- generator 能生成可搜索文本
- runner 有扫描 baseline 或简单倒排索引

外部搜索系统可以后置。

### 10.2 不要过早引入太多 layout

先把 workload 和指标做扎实，再扩展 layout。否则会变成多个不完整 baseline。

### 10.3 控制 payload-heavy 的本地磁盘压力

`payload_heavy` 应默认使用 object refs，并提供小规模 profile。大 payload inline 只用于专门实验。

## 11. 总结

后续 benchmark 强化的核心方向是：

```text
从单一 get_trace_tree toy benchmark，升级为覆盖真实 Agent observability 生命周期的 benchmark。
```

重点包括：

- 更真实、更大规模、长尾分布的数据。
- 高频写入和 burst ingestion。
- 长生命周期 running trace。
- 大 payload / object refs。
- `get_trace_tree`、`get_span`、`filter_spans`、`search_text`、freshness、hot trace、thread reconstruction。
- 按操作类型拆分的延迟、扫描、放大和存储指标。

第一阶段最重要的是完成：

```text
realistic generator + expanded operations + per-op RocksDB metrics
```

这样后续再做 TraceLSM / compaction / external systems 对比才有意义。
