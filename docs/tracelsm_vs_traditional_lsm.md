# TraceLSM 相比传统 LSM-tree 的实现内容与对比总结

## 1. 核心结论

TraceLSM 的目标不是在所有 KV workload 上替代传统 LSM-tree，而是在 **Agent observability trace workload** 上利用传统 LSM-tree 不具备的 trace 语义来降低读放大、写放大和本地存储成本。

最重要的区别：

```text
传统 LSM-tree 只理解 key/value 和 key order；
TraceLSM 理解 trace/span/event/payload 的生命周期和语义。
```

因此，TraceLSM 的优势主要来自：

```text
large payload separation
+ trace lifecycle-aware semantic compaction
+ object-backed trace segment
+ hybrid cache
```

一句话 claim：

```text
TraceLSM reduces read and write amplification for agent observability workloads by exploiting trace lifecycle semantics unavailable to traditional LSM compaction.
```

## 2. Agent trace workload 为什么不同

Agent trace 和传统 RPC trace / 普通日志不同：

| 特征 | 传统 RPC trace / log | Agent trace |
|---|---|---|
| 生命周期 | 通常短，一次性写完 | 可能 long-running，持续 append/update |
| payload | 小 metadata 为主 | prompt/completion/tool output/retrieved docs 很大 |
| 写入模式 | append 为主 | insert span、append event、update span/trace 混合 |
| 查询路径 | trace lookup / log scan | trace tree、span detail、filter、search、thread reconstruction |
| 完成后状态 | 普通历史记录 | 可转为 immutable completed execution record |
| 数据组织机会 | key/time based | trace lifecycle + span tree semantic compaction |

传统 LSM-tree 面对这些 workload 时，只能做 key/value 级别的 flush/compaction，无法理解：

```text
哪些 key 属于同一个 trace
哪个 trace 已经 completed
哪些 span update 可以合并
哪些 event 可以组成 timeline
哪些 payload 可以外置
哪些 rows 可以 materialize 成 trace segment
```

## 3. 传统 LSM-tree baseline

传统 LSM-tree 在 Agent trace 上可以这样存：

```text
T#{trace_id}                         -> trace state
S#{trace_id}#{span_id}               -> span state/update
E#{trace_id}#{timestamp_ns}#{event_id}-> event
C#{trace_id}#{parent_span_id}#{span_id}-> child ref
```

它的优点：

- 写入吞吐高。
- point lookup 很快。
- prefix scan 支持 `get_trace_tree(trace_id)`。
- 实现简单、成熟。

但它的问题是：

1. 大 payload 会进入 LSM value，导致 compaction 反复搬运大 value。
2. span/trace 多次 update 会产生多个版本或 delta。
3. completed trace 查询仍然需要 scan 多个 KV 并重建 tree。
4. cold trace 仍然占用本地 SSD 和 cache。
5. 普通 compaction 不知道 trace 已完成，无法进行 semantic reduction。

## 4. TraceLSM 实现内容

TraceLSM 可以先作为 RocksDB 之上的一层实现，而不是一开始 fork RocksDB。当前 C++ MVP 已新增 `tracelsm_object` layout：写入路径先在 TraceLSM 层把超过阈值的 inline `text` payload 写入本地 object store，再把 compact JSON/object ref 写入 RocksDB；查询路径默认读取 compact value，不自动 hydration。

推荐分层：

```text
Application / Benchmark
        |
        v
TraceLSM Layer
        |
        v
RocksDB substrate
        |
        v
Local SSD + Object Store
```

RocksDB 负责：

```text
WAL
MemTable
SST
normal compaction
Bloom/filter block
block cache
prefix scan
point lookup
```

TraceLSM layer 负责：

```text
trace-aware key encoding
secondary indexes
payload splitter
object store manager
semantic compaction scheduler
TraceSegment builder/reader
hybrid cache
query planner
```

## 5. 实现模块

### 5.1 ObjectStore abstraction

接口：

```cpp
class ObjectStore {
 public:
  Status Put(std::string_view key, std::string_view data);
  Status Get(std::string_view key, std::string* data);
  Status Delete(std::string_view key);
};
```

第一阶段实现：

```text
LocalFileObjectStore
```

后续实现：

```text
S3CompatibleObjectStore / COSObjectStore
LatencyInjectedObjectStore
```

对象布局：

```text
payloads/{tenant}/{date}/{trace_id}/{span_id}/{payload_id}.payload
segments/{tenant}/{date}/{trace_id}.segment
manifests/{trace_id}.json
```

### 5.2 Payload splitter

写入 span/event 时，把数据拆成：

```text
metadata -> RocksDB/LSM
large payload -> local payload cache + object storage
```

