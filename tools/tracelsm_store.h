#pragma once

#include <cstdint>
#include <string>

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

class TraceLSMStore {
 public:
  TraceLSMStore(TraceLSMConfig config, ObjectStore* object_store, TraceLSMMetrics* metrics);

  PreparedValue PrepareValue(const std::string& value, uint64_t sequence);

 private:
  TraceLSMConfig config_;
  ObjectStore* object_store_ = nullptr;
  TraceLSMMetrics* metrics_ = nullptr;
};

}  // namespace tracelsm
