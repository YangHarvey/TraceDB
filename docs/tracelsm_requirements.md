# TraceLSM 需求清单（工程视角）

> 状态：rolling 文档，跟随实现进度更新。
> 最后更新：2026-05-26。
>
> 范围：当前 `tools/` 下 C++ TraceLSM 原型 + Python 工具链 + benchmark 套件。
> 目的：在一个地方对照看"已实现 / 进行中 / 待办（按优先级）"，并保留架构演进的关键思路。
> 与 `docs/tracelsm_object_storage_design.md` 的关系：本文是工程交付视角；那份是设计文档/研究计划视角。

---

## 1. 总览矩阵

| 模块 | 状态 | 备注 |
|---|---|---|
| Synthetic agent trace generator | ✅ 已实现 | `agent-trace-storage-notes/synthetic-agent-trace-generator/` |
| RocksDB baseline (`trace_first_with_indexes`) | ✅ 已实现 | `tools/rocksdb_trace_bench.cc`，10GB / 100GB 已跑过；2026-05-26 修复 update 覆盖语义（read-modify-write） |
| ClickHouse baseline | ✅ 已实现 | `tools/clickhouse_trace_bench.py`，100GB 已跑过 |
| TraceLSM C++ wrapper（`tracelsm_object` layout） | ✅ 已实现 | `tools/tracelsm_store.{h,cc}` |
| ObjectStore 抽象 + LocalFile / 腾讯云 COS 后端 | ✅ 已实现 | `tools/tracelsm_object_store.{h,cc}` |
| COS 写入并发流水线（worker pool + pipeline） | ✅ 已实现 | `--object_concurrency / --pipeline_depth` |
| 10GB / 100GB TraceLSM-COS vs RocksDB 对比 | ✅ 已完成 | 见第 5 节结果摘要 |
| Bench artifact 清理脚本 | ✅ 已实现 | `tools/clean_bench_artifacts.sh` |
| 异步本地 WAL（写完即返回） | ⏳ **P2 待办** | 见第 4.1 节，**当前同步等 COS 是写吞吐瓶颈** |
| Semantic compaction | ⏳ P3 待办 | 论文主线之一，依赖异步 WAL 落地 |
| Hot/Cold 分层 + payload cache | ⏳ P3 待办 | 设计文档 Phase 4 |
| 查询回落 COS 路径 + 长尾验证 | ⏳ P1 待办 | 当前查询都打主表，没验证 trade-off 成本侧 |
| 崩溃恢复 / 数据校验 | ⏳ P1 待办 | 当前进程退出前 Drain，没做 crash-safe |
| 真实 OTel/LangSmith trace 接入 | ⏳ P2 待办 | 设计文档第 13 节 |

图例：✅ 已实现 / ⏳ 待办或进行中

---

## 2. 已实现的能力

### 2.1 数据生成

- `generate_agent_traces.py`：参数化生成 trace/span/event/payloads/operations 五类 JSONL。
- 已稳定生成 10GB（1M ops）和 100GB（8.42M ops）规模数据。

### 2.2 写入路径

- `rocksdb_trace_bench --layout trace_first_with_indexes`：纯 RocksDB baseline，全 value 直写。
- `rocksdb_trace_bench --layout tracelsm_object`：TraceLSM 写路径
  - 扫 trace JSON 中所有 ≥ `inline_payload_threshold` 的 `text` 字段；
  - 通过 `ObjectStore.PutAsync` 异步上传到对象存储；
  - 原 JSON 字段被替换为 `{payload_ref, object_key, backend_uri, payload_state, payload_digest, offloaded_text:true}`；
  - **当前**：在写 RocksDB 之前 `Finalize()` 等待所有 PUT 完成（强一致），是吞吐瓶颈。

### 2.3 ObjectStore 后端

- `LocalFileObjectStore`：开发 / 离线测试用。
- `CosObjectStore`：
  - 腾讯云 COS XML API + 签名 v3（HMAC-SHA1 via OpenSSL）；
  - 默认走内网 endpoint `<bucket>.cos-internal.<region>.tencentcos.cn`（公网已被禁用）；
  - libcurl 复用 + TLS keepalive；
  - 自管 worker pool，`PutAsync()` 入队返回 `std::future<Status>`；
  - 重试内重新计算 Authorization，避免长 backoff 后签名过期。

