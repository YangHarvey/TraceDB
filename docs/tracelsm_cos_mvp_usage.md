# TraceLSM Object Store / COS MVP 使用说明

本文档说明 TraceLSM 第一阶段最小实现 —— Object Storage 抽离 —— 的使用方式。
该阶段只完成「把大 payload 从本地数据集抽到对象存储」，不实现 semantic
compaction、trace segment、查询时自动 hydrate。

## 1. 范围

本 MVP 完成：

- 离线工具从 `payloads.jsonl` 流式读取 payload。
- 把 payload `text` 内容写入：
  - `local` 后端：本机目录，方便复现实验。
  - `cos` 后端：腾讯云对象存储 (COS) XML API。
- C++ 热路径 `tracelsm_object` 也支持 `--object_backend cos`，可在写 RocksDB compact value 前同步上传大 inline payload。
- 离线工具输出 `object_manifest.jsonl` 与 `object_store_summary.json`，作为后续
  TraceSegment / payload hydration / semantic compaction 的稳定输入。
- 支持抽样下载校验 (`verify --deep`) 和 HEAD-only 校验。
- 支持 `--dry-run`、`--sample-limit`、`--concurrency`、自动重试。

本 MVP **不**做：

- 不实现 `SemanticCompactor`。
- 不实现 completed trace segment。
- 不修改 `tools/rocksdb_trace_bench.cc` 的查询路径。
- 不在查询时自动从对象存储拉 payload。
- 不会删除 `payloads.jsonl` 或 RocksDB 行。

## 2. 文件

| 路径 | 说明 |
|---|---|
| `tools/tracelsm_object_store.py` | 工具入口，支持 `upload` / `verify` 两个子命令 |
| `bench-results/<profile>/object-store/object_manifest.jsonl` | 上传后生成的对象 manifest |
| `bench-results/<profile>/object-store/object_store_summary.json` | 本次运行汇总 |

## 3. 对象 Key 布局

工具会把每个 payload 写到一个稳定的、可复现的 object key：

```text
{key_prefix}/payloads/{trace_id}/{span_id}/{payload_kind}/{payload_id}.payload
```

例如：

```text
tracelsm/payloads/trc_xxxx/spn_yyyy/completion/pay_zzzz.payload
```

`key_prefix` 由 `--key-prefix` 或 Makefile 变量 `OBJECTSTORE_KEY_PREFIX`
控制；默认 `tracelsm`。这样后续 `TraceSegment` 与 hydration 路径可以基于同
样的规则反向推导 object key。

## 4. Manifest 格式

`object_manifest.jsonl` 每行一个 JSON：

```json
{
  "payload_ref": "obj://payloads/trc_x/spn_x/completion/pay_x",
  "trace_id": "trc_x",
  "span_id": "spn_x",
  "payload_kind": "completion",
  "object_key": "tracelsm/payloads/trc_x/spn_x/completion/pay_x.payload",
  "backend_uri": "cos://my-bucket-1250000000/tracelsm/payloads/...",
  "backend": "cos",
  "bytes": 65536,
  "declared_bytes": 65536,
  "sha256": "<hex>",
  "storage_state": "remote",
  "uploaded_at_ns": 1790000000000000000
}
```

## 5. 本地后端使用

最常用的开发/复现路径。例：用 smoke profile 跑：

```bash
make bench-smoke
make object-store-upload PROFILE=smoke
make object-store-verify PROFILE=smoke
```

直接调用工具也可以：

```bash
python3 tools/tracelsm_object_store.py upload \
  --backend local \
  --local-root bench-results/smoke/objectstore \
  --input bench-results/smoke/payloads.jsonl \
  --out-dir bench-results/smoke/object-store \
  --key-prefix tracelsm \
  --concurrency 4

python3 tools/tracelsm_object_store.py verify \
  --backend local \
  --local-root bench-results/smoke/objectstore \
  --manifest bench-results/smoke/object-store/object_manifest.jsonl \
  --sample-limit 200 \
  --deep
```

## 6. 腾讯 COS 后端使用

凭证 **绝不写进仓库**，必须通过环境变量传入：

