# RocksDB Agent Observability Benchmark 与 SmithDB 公开指标对比

## 1. 结论摘要

本轮实验使用 `standard` profile，在本地单机 RocksDB 上验证了 `trace_first_with_indexes` layout 对 Agent observability workload 的基础性能。

结果显示：在 **本地、单进程、热缓存、索引充分、无服务层开销** 的条件下，RocksDB 对核心查询可以达到微秒级延迟：

- `get_span` P50: `3µs`
- `query_trace_tree` P50: `60µs`
- `filter_spans` P50: `33µs`
- `search_text` P50: `36µs`
- freshness P50: `1µs`

这说明 RocksDB 作为底层 trace store 有很大的性能余量。

但当前实验规模仍偏小：

- logical input bytes 约 `800MB`
- RocksDB SST size 约 `1.95GB`
- 整个实验目录约 `3.4GB`

因此当前结果更适合作为 **hot-cache / small-to-medium dataset upper bound**，不能直接代表生产级 Agent observability 系统在大规模冷/温数据上的端到端性能。

## 2. 实验配置

结果目录：

```text
bench-results/rocksdb-standard-20260523-0028
```

运行命令核心参数：

```bash
python3 agent-trace-storage-notes/synthetic-agent-trace-generator/generate_agent_traces.py \
  --profile standard \
  --out bench-results/rocksdb-standard-20260523-0028 \
  --otel-json

build/rocksdb_trace_bench \
  --ops bench-results/rocksdb-standard-20260523-0028/operations.jsonl \
  --db bench-results/rocksdb-standard-20260523-0028/rocksdb-trace-first-indexed-db \
  --layout trace_first_with_indexes \
  --progress_interval 1000000 \
  > bench-results/rocksdb-standard-20260523-0028/rocksdb_trace_first_with_indexes.json
```

## 3. 数据规模

| 指标 | 数值 |
|---|---:|
| traces | `10,000` |
| spans | `212,435` |
| events | `1,533,055` |
| payloads | `19,283` |
| operations | `2,007,544` |
| logical write ops | `1,977,254` |
| RocksDB physical writes, including indexes | `12,765,701` |
| payload bytes | `139,992,650` |
| logical input bytes | `800,553,386` |
| RocksDB SST size | `1,951,347,347` |
| experiment directory size | about `3.4GB` |

当前规模对验证功能和初步性能足够，但对数据库 benchmark 来说仍然偏小。尤其是 RocksDB SST 只有约 `2GB`，很容易被 OS page cache / block cache 覆盖，因此读延迟会明显偏向热缓存场景。

## 4. 运行耗时与资源

| 阶段 | Wall time | Max RSS |
|---|---:|---:|
| 数据生成 | `492.23s` | `2.38GB` |
| RocksDB replay | `44.26s` | `684MB` |

观察：

1. 数据生成比 RocksDB replay 慢得多。
2. 当前 Python generator 会在内存中聚合 traces/spans/events/operations 后统一写出，扩大到 10GB+ 级别时会成为瓶颈。
3. 若要跑 10GB/100GB 级别实验，建议先把 generator 改为 streaming/batched 输出。

## 5. RocksDB 总体性能

| 指标 | 数值 |
|---|---:|
| replay elapsed | `44.1947s` |
| end-to-end ops/sec | `45,425` |
| write-path ops/sec | `425,717` |
| query avg | `12.13µs` |
| query P50 | `1µs` |
| query P95 | `62µs` |
| query P99 | `117µs` |
| freshness P50 | `1µs` |
| freshness P95 | `1µs` |
| freshness P99 | `7µs` |
| index write amplification | `5.46x` |
| storage amplification | `2.44x` |
| parse errors | `0` |
| write errors | `0` |

注意：全局 `query P50 = 1µs` 受大量 `freshness_probe` 和热缓存 key lookup 影响，不应单独作为核心 UI 查询延迟结论。更有意义的是按 operation 拆分后的指标。

## 6. 按操作类型拆分

| Operation | Count | P50 | P95 | P99 | 说明 |
|---|---:|---:|---:|---:|---|
| `get_span` | `1,831` | `3µs` | `34µs` | `43µs` | 单 span/run lookup，基本是 point lookup |
| `query_trace_tree` | `2,349` | `60µs` | `119µs` | `159µs` | trace tree load，扫描该 trace 下 spans/events |
| `filter_spans` | `1,319` | `33µs` | `50µs` | `61µs` | 当前主要扫描二级索引 |
| `search_text` | `902` | `36µs` | `58µs` | `68µs` | 简单倒排索引，不等价完整全文搜索系统 |
| `query_running_traces` | `882` | `56µs` | `232µs` | `418µs` | running trace index scan |
| `get_thread` | `920` | `18µs` | `36µs` | `45µs` | thread reconstruction index scan |
| `list_traces_by_session` | `511` | `20µs` | `36µs` | `43µs` | session index scan |

## 7. 与 SmithDB 公开指标对比

SmithDB 公开资料中常见性能声明：

| Workload | SmithDB 公开 P50 | TraceDB/RocksDB 本轮 P50 | 表面差距 |
|---|---:|---:|---:|
| Trace tree load | about `92ms` | `60µs` | about `1,500x` faster |
| Single run load | about `71ms` | `3µs` | about `23,000x` faster |
| Run filtering | about `82ms` | `33µs` | about `2,400x` faster |
| Full-text search | about `400ms` | `36µs` | about `11,000x` faster |
| Ingestion freshness | sub-second median | `1µs` | much lower |

