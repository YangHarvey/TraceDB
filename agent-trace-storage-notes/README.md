# Agent Trace 与存储系统结合点笔记

## 1. 背景场景

这次讨论的场景来自一篇关于 LangChain 自研 `SmithDB` 的文章。核心问题是：AI Agent 在运行过程中会产生大量执行轨迹数据，这些数据不只是普通日志，而是包含任务执行路径、模型调用、工具调用、上下文变化、错误、重试、成本、延迟等信息。

这类数据需要被高效地：

- 写入
- 更新
- 查询
- 搜索
- 聚合分析
- 实时观察
- 长期归档

因此它和存储系统有很强的结合点。

---

## 2. Trace 是什么

`Trace` 可以理解为一次完整任务或请求的全链路执行轨迹。

例如用户让 Agent：

> 帮我分析一篇论文，并生成总结。

那么从用户发起请求，到 Agent 最终输出结果，中间所有步骤合起来就是一个 `trace`。

它可能包含：

- 用户输入
- Agent 规划过程
- 模型调用
- 工具调用
- 文件读取
- 网络搜索
- 重试
- 报错
- 最终输出
- 总耗时
- 总 token
- 总成本

一句话：

> `Trace` 是一次 Agent 任务的完整执行记录。

---

## 3. Span 是什么

`Span` 是 `Trace` 里面的一个执行片段。

如果 `Trace` 是完整链路，那么 `Span` 就是链路里的一个阶段或步骤。

例如：

```text
Trace: 分析论文并生成总结
├─ Span: 理解用户需求
├─ Span: 读取论文
│  ├─ Span: 下载 PDF
│  ├─ Span: 解析 PDF
│  └─ Span: 提取表格
├─ Span: 总结论文
│  ├─ Span: 总结摘要
│  ├─ Span: 总结方法
│  └─ Span: 总结实验
└─ Span: 生成最终回答
```

`Span` 通常具有：

- `span_id`
- `trace_id`
- `parent_span_id`
- `name`
- `type`
- `start_time`
- `end_time`
- `status`
- `input`
- `output`
- `error`
- `metadata`

一句话：

> `Span` 是 `Trace` 中一个有开始和结束的执行阶段，并且 `Span` 之间可以嵌套形成树。

---

## 4. Event 是什么

`Event` 是某个时间点发生的一件具体事情。

它比 `Span` 更细，通常挂在某个 `Span` 下面。

关系可以理解为：

```text
Trace = 一次完整任务
Span = 任务里的一个阶段
Event = 某个阶段中某个时间点发生的事情
```

例如：

```text
Trace: 搜索并总结 SmithDB
├─ Span: 调用搜索工具
│  ├─ Event: search_started
│  ├─ Event: request_sent
│  ├─ Event: response_received
│  └─ Event: search_finished
└─ Span: 调用大模型生成总结
   ├─ Event: llm_call_started
   ├─ Event: first_token_received
   ├─ Event: stream_chunk_received
   └─ Event: llm_call_finished
```

区别：

| 概念 | 含义 | 是否有持续时间 |
|---|---|---|
| `Trace` | 一次完整任务 | 有 |
| `Span` | 一个执行阶段 | 有 |
| `Event` | 某个瞬间发生的事情 | 通常没有 |

一句话：

> `Event` 是 `Span` 运行过程中发生的细粒度时间点记录。

---

## 5. Trace / Span / Event 的存储关系

一种常见的逻辑模型是：

```text
traces
  trace_id
  user_id
  session_id
  root_span_id
  start_time
  end_time
  status
  total_tokens
  total_cost
  metadata

spans
  span_id
  trace_id
  parent_span_id
  name
  span_type
  start_time
  end_time
  status
  input_ref
  output_ref
  error
  attributes

events
  event_id
  trace_id
  span_id
  timestamp
  event_type
  payload
```

嵌套关系一般不是通过物理嵌套 JSON 存储，而是通过：

```text
span_id
parent_span_id
trace_id
```

在查询时还原成树。

---

## 6. 为什么这个场景对存储系统有挑战

Agent trace 数据不是一次性写完的，而是长时间持续产生。

一个任务可能会经历：

```text
insert trace(status=running)
insert span(status=running)
insert event(span_started)
insert event(tool_called)
update span(status=success, end_time=...)
insert another span
insert more events
update trace(status=success, end_time=..., total_cost=...)
```

所以它同时具有：

