# TraceLSM C++ Object-Backed MVP 使用说明

本 MVP 在 RocksDB 之上增加一层 C++ `TraceLSM` 写入包装层，对 benchmark 输入中的大 inline payload 做对象化：

```text
operation JSON -> TraceLSM payload splitter -> local object store / Tencent COS
                                      -> compact JSON/object ref -> RocksDB
```

## 构建

```bash
make all
```

`Makefile` 会把以下源文件一起编译进 `build/rocksdb_trace_bench`：

- `tools/rocksdb_trace_bench.cc`
- `tools/tracelsm_object_store.cc`
- `tools/tracelsm_store.cc`

## 直接运行

```bash
python3 agent-trace-storage-notes/synthetic-agent-trace-generator/generate_agent_traces.py \
  --profile smoke \
  --payload-mode inline \
  --out bench-results/smoke-tracelsm-object \
  --otel-json

build/rocksdb_trace_bench \
  --ops bench-results/smoke-tracelsm-object/operations.jsonl \
  --db bench-results/smoke-tracelsm-object/rocksdb-tracelsm-object-db \
  --layout tracelsm_object \
  --object_root bench-results/smoke-tracelsm-object/tracelsm-objectstore \
  --object_key_prefix tracelsm \
  --inline_payload_threshold 4096 \
  > bench-results/smoke-tracelsm-object/rocksdb_tracelsm_object.json
```

也可以使用封装 target：

```bash
make bench-tracelsm-object PROFILE=smoke
```

## 参数

- `--layout tracelsm_object`：启用 C++ TraceLSM object-backed layout。
- `--object_backend local|cos`：对象存储后端，默认 `local`。
- `--object_root <path>`：本地对象存储目录，仅 `local` 后端使用。
- `--object_key_prefix <prefix>`：对象 key 前缀，默认 `tracelsm`。
- `--inline_payload_threshold <bytes>`：当 JSON 中的 inline `text` 字段解码后大于等于该阈值时外置，默认 `4096`。
- `--cos_bucket <bucket>` / `--cos_region <region>` / `--cos_endpoint <host>`：COS 非敏感配置；也可用同名环境变量。
  - 默认 endpoint 是内网域名 `cos-internal.<region>.tencentcos.cn`（要求运行机器与 bucket 在同一腾讯云内网/VPC）。如需走公网，请显式设 `COS_ENDPOINT=<bucket>.cos.<region>.myqcloud.com` 或 `COS_ENDPOINT=cos.<region>.myqcloud.com`。
  - 当 `--cos_endpoint` 是 region-level 域名（如 `cos-internal.ap-beijing.tencentcos.cn`）时，工具会自动前置 `<bucket>.` 形成 vhost-style 主机名。
- `--cos_timeout_sec <sec>` / `--cos_retries <n>`：COS 请求超时和重试次数。

## 腾讯云 COS 热路径使用

C++ 热路径的 COS 后端使用 COS XML API，密钥只从环境变量读取，不要写入命令行、文档或仓库文件：

```bash
export COS_SECRET_ID=...
export COS_SECRET_KEY=...
export COS_BUCKET=mybucket-1250000000
export COS_REGION=ap-beijing

make bench-tracelsm-cos PROFILE=smoke TRACELSM_OBJECT_KEY_PREFIX=tracelsm-cpp-smoke
```

也可以直接运行：

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

`backend_uri` 会写成 `cos://{bucket}/{object_key}`，benchmark summary 只输出 `cos_bucket`、`cos_region`、`cos_endpoint` 等非敏感字段，不输出 `COS_SECRET_KEY` 或 Authorization。

## 对象布局

对象 key 兼容现有 Python MVP 的 payload 路径风格：

```text
{prefix}/payloads/{trace_id}/{span_id}/{payload_kind}/{payload_id}.payload
```

RocksDB 中保留 compact JSON，例如 payload 内的：

```json
{
  "payload_ref": "obj://payloads/...",
  "object_key": "tracelsm/payloads/...payload",
  "backend_uri": "local://...",
  "payload_state": "local",
  "payload_digest": "fnv64:...",
  "offloaded_text": true,
  "bytes": 8192,
  "payload_kind": "completion"
}
```

## 输出指标

`rocksdb_trace_bench` 的 JSON summary 增加了以下字段：

- `rocksdb_value_bytes`：成功写入 RocksDB 的 value 字节总量。
- `object_puts`：对象写入次数。
- `object_bytes`：对象写入总字节数。
- `object_errors`：对象写入错误数。
- `offloaded_values`：发生 payload 外置的 RocksDB value 数。
- `offloaded_fields`：被外置的 `text` 字段数。
- `tracelsm_original_value_bytes`：TraceLSM 处理前的 write value 字节数。
- `tracelsm_compact_value_bytes`：TraceLSM 改写后的 write value 字节数。

## 当前限制

- 已支持 `LocalFileObjectStore` 和 C++ 热路径 `CosObjectStore`；当前 COS 上传是同步写入，尚未实现异步 upload queue。
- 默认不做 query-time hydration，查询返回 RocksDB 中的 compact value/object ref。
- payload 检测面向当前 synthetic generator 的 JSON 形态，主要外置大 inline `text` 字段。
- 写入顺序是先写对象、再写 RocksDB 引用；如果 RocksDB 写失败，可能留下孤儿对象，后续可通过 manifest/GC 清理。
- 暂不实现 semantic compaction、TraceSegment builder、冷热分层和对象缓存。
