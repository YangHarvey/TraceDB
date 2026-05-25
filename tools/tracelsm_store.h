#pragma once

#include <cstdint>
#include <future>
#include <string>
#include <vector>

#include "tracelsm_object_store.h"

namespace tracelsm {

struct TraceLSMConfig {
  uint64_t inline_payload_threshold = 4096;
  std::string object_key_prefix = "tracelsm";
};

struct TraceLSMMetrics {
  uint64_t object_puts = 0;
  uint64_t object_bytes = 0;
  uint64_t object_errors = 0;
  uint64_t offloaded_values = 0;
  uint64_t offloaded_fields = 0;
  uint64_t original_value_bytes = 0;
  uint64_t compact_value_bytes = 0;
};

struct PreparedValue {
  bool ok = true;
  bool offloaded = false;
  std::string value;
  std::string error;
};

// Captures an in-flight PrepareValue: object PUTs have been submitted to the
// async pool but may not yet have completed. Call TraceLSMStore::Finalize to
// block until all PUTs land and obtain the final PreparedValue.
struct PendingPreparedValue {
  PreparedValue prepared;
  std::vector<std::future<Status>> pending;
};

class TraceLSMStore {
 public:
  TraceLSMStore(TraceLSMConfig config, ObjectStore* object_store, TraceLSMMetrics* metrics);

  // Synchronous variant: submits and waits for all PUTs.
  PreparedValue PrepareValue(const std::string& value, uint64_t sequence);

  // Two-phase API for pipelining: submit all object PUTs and return the partially
  // built PreparedValue (with payload_ref already substituted) plus the futures
  // that callers must wait on before committing the value to RocksDB.
  PendingPreparedValue PrepareValueAsync(const std::string& value, uint64_t sequence);

  // Blocks until all PUTs in `pending` land, then promotes it to a PreparedValue
  // (success or first error). On error, prepared.ok is set to false.
  PreparedValue Finalize(PendingPreparedValue pending);

 private:
  TraceLSMConfig config_;
  ObjectStore* object_store_ = nullptr;
  TraceLSMMetrics* metrics_ = nullptr;
};

}  // namespace tracelsm
