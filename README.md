# traceDB

`traceDB` is an experimental project for studying storage designs for Agent execution traces.

Current focus:

- Synthetic Agent trace workload generation
- LSM/RocksDB baselines for trace/span/event storage
- `get_trace_tree(trace_id)` latency and read amplification
- Trace-aware compaction and hot/cold lifecycle-aware layout

## Notes

Project notes are under:

```text
agent-trace-storage-notes/
```

Key documents:

- `agent-trace-storage-notes/README.md`
- `agent-trace-storage-notes/workloads-and-datasets.md`
- `agent-trace-storage-notes/paper-idea-tracelsm.md`
- `agent-trace-storage-notes/implementation-plan-tracelsm.md`

## Synthetic workload

```bash
cd agent-trace-storage-notes/synthetic-agent-trace-generator
python3 generate_agent_traces.py --out ./sample --num-traces 1000 --avg-spans 20 --avg-events-per-span 5 --otel-json
```

## RocksDB trace benchmark

Build:

```bash
make rocksdb-static
make
```

Run:

```bash
./build/rocksdb_trace_bench \
  --ops agent-trace-storage-notes/synthetic-agent-trace-generator/sample/operations.jsonl \
  --db bench-results/rocksdb-trace-first-db \
  --destroy_db true \
  --layout trace_first
```

See `docs/rocksdb_trace_bench.md` for details.