### 2.4 写入并发流水线（方案 A）

- `--object_concurrency N`：worker 数（每 worker 一个长连接 CURL）。
- `--pipeline_depth D`：主循环预读 D 行 → 全部 `PrepareValueAsync` 提交 → 队头 `Finalize` 后写 RocksDB。
- 在保留 RocksDB 写入顺序的前提下把 PUT 流水化。
- 实测 10GB / 100GB 上稳态 ~750–800 ops/s，瓶颈在 COS 网络。

### 2.5 Benchmark 与运维

- bench summary JSON 字段：`object_puts/object_bytes/object_errors/offloaded_values/offloaded_fields/tracelsm_original_value_bytes/tracelsm_compact_value_bytes/object_concurrency/pipeline_depth` 等。
- Makefile：`bench-tracelsm-object`、`bench-tracelsm-cos`、`bench-rocksdb-vs-tracelsm-*` 等 target；可通过 `TRACELSM_OBJECT_CONCURRENCY` / `TRACELSM_PIPELINE_DEPTH` 调参。
- `tools/clean_bench_artifacts.sh`：分级清理本地中间产物，可选 `--cos` 清 COS 前缀；默认 dry-run。
- 文档：`docs/tracelsm_cpp_mvp_usage.md`、`docs/tracelsm_cos_mvp_usage.md`。

---

## 3. 已经验证过的实验结论

### 3.1 10GB（前 1M ops）

| 指标 | RocksDB（5/23 旧 baseline rerun，含 update 覆盖 bug） | RocksDB（5/26 新 baseline，read-modify-write） | TraceLSM-COS（5/24） |
|---|---|---|---|
| 用时 | 441.83 s | 330.47 s | 1289.96 s |
| 吞吐 ops/s | 2263 | 3026 | 775 |
| User+Sys CPU 时间 | 75 s（17% util） | 68 s（20% util） | — |
| File system inputs（块） | 26,897,464 | 14,295,904 | — |
| Major page faults | 42 | 0 | — |
| 主表 SST | 8.06 GB | 7.58 GB | 5.08 GB |
| query avg µs | 134.8 | 12.2 | 16.3 |
| query p99 µs | 404 | 189 | 193 |
| query_trace_tree avg / p99 µs | 1830 / 43695 | 152 / 435 | — |
| 外置对象 / 字节 | — | — | 25,043 / 1.91 GiB |
| merge_reads / misses / failures | — | 144,177 / 0 / 0 | — |
| merge_read 时长 | — | 1.77 s（占总时长 0.5%） | — |

**关键观察（重要纠偏）**：

- **写入侧合并语义修复带来的差异极小**：`writes / logical_writes / index_writes / index_write_amplification` 在新旧 baseline 上完全相同；`rocksdb_value_bytes` 仅 +0.26%（10.530 GB → 10.558 GB）；`write_time_sec` 仅 +12%（42.15 → 47.35 s，差额来自 144,177 次 merge_read 的合计 ~1.77 s）。
- **query 加速不是合并语义带来的**：每个 query 类型的 `scanned_keys` / `scanned_bytes` 在新旧 baseline 上几乎一致（同样的 work 量），合并只改 record 内容、不改 key 结构、不改索引；query 路径未受影响。
- **query 加速是机器 / page cache 状态差异**：5/23 两次跑都有几十次 major page faults（磁盘随机读），file system inputs 是 5/26 的 1.9–2.2×；5/26 跑时 page cache 命中接近 100%，所以 `query_trace_tree`、`search_text`、`filter_spans` 等"打主表 value" 的 query 加速 10–30×，而只走 prefix-index 的 `query_running_traces` 仅快 1.3×（瓶颈本身就在 CPU 不在 IO）。
- 严格的"修复语义对吞吐/查询的影响"实验需要在同一机器、同一时段、相同 page cache 起点上跑修复前/修复后两个二进制；但当前数据已经足以说明：**修复使写入路径多了一次 Get（开销 0.5%），换来正确性，没有副作用**。
- TraceLSM-COS（5/24）数据未重跑。如果以 5/26 新 baseline 作分母，写吞吐慢比例为 **3026 → 775，慢 3.91×**；但这部分包含了机器/cache 偏差，要拿来和 RocksDB 横向对比同样需要在相同机器/cache 状态下重跑（建议作为 P1 的延伸实验，与 P1-1 一同安排）。

