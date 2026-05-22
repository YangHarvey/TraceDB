# Agent Trace 存储方向：可用 Workload 与数据集

## 结论

目前严格意义上的 **公开 Agent Trace 存储 benchmark / 标准 workload** 还不成熟。这个方向比较新，LangSmith、SmithDB 这类系统更多是工业内部真实负载。

但可以从三个层次开始试：

1. **真实分布式 tracing 数据集**：适合先验证 trace/span 树加载、索引、聚合。
2. **微服务 benchmark 自己产 trace**：适合生成可控 workload。
3. **自造 Agent trace workload**：最贴近 SmithDB/Agent observability 场景。

---

## 1. 公开 Trace 数据集

### 1.1 DeathStarBench Traces

地址：

- https://gitlab.mpi-sws.org/cld/trace-datasets/deathstarbench_traces

特点：

- 来源于 DeathStarBench 微服务 benchmark。
- 包含 raw Jaeger traces。
- 也包含转换后的 X-Trace 格式。
- 适合研究调用链、span 层级、服务依赖、请求路径、延迟分析。

适合验证：

- `trace_id` 查询
- `span_id` 查询
- `parent_span_id` 树还原
- 单 trace 加载延迟
- span duration 统计
- 服务调用路径聚合

不足：

- 这是微服务 trace，不是 LLM/Agent trace。
- 一般没有 prompt、completion、token、tool call、agent retry 等字段。

---

### 1.2 Pre-processed Tracing Data for Popular Microservice Benchmarks

地址：

- https://experts.illinois.edu/en/datasets/pre-processed-tracing-data-for-popular-microservice-benchmarks/
- DOI: `10.13012/B2IDB-6738796_V1`

特点：

- 包含 4 个微服务 benchmark 的预处理 tracing 数据：
  - DeathStarBench Social Network
  - DeathStarBench Media Service
  - DeathStarBench Hotel Reservation
  - Train Ticket Booking
- CSV 格式。
- 每行包含 tracing ID 和各微服务组件执行时长。
- 数据未采样。

适合验证：

- 时间序列聚合
- 异常检测
- latency 分析
- OLAP 查询
- workload replay

不足：

- 更偏性能诊断和异常检测。
- 不一定保留完整原始 span/event 结构。

---

## 2. 可生成 Trace 的 Benchmark / Workload

### 2.1 DeathStarBench

地址：

- https://github.com/delimitrou/DeathStarBench

特点：

- 经典微服务 benchmark。
- 包含 social network、media service、hotel reservation 等应用。
- 可以配合 Jaeger / OpenTelemetry 采集 trace。

适合用来生成：

- 高并发 trace 写入
- 多服务 span 树
- 长尾延迟 workload
- 不同请求类型的 trace 分布

和 Agent trace 的差异：

- 微服务 trace 更偏 RPC 调用链。
- Agent trace 更偏 LLM 调用、工具调用、推理路径、上下文变化。

但底层存储问题有相似性：

- 高频写入
- span 树结构
- trace 查询
- 时间范围过滤
- 延迟聚合

---

### 2.2 OpenTelemetry Demo

地址：

- https://github.com/open-telemetry/opentelemetry-demo

特点：

- OpenTelemetry 官方 demo 应用。
- 可以稳定地产生 traces、metrics、logs。
- 适合测试 OTLP ingest pipeline。

适合验证：

- OTLP trace ingest
- collector 接入
- batch 写入
- span attribute 存储
- trace 查询接口

---

## 3. Agent / LLM Trace 方向

### 3.1 LangSmith + OpenTelemetry 格式

LangSmith 支持 OpenTelemetry-based tracing，因此可以把 Agent trace workload 设计成 OTLP / OpenTelemetry span 格式。

适合关注这些字段：

```text
trace_id
span_id
parent_span_id
span_name
span_kind: chain / llm / tool / retriever / embedding
start_time
end_time
status
attributes
span_events
```

Agent 场景可以增加：

```text
model
prompt_tokens
completion_tokens
total_tokens
cost
tool_name
retriever_documents
input_ref
output_ref
error
retry_count
thread_id
session_id
```

这样做的好处是：

- 不必自定义全新格式。
- 可复用 OpenTelemetry Collector。
- 可与 LangSmith、Jaeger、Tempo 等生态兼容。

---

## 4. 推荐先试的 Workload 设计

如果你想验证一个存储系统原型，我建议先做一个 synthetic Agent trace generator。

### 4.1 基础数据模型

```text
Trace
  trace_id
  session_id
  user_id
  start_time
  end_time
  status
  total_tokens
  total_cost

Span
  span_id
  trace_id
  parent_span_id
  name
  kind: agent / chain / llm / tool / retriever / code / http
  start_time
  end_time
  status
  latency_ms
  attributes

Event
  event_id
  trace_id
  span_id
  timestamp
  event_type
  payload
```

---

### 4.2 负载参数

可以先暴露这些参数：

```text
num_traces                 trace 数量
spans_per_trace_avg         每个 trace 平均 span 数
span_depth_max              span 树最大深度
events_per_span_avg         每个 span 平均 event 数
long_running_ratio          长时间运行 trace 占比
error_ratio                 错误率
retry_ratio                 重试率
large_payload_ratio         大 input/output 占比
streaming_event_ratio       streaming event 占比
hot_trace_read_ratio        运行中 trace 查询占比
```

---

### 4.3 典型操作比例

一个比较合理的初始 workload：

```text
70% append event
15% insert span
5% update/materialize span status
5% query trace tree
3% search/filter traces
2% aggregate metrics
```

