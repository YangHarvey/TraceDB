CXX ?= g++
ROCKSDB_DIR ?= third_party/rocksdb
BUILD_DIR ?= build

CXXFLAGS ?= -std=c++20 -O2 -g
CPPFLAGS += -I$(ROCKSDB_DIR)/include -I$(ROCKSDB_DIR)
LDFLAGS += $(ROCKSDB_DIR)/librocksdb.a -lpthread -ldl -lz

.PHONY: all clean rocksdb-static

all: $(BUILD_DIR)/rocksdb_trace_bench

rocksdb-static:
	$(MAKE) -C $(ROCKSDB_DIR) static_lib -j$$(nproc) DISABLE_WARNING_AS_ERROR=1

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/rocksdb_trace_bench: tools/rocksdb_trace_bench.cc $(ROCKSDB_DIR)/librocksdb.a | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $< -o $@ $(LDFLAGS)

clean:
	rm -rf $(BUILD_DIR)