### 3.2 100GB（8.42M ops）

| 指标 | RocksDB (5/23 baseline) | TraceLSM-COS | 差异 |
|---|---|---|---|
| 用时 | 5281.66 s（88 min） | 11154 s（3 h 6 min） | 慢 2.11× |
| 吞吐 ops/s | 1594 | 755 | -52.6% |
| 主表 SST | 60.53 GB | 41.27 GB | -31.8% |
| query avg µs | 154.4 | 41.6 | -73.0% |
| query p99 µs | 285 | 262 | -8.1% |
| 外置对象 / 字节 | — | 211,781 / 16.15 GiB | — |
| CPU 占用 | — | 6%（IO-bound） | — |

**关键观察**：

- TraceLSM-COS 写吞吐被 COS 网络锁在 ~750 ops/s，从 10GB 到 100GB 几乎不变（-2.6%）；RocksDB 同期掉 30%。
- 数据规模越大，两者写吞吐绝对差距在缩小。
- 主表 SST 缩水比例稳定在 ~32%。
- 查询延迟头部（p50/p95）随数据规模变大优势收窄，**长尾（avg/p99）仍领先**。

---

## 4. 待办需求（按优先级）

### P0 —— 阻塞性需求

无。当前原型已经能跑通端到端 100GB benchmark。

### P1 —— 高优先级

#### P1-1：查询回落 COS 路径与长尾验证

**问题**：当前所有 query 路径只读主表，外置 payload 从未被读出，因此 trade-off 的成本侧没被量化。

**需求**：

- 在 bench 里增加一类 query：必须 fetch 完整 `text` 字段（即触发回 COS）。
- 度量：
  - `cos_get_avg_us / p95 / p99`
  - `cos_get_count`
  - 在不同热度（最近 N% 数据 / 全量随机）下的命中率与延迟分布。
- 决策依据：决定是否需要 P3 的 payload cache。

**工作量**：1 天。

#### P1-2：崩溃恢复 / 数据一致性校验

**问题**：当前进程异常退出时，未完成的 PUT 直接丢失（worker pool destructor 不保证 drain）；主表里可能存在指向不存在 COS key 的 ref。

**需求**：

- 进程退出前显式 drain 所有 in-flight PUT。
- 写一个 `verify_tracelsm_db` 工具：扫主表所有 `payload_ref`，对照 COS / LocalFile 验证存在性。
- bench 跑完自动跑 verify，作为正确性门禁。

**工作量**：0.5–1 天。

#### P1-3：`--object_concurrency` 上限验证

**问题**：当前 32，CPU 只占 6%，理论上还能加。但加到多少 COS 服务端会反压未知。

**需求**：

- 10GB 跑 4 / 16 / 32 / 64 / 128 五档，画一条 ops/s vs concurrency 曲线。
- 找出"边际收益归零"的拐点，作为默认值。

**工作量**：0.5 天（脚本化 + 等跑分）。

#### P1-4：✅ baseline `update_trace` / `update_span` 语义修复（已完成 2026-05-26）

**问题**：generator 的 `update_*` 操作只携带 patch 字段（end_ns / status / latency_ms / metrics 等），原 bench 用 `Put(KeyTrace, stored_line)` 把 patch 整段覆盖到 `T#trace_id`，导致只在 `insert_trace` 阶段写下的字段（`session_id` / `user_id` / `metadata` / `parent_span_id` 等）**全部丢失**。`tracelsm_object` layout 受同等影响。

这个 bug 让 baseline 给出一个"其实根本不是完整 trace 记录"的对比基准，正确性站不住，相关 100GB 结论需要在新 baseline 下复核。

**修复（路径 a：read-modify-write）**：

- 在 `update_span` / `update_trace` 写路径里：
  1. `db->Get(KeyTrace/KeySpanByTrace)` 拿到 `stored_old`；
  2. 从 `line` 中抽取顶层 `patch` 对象；
  3. 把 patch 的每个 top-level 字段 replace-or-append 到 stored_old 的 `record:{...}` 内层；
  4. 重写外壳为 `{"op":"insert_(trace|span)","timestamp_ns":<new>,"record":{merged}}`，下游 substring extractor 与索引语义保持不变；
  5. `Put(merged)`。