```bash
export COS_SECRET_ID=...
export COS_SECRET_KEY=...
export COS_BUCKET=mybucket-1250000000     # 必须带 appid 后缀
export COS_REGION=ap-shanghai             # 二选一：region 或 endpoint
# 默认会拼成内网域名 cos-internal.<region>.tencentcos.cn（要求与 bucket 在同一腾讯云 VPC）
# 走公网时显式覆盖：
# export COS_ENDPOINT=mybucket-1250000000.cos.ap-shanghai.myqcloud.com
```

试跑（小样本）：

```bash
make object-store-cos-upload \
  PROFILE=smoke \
  OBJECTSTORE_SAMPLE_LIMIT=50 \
  OBJECTSTORE_CONCURRENCY=4

make object-store-cos-verify PROFILE=smoke OBJECTSTORE_VERIFY_LIMIT=20
```

完整跑：

```bash
make object-store-cos-upload PROFILE=standard OBJECTSTORE_CONCURRENCY=8
make object-store-cos-verify PROFILE=standard
```

也可以直接调用：

```bash
python3 tools/tracelsm_object_store.py upload \
  --backend cos \
  --input bench-results/standard/payloads.jsonl \
  --out-dir bench-results/standard/object-store-cos \
  --key-prefix tracelsm \
  --concurrency 8
```

`--dry-run` 不发请求，只计算 key/sha，可以先用它确认 manifest shape。

### C++ 热路径 COS 使用

```bash
export COS_SECRET_ID=...
export COS_SECRET_KEY=...
export COS_BUCKET=mybucket-1250000000
export COS_REGION=ap-beijing

make bench-tracelsm-cos PROFILE=smoke TRACELSM_OBJECT_KEY_PREFIX=tracelsm-cpp-smoke
```

等价直接调用：

```bash
build/rocksdb_trace_bench \
  --ops bench-results/smoke-tracelsm-object/operations.jsonl \
  --db bench-results/smoke-tracelsm-cos/rocksdb-tracelsm-cos-db \
  --layout tracelsm_object \
  --object_backend cos \
  --object_key_prefix tracelsm-cpp-smoke \
  --inline_payload_threshold 4096 \
  > bench-results/smoke-tracelsm-cos/rocksdb_tracelsm_cos.json
```

## 7. 安全注意

- 工具不会把 `COS_SECRET_KEY`、`Authorization` 头、或完整 payload text 写到
  `object_manifest.jsonl`、`object_store_summary.json` 或日志里。
- 如果你拿到内部 Hunyuan 真实 trace 校准 generator，请在导出 payloads 之前
  在数据源侧做脱敏，不要让真实 prompt/completion 直接进入这个上传流程。
- Makefile 不接受任何密钥参数；C++ benchmark 也不接受密钥命令行参数，密钥只能通过环境变量传入。

## 8. 与设计文档的关系

- 本工具实现 `docs/tracelsm_object_storage_design.md` §6.2 中的「大 payload
  写入 -> 本地 cache -> 异步上传 object storage」的对象端部分。
- 本工具实现 `docs/tracelsm_vs_traditional_lsm.md` §5.1「ObjectStore
  abstraction」和 §5.2「Payload splitter」的 externalization 一半；尚未做
  「LSM row 写 payload_ref 元数据」的 RocksDB 端改动。
- `SemanticCompactor`、`TraceSegmentBuilder`、`HybridCache`、查询时
  hydration 全部留待下一阶段。

## 9. 下一步建议

- 在 `tools/rocksdb_trace_bench.cc` 内增加一个 layout，写 span/event 行时
  把 inline payload 替换为 manifest 中的 `payload_ref`，对比 LSM 大小与
  compaction 行为。
- 引入异步 upload queue，让 generator 可以直接驱动上传，而不是事后批处理。
- 实现 `SemanticCompactor`：扫描 completed trace -> 生成 `segments/...`
  对象 -> 写 `G#{trace_id}` manifest。
- 加 `LatencyInjectedObjectStore`，模拟 5ms/20ms/100ms object storage 延迟
  下的查询路径。
