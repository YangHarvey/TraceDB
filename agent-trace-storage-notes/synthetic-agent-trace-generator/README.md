# Synthetic Agent Trace Generator

这是一个可直接运行的 synthetic Agent trace 负载生成器，用于测试面向 Agent observability 的存储系统。

它生成 4 类 JSONL 文件：

| 文件 | 含义 |
|---|---|
| `traces.jsonl` | 物化后的 trace 摘要 |
| `spans.jsonl` | 物化后的 span 表，包含 `trace_id` / `span_id` / `parent_span_id` |
| `events.jsonl` | append-only event log |
| `operations.jsonl` | 可重放 workload，包含 `insert_trace`、`insert_span`、`append_event`、`update_span`、`update_trace`、`query_trace_tree` 等操作 |

## 快速开始

```bash
cd /data/workspace/SQLEngine/agent-trace-storage-notes/synthetic-agent-trace-generator
python3 generate_agent_traces.py --out ./sample --num-traces 1000 --avg-spans 20 --avg-events-per-span 5 --otel-json
```

生成后目录结构类似：

```text
sample/
├─ traces.jsonl
├─ spans.jsonl
├─ events.jsonl
├─ operations.jsonl
├─ otel_spans.jsonl
└─ summary.json
```

## 推荐参数

### 小规模冒烟测试

```bash
python3 generate_agent_traces.py --out ./sample-small --num-traces 100 --avg-spans 12 --avg-events-per-span 3 --otel-json
```

### 中等规模负载

```bash
python3 generate_agent_traces.py --out ./sample-medium --num-traces 10000 --avg-spans 20 --avg-events-per-span 5 --long-running-ratio 0.08 --error-ratio 0.05 --otel-json
```

### 偏写入压力负载

```bash
python3 generate_agent_traces.py --out ./sample-write-heavy --num-traces 50000 --avg-spans 30 --avg-events-per-span 8 --streaming-event-ratio 0.6 --query-ratio 0.05
```

### 偏长任务负载

```bash
python3 generate_agent_traces.py --out ./sample-long-running --num-traces 10000 --avg-spans 40 --long-running-ratio 0.25 --avg-events-per-span 6
```

## 数据模型

### `traces.jsonl`

示例字段：

```json
{
  "trace_id": "trc_xxx",
  "session_id": "sess_1",
  "user_id": "user_1",
  "root_span_id": "spn_xxx",
  "start_ns": 1790000000000000000,
  "end_ns": 1790000001234000000,
  "status": "success",
  "total_tokens": 12345,
  "total_cost_usd": 0.0123,
  "span_count": 20,
  "event_count": 130,
  "metadata": {"synthetic": true, "workload": "agent_trace"}
}
```

### `spans.jsonl`

关键字段：

```text
trace_id
span_id
parent_span_id
name
kind: agent / chain / llm / tool / retriever / code / http
start_ns
end_ns
status
latency_ms
attributes
children
```

其中 `parent_span_id` 用来还原 span 树。

### `events.jsonl`

关键字段：

```text
event_id
trace_id
span_id
timestamp_ns
event_type
payload
```

常见 `event_type`：

```text
span_started
span_finished
span_failed
llm_first_token
llm_stream_chunk
tool_call_started
tool_call_finished
retriever_documents
retry_scheduled
log
checkpoint
```

### `operations.jsonl`

这是最适合作为存储系统 replay 的文件。

它把一次 trace 的生命周期拆成按时间排序的操作：

```text
insert_trace
insert_span
append_event
update_span
update_trace
query_trace_tree
query_failed_spans
```

如果你想比较两种设计：

1. **Update-heavy**：按 `operations.jsonl` 重放，执行 `update_span` / `update_trace`。
2. **Append-only**：只消费 `events.jsonl`，后台 materialize 出 `spans` / `traces` 视图。

## 适合测试的指标

- event 写入吞吐
- span insert/update 吞吐
- 单 trace 树加载延迟
- `trace_id` 查询延迟
- `parent_span_id` children 查询延迟
- `kind/status/model/tool_name` 过滤性能
- append-only event 到 materialized span view 的 compaction 成本
- hot running trace 的实时可见性
- 大 payload 对写放大和读放大的影响

## 和真实 Agent 场景的对应关系

| 生成器字段 | Agent 场景 |
|---|---|
| `kind=llm` | 模型调用 |
| `kind=tool` | 工具调用 |
| `kind=retriever` | RAG 检索 |
| `llm_stream_chunk` | streaming token/chunk |
| `retry_scheduled` | 重试 / self-correction |
| `input_ref/output_ref` | 大 prompt/output 放对象存储后的引用 |
| `long_running_ratio` | 长时间运行任务或长对话 |

## 参数说明

```text
--num-traces              trace 数量
--avg-spans               每个 trace 平均 span 数
--max-depth               span 树最大深度
--avg-events-per-span     每个 span 平均 event 数
--long-running-ratio      running/长任务比例
--error-ratio             span 错误率
--retry-ratio             retry event 比例
--large-payload-ratio     大 payload/ref 比例
--streaming-event-ratio   LLM streaming event 比例
--query-ratio             operations 中查询操作比例
--otel-json               额外生成 OTLP-ish `otel_spans.jsonl`
```