- 兼容 `tracelsm_object` layout：合并出来的 record 已经是 ref-form（来自 stored_old），`patch` 不含大字段，不再触发 offload。
- 失败兜底：若 `Get` 未命中或 `ApplyPatchToRecord` 解析失败，回退到原来的 `Put(stored_line)`，并计入 `merge_misses` / `merge_failures`，便于审计。
- 新增 metrics（已写入 summary JSON）：`merge_reads / merge_misses / merge_failures / merge_read_ms`。

**正确性验证（smoke profile，26609 ops，2992 updates）**：

- 编译通过、bench 通过：`merge_reads=2992 / merge_misses=0 / merge_failures=0`。
- 抽样 20 条 `update_trace` 后的 `T#trace_id`，全部满足：`session_id ✓ + end_ns ✓ + metadata ✓ + status terminal ✓`。
- 抽样 20 条 `update_span` 后的 `S#trace_id#span_id`，全部满足：`parent_span_id ✓ + end_ns ✓ + kind ✓ + status terminal ✓`。

**后续动作**：

- 重跑 10GB（含 baseline 与 TraceLSM-COS）作为新对比基线（待 P1-1 一同完成）。
- 100GB 是否重跑视带宽预算决定，结论方向不变（合并语义只让 record 变全，不改变写吞吐结构）。

### P2 —— 中等优先级

#### P2-1：异步本地 WAL（写完即返回）★

**背景**：当前写路径同步等 COS PUT 完成，是吞吐瓶颈。希望写本地后立即返回，PUT 异步进行。

**核心思路（保留待定，方案先冻结）**：

```
入: KV(k, v_full)，含 ≥ threshold 的大字段
1) 把大字段 append 到本地 payload log，得到 local_addr
2) 用 ref{state=LOCAL, local_addr=...} 替换大字段，写入 RocksDB 主表
3) 返回 OK ✅（写路径在此结束）

异步 worker：
4) 读 payload log 的 local_addr 段 → PUT COS 得到 cos_uri
5) 成功后写一条 ref{state=COS, cos_uri=..., supersedes=local_addr} 进 RocksDB

后台合并（依赖 P3 semantic compaction，或 RocksDB 自身 compaction）：
6) 把 LOCAL 版本和 COS 版本合并成最终 ref{state=COS, cos_uri=...}
7) 没有引用的 payload log segment 可回收
```

**读路径**：

```
Get(k) → 看 ref.state：
  LOCAL → 读本地 payload log
  COS   → 读对象存储
RocksDB 自带 sequence number 保证最新版本胜出。
```

**关键设计点（实现前再敲定）**：

- `k_meta` 编码：(a) 同 key 新版本 / (b) 影子 key `__cosref__/k`。倾向 (a)，配合 (b) 留给 P3 semantic compaction 扩展。
- payload log 是否承担 WAL 角色？倾向**是**，记 `(k, full_value)` 而不是只记 payload bytes，崩溃后能重放未完成的 RocksDB 写。
- payload log GC：所有指向该 segment 的 PUT 全部成功后整段删。读路径优先 COS，旧 LOCAL ref 在 RocksDB 旧 SST 里属于"待 compaction 的死版本"。
- fsync 策略：先 per-write fsync 跑通，再加 group commit。

**预期吞吐**：

- 去掉 COS 同步等待后，瓶颈链是 `parse → memcpy → fsync(payload_log) → RocksDB Put`。
- 估计能到 **5000–15000 ops/s**，与 RocksDB baseline 持平甚至反超（因为主表 value 更小、compaction 更轻）。

**风险与代价**：

- 多一个 staging 目录要管理（容量、反压、GC）。
- 主表 schema 多 `state` 字段。
- 真正的 crash-safe 恢复逻辑（与 P1-2 配合）。
- 一致性窗口：本地 OK 返回到 COS PUT 完成之间存在脏数据，需明确"本地副本 + COS 副本"的 SLA 语义。

**工作量**：1–2 天（payload log 模块） + 1 天（状态机/回填/恢复） + 0.5 天（GC） + 0.5 天（验证）≈ **3–4 人日**。

