# TraceLSM C++ Object-Backed MVP 使用说明

本 MVP 在 RocksDB 之上增加一层 C++ `TraceLSM` 写入包装层，对 benchmark 输入中的大 inline payload 做对象化：

```text
operation JSON -> TraceLSM payload splitter -> local object store
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
- `--object_root <path>`：本地对象存储目录。
- `--object_key_prefix <prefix>`：对象 key 前缀，默认 `tracelsm`。
- `--inline_payload_threshold <bytes>`：当 JSON 中的 inline `text` 字段解码后大于等于该阈值时外置，默认 `4096`。

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

- 第一版只实现 `LocalFileObjectStore`，不在 C++ 热路径直接接 COS。
- 默认不做 query-time hydration，查询返回 RocksDB 中的 compact value/object ref。
- payload 检测面向当前 synthetic generator 的 JSON 形态，主要外置大 inline `text` 字段。
- 写入顺序是先写对象、再写 RocksDB 引用；如果 RocksDB 写失败，可能留下孤儿对象，后续可通过 manifest/GC 清理。
- 暂不实现 semantic compaction、TraceSegment builder、冷热分层和对象缓存。