如果模拟 append-only 架构，可以改成：

```text
85% append event
5% materialize span/trace view
5% query hot trace
3% query historical trace
2% aggregate/search
```

---

### 4.4 查询类型

建议覆盖这些查询：

```sql
-- 1. 加载单个 trace 的完整 span 树
SELECT * FROM spans WHERE trace_id = ? ORDER BY start_time;

-- 2. 查询运行中的 trace
SELECT * FROM traces WHERE status = 'running' ORDER BY start_time DESC LIMIT 100;

-- 3. 查询失败 span
SELECT * FROM spans WHERE status = 'error' AND start_time >= ?;

-- 4. 查询慢 LLM span
SELECT * FROM spans WHERE kind = 'llm' AND latency_ms > 5000;

-- 5. 按模型统计 token 和成本
SELECT model, sum(total_tokens), sum(cost) FROM spans WHERE kind = 'llm' GROUP BY model;

-- 6. 查询某个 tool 的调用失败率
SELECT tool_name, count(*), sum(status = 'error') FROM spans WHERE kind = 'tool' GROUP BY tool_name;
```

---

## 5. 更贴近 SmithDB 的测试点

如果目标是存储系统研究，而不是业务观测 UI，可以重点测：

1. **写入吞吐**
   - 每秒 event 数
   - 每秒 span 数
   - batch size 对吞吐的影响

2. **运行中 trace 可见性**
   - event append 后多久能被查询
   - hot trace 查询是否需要读冷存储

3. **单 trace 加载延迟**
   - P50 / P95 / P99
   - span 数增加后的退化曲线

4. **compaction 成本**
   - event -> span view 的合并成本
   - 写放大
   - 读放大

5. **索引效果**
   - `trace_id` point/range query
   - `parent_span_id` children query
   - `kind/status/model/tool_name` filter
   - JSON attribute query
   - full-text query

6. **冷热分层**
   - running trace 在热层
   - completed trace flush 到冷层
   - 查询时合并热层和冷层

---

## 6. 建议路线

### 路线 A：先用公开数据

1. 下载 DeathStarBench traces。
2. 转成统一的 `traces/spans/events` 表。
3. 实现单 trace 加载和 span 树还原。
4. 测试查询延迟和索引设计。

### 路线 B：自己生成 Agent trace

1. 写一个 synthetic trace generator。
2. 生成 LLM/tool/retriever/code 等 span。
3. 生成 span_started/span_finished/token_chunk/error/retry 等 event。
4. 模拟长时间 running trace。
5. 测试 append-only + materialized view。

### 路线 C：接 OpenTelemetry

1. 用 OpenTelemetry Demo 或 LangChain/LangGraph 应用产生 trace。
2. 用 OpenTelemetry Collector 接收 OTLP。
3. 写一个自定义 exporter 落到你的存储系统。
4. 对比 Jaeger/Tempo/LangSmith 风格查询。

---

## 7. 我的建议

如果只是想马上试，优先顺序是：

```text
1. DeathStarBench traces：最快拿到真实 trace 结构
2. OpenTelemetry Demo：最快产生持续 workload
3. Synthetic Agent Trace Generator：最贴近 Agent/SmithDB 问题
```

对于研究 Agent trace 存储，最终还是需要 synthetic workload，因为公开数据集目前大多是微服务 trace，不完全覆盖 LLM/Agent 的特征：

- token streaming
- prompt/completion 大字段
- tool call
- retriever documents
- retry / reflection / planning
- long-running conversation
- cost/token 聚合

---

## 8. 本地可用 Synthetic Agent Trace Generator

我在本目录下补了一个可以直接跑的生成器：

```text
agent-trace-storage-notes/synthetic-agent-trace-generator/
├─ generate_agent_traces.py
├─ README.md
└─ sample-small/
   ├─ traces.jsonl
   ├─ spans.jsonl
   ├─ events.jsonl
   ├─ operations.jsonl
   ├─ otel_spans.jsonl
   └─ summary.json
```

快速运行：

```bash
cd /data/workspace/SQLEngine/agent-trace-storage-notes/synthetic-agent-trace-generator
python3 generate_agent_traces.py --out ./sample --num-traces 1000 --avg-spans 20 --avg-events-per-span 5 --otel-json
```

已经验证过的小样例：

```text
traces:      100
spans:       1175
events:      7138
operations:  9720
otel_spans:  1175
```

最推荐拿来做 replay 的文件是：

```text
operations.jsonl
```

它包含：

```text
insert_trace
insert_span
append_event
update_span
update_trace
query_trace_tree
query_failed_spans
```

如果想测试 append-only 架构，则优先消费：

```text
events.jsonl
```

再异步 materialize 出 `spans` / `traces` 视图。

---

## 9. 找到的现成工具判断

这次查到的现成工具里：

1. `streamfold/otel-loadgen`
   - 通用 OpenTelemetry load generator。
   - 适合压测 collector/backend。
   - 但不是 Agent/LLM 语义负载。

2. `telemetrygen`
   - OpenTelemetry Collector contrib 里的 synthetic telemetry generator。
   - 适合生成通用 traces/metrics/logs。
   - 但 span 结构和 attributes 不贴近 Agent。

3. `langsmith-starter-kit`
   - 更偏 LangSmith 项目、dataset、evaluator、trace 示例初始化。
   - 不是面向存储系统的高吞吐 workload generator。

所以如果目标是研究 Agent trace 存储，现阶段更实用的方案是：

```text
通用 OTel 工具压 ingest pipeline + 本地 synthetic Agent generator 压 Agent 语义存储模型
```