**何时启动**：暂不启动，等 P1-1 / P1-2 完成、且确认查询路径下层不会被同步语义假设污染后再做。

#### P2-2：真实 OTel / LangSmith / LangChain trace 接入

**需求**：用真实 trace 数据跑一遍，验证 synthetic 数据的代表性，作为论文实验的支撑。

**工作量**：2–3 天（数据清洗 + 字段映射）。

#### P2-3：`inline_payload_threshold` 自适应

**需求**：当前固定 4096，可以做：

- 跑一组 1024 / 2048 / 4096 / 8192 / 16384 的曲线，输出"主表大小 vs 外置对象数 vs 查询 avg" 的 Pareto 前沿；
- 后续可考虑按字段类型 / 频次自适应（`text` 默认低、`error` 默认高等）。

**工作量**：0.5–1 天。

### P3 —— 长期 / 研究主线

#### P3-1：Semantic Compaction

依赖 P2-1 已经把 LOCAL/COS 状态机跑通后再做。设计文档 §8 已有大致方案。论文主要贡献点之一。

#### P3-2：Hybrid Cache（payload cache）

依赖 P1-1 量化的回 COS 频率与代价。设计文档 §9。

#### P3-3：Hot/Cold 分层 + lifecycle-aware compaction

设计文档 §7 / §13。论文方向之一。

---

## 5. 当前已知问题 / 风险

1. **写吞吐瓶颈在 COS 同步等待**：见 P2-1。
2. **查询路径未覆盖 COS 回拉**：见 P1-1，可能高估 TraceLSM 查询优势。
3. **崩溃恢复未实现**：见 P1-2，目前只在正常退出路径下安全。
4. **5/23 RocksDB baseline 与当前 bench 二进制不同代码版本**：除 `update_trace`/`update_span` 写入语义已修（2026-05-26，见 P1-4）外，其余 trace_first 路径未改。需要重跑一组 10GB / 100GB 同代码 baseline 用作论文对比基线。
5. **CPU 利用率 6%**：见 P1-3，concurrency 还有提升空间，但未量化。

---

## 6. 决策记录（ADR-style 摘要）

- **ADR-001 / 2026-05-23**：先用 RocksDB 包一层做 TraceLSM 原型，不自研 LSM。理由：聚焦 trace-aware 设计点，避免重新发明 LSM。
- **ADR-002 / 2026-05-24**：COS 后端 endpoint 默认走内网 vhost-style。理由：腾讯云 COS 公网默认禁访问，且 VPC 内网 RTT 低 10×。
- **ADR-003 / 2026-05-24**：写并发采用方案 A（HTTP 并发流水线 + worker keepalive），不做"多 payload 打包成大对象"。理由：实现简单，不引入额外的 manifest/解包路径，足以验证瓶颈定位。
- **ADR-004 / 2026-05-25**：异步本地 WAL（P2-1）方案先冻结、思路保留，不立即实施。理由：当前 MVP 已能跑通对比实验；先把 P1 的查询长尾、崩溃恢复、并发拐点等"现况量化"做完，避免在 P2 里返工。
- **ADR-005 / 2026-05-26**：baseline `update_*` 修复采用**路径 a：read-modify-write**（在 record 内合并 patch、外壳保持 `insert_*`），不采用"start 与 end 共存"或"Merge Operator"。
  - 理由：
    1. 合并后 value 形态完全等价于 generator 直接给一份"完整 record"，下游所有 substring 提取、索引、查询路径零改动；
    2. RocksDB 是对比方案，trade-off 应放在被对比方一侧（自研系统）；baseline 的额外 `Get` 是"为了正确性"应付的开销，结构清晰，便于解释；
    3. 不引入 RocksDB Merge Operator 跨 layout 的兼容/冲突分析；
    4. patch 对象是顶层 `patch:{...}`，与 record 内嵌套字段语义解耦，合并实现可以做成纯字符串级（无 JSON 库依赖）。
  - 代价：每个 `update_*` 多一次 RocksDB Get；smoke 上 ~0.7 µs/次（merge_read_ms=2 / 2992 updates），可忽略。`tracelsm_object` 同步生效（合并值天然兼容 ref-form）。