- 高频 `insert`
- 持续 `update`
- 长生命周期对象
- 树状结构
- 半结构化 JSON
- 大文本 input/output
- 全文搜索需求
- 聚合分析需求
- 实时可见需求

这使它不同于普通日志，也不同于普通 OLTP 表。

---

## 7. Span 为什么也需要更新

`Span` 开始时通常只知道：

```text
span_id
trace_id
parent_span_id
name
start_time
status = running
```

结束后才知道：

```text
end_time
duration_ms
status
output
error
token_count
cost
```

朴素做法是：

```sql
INSERT INTO spans (..., start_time, status)
VALUES (..., now(), 'running');

UPDATE spans
SET end_time = now(),
    status = 'success',
    duration_ms = ...
WHERE span_id = ...;
```

但大规模系统会倾向于把更新转成追加事件：

```text
Event: span_started
Event: span_finished
Event: span_failed
```

然后异步合并出最终的 `span` 状态视图。

---

## 8. 为什么不能只靠 LSM-tree

LSM-tree 擅长高吞吐写入和 append-heavy workload，例如：

```text
trace_id + timestamp + event_id -> event_payload
```

但 Agent trace 系统需要的不只是写得快，还包括：

- 快速加载单个 trace
- 快速还原 span 树
- 查询 span 当前状态
- 支持 JSON 过滤
- 支持全文搜索
- 支持延迟、成本、token 聚合
- 支持长时间 running 的 trace
- 支持热数据实时读取

LSM-tree 可以作为底层组件，但不是完整答案。

更合理的方向是：

```text
事件日志 + 物化视图 + 多索引 + 冷热分层 + compaction
```

---

## 9. 一个可能的系统架构

可以抽象为：

```text
Agent SDK
  -> Ingestion Service
  -> Hot Buffer / WAL / Local SSD
  -> Append-only Event Store
  -> Compaction / Merge
  -> Materialized Trace View
  -> Span Tree Index
  -> Full-text / JSON / OLAP Indexes
  -> Query Service
```

分层职责：

| 层 | 作用 |
|---|---|
| Ingestion | 接收高频 trace/span/event 写入 |
| Hot Buffer | 支持运行中 trace 的实时读取 |
| Event Store | 保存完整 append-only 原始事件 |
| Compaction | 合并事件，生成稳定 segment |
| Materialized View | 提供 trace/span 当前状态 |
| Search Index | 支持全文搜索和 JSON 过滤 |
| OLAP Layer | 支持成本、延迟、token 等聚合分析 |
| Object Storage | 存储大文本、多模态内容、大 JSON |

---

## 10. 和存储系统的潜在研究/工程结合点

这个方向值得关注的点包括：

1. **面向 Agent Trace 的数据模型**
   - 如何表达 trace/span/event
   - 如何表达树状 span 结构
   - 如何处理长生命周期执行过程

2. **Append-only 事件存储**
   - 如何把 update 转换为事件追加
   - 如何降低写放大
   - 如何保证事件顺序和一致性

3. **实时查询与最终一致性**
   - 运行中的 trace 如何实时可见
   - 物化视图如何异步更新
   - 用户查询时如何合并热数据和冷数据

4. **Compaction 策略**
   - 如何把大量细粒度 event 合并成 span 状态
   - 如何按 trace 维度组织 segment
   - 如何降低读放大和写放大

5. **索引设计**
   - `trace_id` 查询
   - `span_id` 查询
   - `parent_span_id` 树查询
   - 时间范围查询
   - JSON path 查询
   - 全文搜索
   - 成本、延迟、token 聚合

6. **冷热分层**
   - 运行中的 trace 放热层
   - 完成后的 trace 进入冷层
   - 大字段进入对象存储
   - 元数据和索引保留在查询层

7. **大模型输出流式数据处理**
   - 是否逐 token 记录
   - 是否按 chunk 聚合
   - 如何记录首 token 延迟
   - 如何在不放大写入的前提下保留可观测性

8. **面向 Agent 的专用数据库/存储层**
   - 类似 SmithDB 的专用 trace store
   - 不只是日志系统，也不只是 OLAP 系统
   - 更像面向 Agent runtime 的 observability data layer

---

## 11. 一句话总结

Agent trace 存储的核心问题是：

> 如何把一个长时间运行、不断变化、树状嵌套、包含大量半结构化和大文本数据的 Agent 执行过程，高吞吐写入、实时观察、低延迟还原，并支持搜索和分析。

这使它成为一个很适合和存储系统结合的方向。