这个表格只能说明：**在当前本地 RocksDB benchmark 口径下，底层 indexed KV 查询延迟远低于 SmithDB 公开的产品级系统延迟。**

不能据此直接宣称 TraceDB 产品级性能比 SmithDB 快几千倍，因为两者口径不同。

## 8. 为什么不是 apples-to-apples

SmithDB 公开数字更可能是 LangSmith 产品/系统层面的用户可感知延迟，可能包含：

- API / RPC / 网络开销
- 鉴权、多租户、project/session 过滤
- JSON parse / serialize
- trace tree 组装
- payload hydrate
- 后台 ingestion pipeline
- 分布式索引/存储系统成本
- 真实生产数据分布
- 冷热数据混合
- 并发读写竞争

本轮 TraceDB/RocksDB benchmark 则是：

- 单机
- 单进程
- 本地 RocksDB API
- 很可能热 page cache
- 无网络
- 无服务层
- 无鉴权
- 查询多为 point lookup 或 index scan
- `search_text` 是简单 token inverted index，不是完整全文检索系统
- `filter_spans` 当前主要返回索引命中，不强制 hydrate 完整 span/event/payload

因此更准确的结论是：

```text
当前 RocksDB baseline 展示了底层存储引擎在 Agent observability workload 上的性能上限；它证明 trace-aware index layout 有很大潜力，但还不能直接等价为生产系统端到端性能。
```

## 9. 当前 1GB/2GB 级数据规模的问题

用户判断“目前 1GB 数据还是太少”是正确的。

主要问题：

1. **数据容易完全进入 cache**
   - SST 约 `1.95GB`。
   - 在现代开发机上非常容易被 page cache 覆盖。
   - 微秒级读延迟更多代表 hot-cache upper bound。

2. **无法观察 compaction 长尾**
   - 2GB 级别数据不足以稳定触发复杂 LSM compaction 行为。
   - P99/P999 写入抖动、读放大、level amplification 都不明显。

3. **无法模拟真实 observability retention**
   - Agent observability 系统通常面对更长时间窗口、更多 tenants、更多 sessions。
   - 需要至少 10GB/100GB 级别才能看到 layout 差异。

4. **search/filter 过于理想**
   - 当前 `filter_spans` 和 `search_text` 主要扫索引。
   - 真实 UI 往往还需要 hydrate 原始 rows、payload refs、分页、排序和聚合。

## 10. 下一阶段实验建议

建议将 benchmark 分为四档：

| Tier | 目标数据量 | 用途 |
|---|---:|---|
| `standard` | 1-3GB | 功能验证、热缓存上限 |
| `medium` | 10-30GB | 本地严肃 benchmark，开始超过 cache 舒适区 |
| `large` | 100GB+ | 观察 LSM compaction、读写放大、冷热数据 |
| `stress` | 500GB-1TB | 接近生产 retention/多租户压力 |

建议优先补一个 `medium` profile，而不是直接跳到 100GB：

```text
medium_10g:
  num_traces: 50k-100k
  avg_spans: 25-35
  avg_events_per_span: 6-10
  payload_mode: refs
  large_payload_ratio: 0.05-0.10
  expected operations: 10M-30M
  expected RocksDB SST: 10GB-30GB
```

然后再跑：

```text
large_100g:
  num_traces: 500k-1M
  expected operations: 100M+
  expected RocksDB SST: 100GB+
```

## 11. 下一步工程改造建议

为了跑真正的大规模实验，需要先做几个工程优化：

### 11.1 Streaming generator

当前 generator 聚合所有数据后再写出，10GB+ 会明显吃内存和时间。建议改为：

- 分批生成 traces/spans/events
- operations 分桶写临时文件
- 最后按 timestamp 做 external merge sort
- summary 逐步聚合 counters

### 11.2 Full hydration benchmark

让这些查询更接近 SmithDB/LangSmith UI：

- `filter_spans` 命中索引后读取完整 span row
- `search_text` 命中倒排索引后读取 event/span/payload metadata
- `query_trace_tree` 可选 hydrate payload refs
- 输出 returned rows/bytes 更真实

### 11.3 冷缓存/温缓存实验

至少区分三类：

- hot-cache：当前结果，性能上限
- warm-cache：常用索引热、payload/旧 trace 冷
- cold-cache：重启进程或尽量 drop cache 后读取

### 11.4 并发服务层 benchmark

最终要加一个简单 HTTP/gRPC wrapper：

- 多客户端并发
- 返回完整 JSON
- 模拟 UI 请求比例
- 测端到端 P50/P95/P99/P999

这样才更适合和 SmithDB 产品级数字比较。

## 12. 推荐后续实验顺序

```text
Step 1: 新增 medium_10g profile
Step 2: generator 改 streaming/batched 输出
Step 3: 跑 medium_10g 的 trace_first_with_indexes
Step 4: 跑 medium_10g 的 layout 对比：trace_first / trace_first_with_indexes / append_only / time_first
Step 5: 实现 full hydration 查询模式
Step 6: 跑 100GB large profile
Step 7: 加 HTTP/gRPC 服务层，做端到端对比
```

## 13. 总结

本轮结果的核心价值是：

```text
RocksDB + trace-aware indexes 在 Agent observability workload 上具有非常高的底层性能上限。
```

但当前数据规模仍然偏小，且测试口径偏底层。下一阶段应该把 benchmark 从 “microbenchmark / hot-cache upper bound” 推进到：

```text
10GB-100GB 数据规模 + full hydration + 冷热缓存 + 并发服务层端到端 benchmark。
```

只有这样，和 SmithDB 公开指标的比较才会更有说服力。