LSM 中仅保留：

```json
{
  "payload_ref": "obj://payloads/...",
  "payload_bytes": 65536,
  "payload_digest": "sha256:...",
  "payload_state": "local|uploading|remote|local+remote"
}
```

这样可以避免大 prompt/completion/tool output 污染 LSM compaction 和 block cache。

### 5.3 TraceSegment builder

当 trace completed：

```text
update_trace(status=success/error)
  -> enqueue Q#completed#{timestamp_ns}#{trace_id}
```

后台 compactor：

```text
1. scan Q#completed#*
2. scan T#{trace_id}, S#{trace_id}#*, E#{trace_id}#*, C#{trace_id}#*
3. merge span updates into final span state
4. order events into timeline
5. collect payload refs
6. build TraceSegment
7. write segment to object store
8. write G#{trace_id} -> segment manifest
```

### 5.4 TraceSegment format

MVP 可先用 JSON：

```json
{
  "trace": {...},
  "spans": [...],
  "events": [...],
  "payload_refs": [...]
}
```

后续优化为二进制格式：

```text
header
trace summary
span table final states
parent-child adjacency index
event timeline
payload ref table
span_id -> offset index
optional token/error mini-index
footer + checksum
```

### 5.5 SemanticCompactor

SemanticCompactor 是核心创新模块。它不是普通 key-level compaction，而是 trace-level materialization。

普通 LSM compaction：

```text
sorted key/value merge
remove tombstone
keep latest version
```

TraceLSM semantic compaction：

```text
trace/span/event deltas
  -> final span tree
  -> ordered event timeline
  -> compacted trace segment
  -> payload ref table
```

### 5.6 Hybrid cache

两类 cache：

```text
SegmentCache:
  trace_id -> decoded TraceSegment

PayloadCache:
  payload_ref -> bytes / local file path
```

Eviction 策略：

| Cache | Policy |
|---|---|
| SegmentCache | trace hotness、recent completed、thread prefetch |
| PayloadCache | size-aware、upload-state-aware、last access |

重要约束：

```text
local-only payload 不能驱逐；remote-confirmed payload 可以驱逐。
```

## 6. 查询路径对比

### 6.1 `get_trace_tree(trace_id)`

传统 LSM：

```text
scan T#{trace_id}
scan S#{trace_id}#*
scan E#{trace_id}#*
scan C#{trace_id}#*
merge span versions
build tree
optional hydrate payload
```

TraceLSM：

```text
if G#{trace_id} exists:
    read TraceSegment
    merge late overlay if exists
else:
    scan RocksDB delta rows
optional hydrate payload refs
```

优势：

```text
fewer keys
fewer seeks
fewer block reads
less JSON decode
tree already materialized
span final state already merged
```

### 6.2 `get_span(span_id)`

传统 LSM：

```text
B#{span_id} -> trace_id
S#{trace_id}#{span_id}
```

TraceLSM：

```text
B#{span_id} -> trace_id
if segment exists:
    use segment span offset index
else:
    S#{trace_id}#{span_id}
```

优势主要在 completed/cold trace 的 segment read，而不是 hot point lookup。

### 6.3 `filter_spans(...)`

传统 LSM：

```text
scan secondary index
fetch span rows from LSM
```

TraceLSM：

```text
scan secondary index
if completed trace:
    fetch span from segment offset
else:
    fetch span from LSM
```

### 6.4 `search_text(keyword)`

传统 LSM：

```text
scan W# token index or scan payload text if inline
```

TraceLSM：

```text
hot/recent: local W# token index
completed segment: segment mini-index
large payload: payload object search metadata / external search optional
```

## 7. 相对传统 LSM 的优势

### 7.1 减少 trace tree 读放大

传统 LSM 对 completed trace 仍然需要读很多 rows。

TraceLSM 可以将 completed trace 转成一个或少数几个 TraceSegment object。

预期收益：

```text
scanned_keys ↓
scanned_bytes ↓
iterator seek ↓
get_trace_tree latency ↓
```

### 7.2 减少 span/update 多版本成本

Agent trace 中常见：

```text
insert_span(status=running)
update_span(progress=25%)
update_span(progress=50%)
update_span(status=success)
```

TraceLSM completed 后可以：

```text
multiple span deltas -> final span state
```

预期收益：

```text
row count ↓
storage amplification ↓
read amplification ↓
```

### 7.3 大 payload 不污染 LSM

传统 LSM inline payload：

```text
large value repeatedly participates in flush/compaction
block cache polluted by large payload
write amplification high
```

TraceLSM：

```text
metadata in LSM
large payload in object storage
```

预期收益：

```text
LSM compaction bytes ↓
write amplification ↓
block cache efficiency ↑
local SSD footprint ↓
```

