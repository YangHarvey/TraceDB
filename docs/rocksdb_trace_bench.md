# rocksdb_trace_bench

`rocksdb_trace_bench` replays synthetic Agent trace operations into RocksDB and measures the first trace-first LSM baseline.

## Build

First make sure RocksDB static library exists:

```bash
make rocksdb-static
```

Then build the benchmark runner:

```bash
make
```

Binary:

```text
build/rocksdb_trace_bench
```

## Generate workload

```bash
cd agent-trace-storage-notes/synthetic-agent-trace-generator
python3 generate_agent_traces.py --out ./sample --num-traces 1000 --avg-spans 20 --avg-events-per-span 5 --otel-json
cd /data/workspace/TraceDB
```

## Run

```bash
./build/rocksdb_trace_bench \
  --ops agent-trace-storage-notes/synthetic-agent-trace-generator/sample/operations.jsonl \
  --db bench-results/rocksdb-trace-first-db \
  --destroy_db true \
  --layout trace_first
```

## Current layout

The first version implements `trace_first` layout:

```text
T#{trace_id} -> trace/update record
S#{trace_id}#{span_id} -> span/update record
E#{trace_id}#{timestamp_ns}#{event_id} -> event record
C#{trace_id}#{parent_span_id}#{span_id} -> child ref
R#{start_time}#{trace_id} -> running trace ref
X#{timestamp_ns}#{trace_id}#{span_id} -> error span ref
```

## Measured operation

`query_trace_tree` currently does:

```text
Get T#{trace_id}
Scan S#{trace_id}#*
Scan E#{trace_id}#*    # controlled by --scan_events
```

This gives a baseline for single-trace reconstruction latency and scan amplification.

## Useful options

```text
--ops <path>                  operations.jsonl path
--db <path>                   RocksDB database path
--destroy_db true|false       destroy DB before run
--disable_wal true|false      disable WAL
--sync true|false             sync writes
--scan_events true|false      include events in get_trace_tree
--max_ops <n>                 stop after n operations
--progress_interval <n>       progress log interval
```

## Output

The program prints a JSON summary with:

```text
writes / inserts / updates / appends / queries
end_to_end_ops_per_sec
write_ops_per_sec_in_write_path
scanned_keys
scanned_bytes
query_avg_us / p50 / p95 / p99
rocksdb_estimate_live_data_size
rocksdb_total_sst_files_size
```
