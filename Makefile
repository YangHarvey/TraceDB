CXX ?= g++
ROCKSDB_DIR ?= third_party/rocksdb
BUILD_DIR ?= build
BENCH_RESULTS_DIR ?= bench-results
GENERATOR ?= agent-trace-storage-notes/synthetic-agent-trace-generator/generate_agent_traces.py
PROFILE ?= smoke
LAYOUTS ?= trace_first trace_first_with_indexes append_only update_heavy time_first tracelsm_segment tracelsm_object
CLICKHOUSE_DATABASE ?= tracedb_$(PROFILE)
CLICKHOUSE_MAX_QUERIES ?= 5000
CLICKHOUSE_QUERY_TRANSPORT ?= http

OBJECTSTORE_BACKEND ?= local
OBJECTSTORE_LOCAL_ROOT ?= $(BENCH_RESULTS_DIR)/$(PROFILE)/objectstore
OBJECTSTORE_KEY_PREFIX ?= tracelsm
OBJECTSTORE_CONCURRENCY ?= 4
OBJECTSTORE_SAMPLE_LIMIT ?= 0
OBJECTSTORE_VERIFY_LIMIT ?= 200
OBJECTSTORE_VERIFY_STRIDE ?= 1
TRACELSM_OBJECT_ROOT ?= $(BENCH_RESULTS_DIR)/$(PROFILE)-tracelsm-object/tracelsm-objectstore
TRACELSM_OBJECT_KEY_PREFIX ?= tracelsm
TRACELSM_INLINE_PAYLOAD_THRESHOLD ?= 4096

CXXFLAGS ?= -std=c++20 -O2 -g
CPPFLAGS += -I$(ROCKSDB_DIR)/include -I$(ROCKSDB_DIR) -Itools
LDFLAGS += $(ROCKSDB_DIR)/librocksdb.a -lpthread -ldl -lz
BENCH_SRCS := tools/rocksdb_trace_bench.cc tools/tracelsm_object_store.cc tools/tracelsm_store.cc

.PHONY: all clean rocksdb-static gen bench-smoke bench-dev bench-standard bench-write-heavy bench-long-running bench-payload-heavy bench-deep-tree bench-layouts bench-tracelsm-object external-baseline clickhouse-bench object-store-upload object-store-verify object-store-cos-upload object-store-cos-verify

all: $(BUILD_DIR)/rocksdb_trace_bench

rocksdb-static:
	$(MAKE) -C $(ROCKSDB_DIR) static_lib -j$$(nproc) DISABLE_WARNING_AS_ERROR=1

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/rocksdb_trace_bench: $(BENCH_SRCS) $(ROCKSDB_DIR)/librocksdb.a | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(BENCH_SRCS) -o $@ $(LDFLAGS)

gen:
	python3 $(GENERATOR) --profile $(PROFILE) --out $(BENCH_RESULTS_DIR)/$(PROFILE) --otel-json

bench-smoke: all
	python3 $(GENERATOR) --profile smoke --out $(BENCH_RESULTS_DIR)/smoke --otel-json
	$(BUILD_DIR)/rocksdb_trace_bench --ops $(BENCH_RESULTS_DIR)/smoke/operations.jsonl --db $(BENCH_RESULTS_DIR)/smoke/rocksdb-trace-first-indexed-db --layout trace_first_with_indexes > $(BENCH_RESULTS_DIR)/smoke/rocksdb_trace_first_with_indexes.json

bench-dev: all
	python3 $(GENERATOR) --profile dev --out $(BENCH_RESULTS_DIR)/dev --otel-json
	$(BUILD_DIR)/rocksdb_trace_bench --ops $(BENCH_RESULTS_DIR)/dev/operations.jsonl --db $(BENCH_RESULTS_DIR)/dev/rocksdb-trace-first-indexed-db --layout trace_first_with_indexes > $(BENCH_RESULTS_DIR)/dev/rocksdb_trace_first_with_indexes.json