### 7.4 利用 trace lifecycle 做冷热转换

传统 LSM：

```text
level is based on size/age, not trace lifecycle
```

TraceLSM：

```text
running trace -> hot delta layout
completed trace -> compacted segment
cold trace -> object storage
late event -> overlay + re-segment
```

### 7.5 降低 cold retention 成本

传统 LSM：

```text
all historical trace data remains on local SSD unless externally archived
```

TraceLSM：

```text
cold completed segment + payload -> object storage
local SSD keeps hot metadata/cache/index
```

预期收益：

```text
local SSD bytes ↓
retention cost ↓
cache pollution ↓
```

## 8. 什么时候优势不明显

TraceLSM 不应宣称总是比传统 LSM 好。

优势不明显的情况：

1. **纯 point lookup**：RocksDB 已经非常快。
2. **payload 很小**：value separation 收益有限。
3. **trace 很短**：semantic compaction 收益有限。
4. **全是 running trace**：trace 未 completed，不能生成 stable segment。
5. **强实时全文搜索**：可能需要外部搜索系统配合。

更准确的说法：

```text
TraceLSM improves payload-heavy, update-heavy, trace-tree-centric, lifecycle-rich agent observability workloads.
```

## 9. 对比实验设计

### 9.1 Baselines

至少需要四组：

| Baseline | 含义 | 证明点 |
|---|---|---|
| Plain LSM | trace/span/event 直接 KV，payload inline | 普通 LSM 基线 |
| LSM + indexes | trace-first + 二级索引，payload inline | key layout 和 index 的收益 |
| LSM + value separation | metadata in LSM, payload external/blob | 大 value separation 的收益 |
| TraceLSM | value separation + semantic compaction + segment | trace semantic compaction 的额外收益 |

可扩展 baseline：

```text
RocksDB BlobDB
ClickHouse
Elasticsearch/OpenSearch
Tempo/Jaeger
PostgreSQL JSONB
```

### 9.2 指标

核心指标：

```text
get_trace_tree latency P50/P95/P99/P999
get_span latency
filter/search latency
freshness latency
scanned_keys
scanned_bytes
read amplification
write amplification
storage amplification
local SSD bytes
object storage bytes
object GET/PUT count
payload hydrate latency
semantic compaction time
row reduction ratio
segment size / raw rows size
cache hit ratio
```

### 9.3 实验 workload

需要覆盖：

```text
standard_10g
large_100g
payload_heavy_100g
write_heavy
long_running
search_heavy
thread_heavy
cold_cache
warm_cache
hot_cache
```

## 10. 最小可行实现路线

### MVP 1：ObjectStore + PayloadSplitter

实现：

```text
LocalFileObjectStore
payload_ref metadata
large payload externalization
```

对比：

```text
LSM inline payload
vs
LSM object payload
```

### MVP 2：SemanticCompactor + TraceSegment

实现：

```text
completed trace queue
TraceSegmentBuilder
TraceSegmentReader
G#{trace_id} manifest
get_trace_tree from segment
```

先不删除原始 rows，只比较读取收益。

### MVP 3：GC / row reduction

实现：

```text
segment durable 后删除 S/E/C old rows
late overlay
re-segment
```

比较：

```text
storage amplification
read amplification
row reduction ratio
```

### MVP 4：Hybrid cache

实现：

```text
SegmentCache
PayloadCache
trace-aware eviction
```

比较：

```text
hot/warm/cold cache latency
object GET count
payload hydrate latency
```

## 11. 论文表述建议

不建议 claim：

```text
TraceLSM is faster than LSM-tree for all workloads.
```

建议 claim：

```text
TraceLSM exploits agent trace lifecycle semantics to reduce read/write amplification and local storage cost for payload-heavy, update-heavy observability workloads.
```

更具体：

```text
For completed agent traces, TraceLSM transforms write-optimized trace deltas into read-optimized object-backed trace segments, while separating large payloads from LSM compaction.
```

## 12. 当前判断

TraceLSM 相比传统 LSM-tree 的优势是明确的，但必须在正确 workload 上证明：

```text
large payload
long-running trace
frequent append/update
completed trace tree load
cold retention
thread reconstruction
```

如果只测普通 KV point lookup，TraceLSM 不一定有优势。

最核心的实验应该证明：

```text
semantic compaction 把 completed trace 从大量 delta rows 转成 compacted TraceSegment，显著减少 get_trace_tree 的 read amplification；
object-backed payload separation 避免大 prompt/tool output 进入 LSM compaction，显著减少 write amplification 和本地 SSD 占用。
```

这就是 TraceLSM 相比传统 LSM-tree 的核心价值。