bench-standard: all
	python3 $(GENERATOR) --profile standard --out $(BENCH_RESULTS_DIR)/standard --otel-json
	$(BUILD_DIR)/rocksdb_trace_bench --ops $(BENCH_RESULTS_DIR)/standard/operations.jsonl --db $(BENCH_RESULTS_DIR)/standard/rocksdb-trace-first-indexed-db --layout trace_first_with_indexes > $(BENCH_RESULTS_DIR)/standard/rocksdb_trace_first_with_indexes.json

bench-write-heavy: all
	python3 $(GENERATOR) --profile write_heavy --out $(BENCH_RESULTS_DIR)/write_heavy --otel-json
	$(BUILD_DIR)/rocksdb_trace_bench --ops $(BENCH_RESULTS_DIR)/write_heavy/operations.jsonl --db $(BENCH_RESULTS_DIR)/write_heavy/rocksdb-trace-first-indexed-db --layout trace_first_with_indexes > $(BENCH_RESULTS_DIR)/write_heavy/rocksdb_trace_first_with_indexes.json

bench-long-running: all
	python3 $(GENERATOR) --profile long_running --out $(BENCH_RESULTS_DIR)/long_running --otel-json
	$(BUILD_DIR)/rocksdb_trace_bench --ops $(BENCH_RESULTS_DIR)/long_running/operations.jsonl --db $(BENCH_RESULTS_DIR)/long_running/rocksdb-trace-first-indexed-db --layout trace_first_with_indexes > $(BENCH_RESULTS_DIR)/long_running/rocksdb_trace_first_with_indexes.json

bench-payload-heavy: all
	python3 $(GENERATOR) --profile payload_heavy --out $(BENCH_RESULTS_DIR)/payload_heavy --otel-json
	$(BUILD_DIR)/rocksdb_trace_bench --ops $(BENCH_RESULTS_DIR)/payload_heavy/operations.jsonl --db $(BENCH_RESULTS_DIR)/payload_heavy/rocksdb-trace-first-indexed-db --layout trace_first_with_indexes > $(BENCH_RESULTS_DIR)/payload_heavy/rocksdb_trace_first_with_indexes.json

bench-deep-tree: all
	python3 $(GENERATOR) --profile deep_tree --out $(BENCH_RESULTS_DIR)/deep_tree --otel-json
	$(BUILD_DIR)/rocksdb_trace_bench --ops $(BENCH_RESULTS_DIR)/deep_tree/operations.jsonl --db $(BENCH_RESULTS_DIR)/deep_tree/rocksdb-trace-first-indexed-db --layout trace_first_with_indexes > $(BENCH_RESULTS_DIR)/deep_tree/rocksdb_trace_first_with_indexes.json

bench-layouts: all
	rm -rf $(BENCH_RESULTS_DIR)/$(PROFILE)-layouts
	python3 $(GENERATOR) --profile $(PROFILE) --out $(BENCH_RESULTS_DIR)/$(PROFILE)-layouts --otel-json
	for layout in $(LAYOUTS); do \
	  extra=""; \
	  if [ "$$layout" = "tracelsm_object" ]; then extra="--object_root $(BENCH_RESULTS_DIR)/$(PROFILE)-layouts/tracelsm-objectstore --object_key_prefix $(TRACELSM_OBJECT_KEY_PREFIX) --inline_payload_threshold $(TRACELSM_INLINE_PAYLOAD_THRESHOLD)"; fi; \
	  $(BUILD_DIR)/rocksdb_trace_bench --ops $(BENCH_RESULTS_DIR)/$(PROFILE)-layouts/operations.jsonl --db $(BENCH_RESULTS_DIR)/$(PROFILE)-layouts/rocksdb-$$layout-db --layout $$layout $$extra > $(BENCH_RESULTS_DIR)/$(PROFILE)-layouts/rocksdb_$$layout.json; \
	done

bench-tracelsm-object: all
	rm -rf $(BENCH_RESULTS_DIR)/$(PROFILE)-tracelsm-object
	python3 $(GENERATOR) --profile $(PROFILE) --out $(BENCH_RESULTS_DIR)/$(PROFILE)-tracelsm-object --payload-mode inline --otel-json
	$(BUILD_DIR)/rocksdb_trace_bench \
	  --ops $(BENCH_RESULTS_DIR)/$(PROFILE)-tracelsm-object/operations.jsonl \
	  --db $(BENCH_RESULTS_DIR)/$(PROFILE)-tracelsm-object/rocksdb-tracelsm-object-db \
	  --layout tracelsm_object \
	  --object_root $(TRACELSM_OBJECT_ROOT) \
	  --object_key_prefix $(TRACELSM_OBJECT_KEY_PREFIX) \
	  --inline_payload_threshold $(TRACELSM_INLINE_PAYLOAD_THRESHOLD) \
	  > $(BENCH_RESULTS_DIR)/$(PROFILE)-tracelsm-object/rocksdb_tracelsm_object.json

external-baseline:
	python3 tools/external_baseline_export.py --input $(BENCH_RESULTS_DIR)/$(PROFILE) --out $(BENCH_RESULTS_DIR)/$(PROFILE)/external-baselines

clickhouse-bench:
	python3 tools/clickhouse_trace_bench.py --data-dir $(BENCH_RESULTS_DIR)/$(PROFILE) --database $(CLICKHOUSE_DATABASE) --mode all --reset --max-queries $(CLICKHOUSE_MAX_QUERIES) --query-transport $(CLICKHOUSE_QUERY_TRANSPORT) --out $(BENCH_RESULTS_DIR)/$(PROFILE)/clickhouse_bench.json

object-store-upload:
	python3 tools/tracelsm_object_store.py upload \
	  --backend local \
	  --local-root $(OBJECTSTORE_LOCAL_ROOT) \
	  --input $(BENCH_RESULTS_DIR)/$(PROFILE)/payloads.jsonl \
	  --out-dir $(BENCH_RESULTS_DIR)/$(PROFILE)/object-store \
	  --key-prefix $(OBJECTSTORE_KEY_PREFIX) \
	  --concurrency $(OBJECTSTORE_CONCURRENCY) \
	  --sample-limit $(OBJECTSTORE_SAMPLE_LIMIT)

object-store-verify:
	python3 tools/tracelsm_object_store.py verify \
	  --backend local \
	  --local-root $(OBJECTSTORE_LOCAL_ROOT) \
	  --manifest $(BENCH_RESULTS_DIR)/$(PROFILE)/object-store/object_manifest.jsonl \
	  --sample-limit $(OBJECTSTORE_VERIFY_LIMIT) \
	  --stride $(OBJECTSTORE_VERIFY_STRIDE) \
	  --deep

object-store-cos-upload:
	python3 tools/tracelsm_object_store.py upload \
	  --backend cos \
	  --input $(BENCH_RESULTS_DIR)/$(PROFILE)/payloads.jsonl \
	  --out-dir $(BENCH_RESULTS_DIR)/$(PROFILE)/object-store-cos \
	  --key-prefix $(OBJECTSTORE_KEY_PREFIX) \
	  --concurrency $(OBJECTSTORE_CONCURRENCY) \
	  --sample-limit $(OBJECTSTORE_SAMPLE_LIMIT)

object-store-cos-verify:
	python3 tools/tracelsm_object_store.py verify \
	  --backend cos \
	  --manifest $(BENCH_RESULTS_DIR)/$(PROFILE)/object-store-cos/object_manifest.jsonl \
	  --sample-limit $(OBJECTSTORE_VERIFY_LIMIT) \
	  --stride $(OBJECTSTORE_VERIFY_STRIDE) \
	  --deep

clean:
	rm -rf $(BUILD_DIR)
