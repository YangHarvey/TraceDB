#include "tracelsm_store.h"

#include <algorithm>
#include <cctype>
#include <future>
#include <iomanip>
#include <sstream>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace tracelsm {
namespace {

struct StringFieldMatch {
  size_t field_begin = 0;
  size_t value_begin = 0;
  size_t value_end = 0;
  std::string decoded;
};

std::string EscapeJson(const std::string& input) {
  std::string out;
  out.reserve(input.size() + 8);
  for (char c : input) {
    switch (c) {
      case '\\': out += "\\\\"; break;
      case '"': out += "\\\""; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default: out += c; break;
    }
  }
  return out;
}

std::string CleanPathPart(std::string value, const std::string& fallback) {
  if (value.empty()) value = fallback;
  for (char& c : value) {
    const unsigned char u = static_cast<unsigned char>(c);
    if (!(std::isalnum(u) || c == '_' || c == '-' || c == '.')) c = '_';
  }
  return value.empty() ? fallback : value;
}

std::string HexU64(uint64_t value) {
  std::ostringstream oss;
  oss << std::hex << std::setw(16) << std::setfill('0') << value;
  return oss.str();
}

uint64_t Fnv1a64(std::string_view data) {
  uint64_t h = 1469598103934665603ull;
  for (unsigned char c : data) {
    h ^= c;
    h *= 1099511628211ull;
  }
  return h;
}

bool DecodeJsonString(const std::string& json, size_t quote_pos, size_t* value_end, std::string* out) {
  if (quote_pos >= json.size() || json[quote_pos] != '"') return false;
  out->clear();
  bool escaped = false;
  for (size_t i = quote_pos + 1; i < json.size(); ++i) {
    const char c = json[i];
    if (escaped) {
      switch (c) {
        case 'n': out->push_back('\n'); break;
        case 'r': out->push_back('\r'); break;
        case 't': out->push_back('\t'); break;
        case 'b': out->push_back('\b'); break;
        case 'f': out->push_back('\f'); break;
        case '\\': out->push_back('\\'); break;
        case '"': out->push_back('"'); break;
        default: out->push_back(c); break;
      }
      escaped = false;
    } else if (c == '\\') {
      escaped = true;
    } else if (c == '"') {
      *value_end = i;
      return true;
    } else {
      out->push_back(c);
    }
  }
  return false;
}

bool ExtractStringField(const std::string& json, const std::string& field, std::string* out) {
  const std::string needle = "\"" + field + "\":";
  const size_t pos = json.find(needle);
  if (pos == std::string::npos) return false;
  size_t i = pos + needle.size();
  while (i < json.size() && json[i] == ' ') ++i;
  if (json.compare(i, 4, "null") == 0) {
    out->clear();
    return true;
  }
  size_t end = 0;
  return DecodeJsonString(json, i, &end, out);
}

std::vector<StringFieldMatch> FindStringFieldMatches(const std::string& json, const std::string& field) {
  std::vector<StringFieldMatch> matches;
  const std::string needle = "\"" + field + "\":";
  size_t search = 0;
  while (true) {
    const size_t pos = json.find(needle, search);
    if (pos == std::string::npos) break;
    size_t i = pos + needle.size();
    while (i < json.size() && json[i] == ' ') ++i;
    size_t end = 0;
    std::string decoded;
    if (DecodeJsonString(json, i, &end, &decoded)) {
      matches.push_back(StringFieldMatch{pos, i + 1, end, std::move(decoded)});
      search = end + 1;
    } else {
      search = pos + needle.size();
    }
  }
  return matches;
}

std::string BuildPayloadRef(const std::string& trace_id, const std::string& span_id, const std::string& kind,
                            const std::string& payload_id) {
  return "obj://payloads/" + trace_id + "/" + span_id + "/" + kind + "/" + payload_id;
}

std::string BuildObjectKey(const std::string& prefix, const std::string& trace_id, const std::string& span_id,
                           const std::string& kind, const std::string& payload_id) {
  std::string clean_prefix = prefix;
  while (!clean_prefix.empty() && clean_prefix.front() == '/') clean_prefix.erase(clean_prefix.begin());
  while (!clean_prefix.empty() && clean_prefix.back() == '/') clean_prefix.pop_back();
  const std::string head = clean_prefix.empty() ? "" : clean_prefix + "/";
  return head + "payloads/" + trace_id + "/" + span_id + "/" + kind + "/" + payload_id + ".payload";
}

}  // namespace

TraceLSMStore::TraceLSMStore(TraceLSMConfig config, ObjectStore* object_store, TraceLSMMetrics* metrics)
    : config_(std::move(config)), object_store_(object_store), metrics_(metrics) {}

PreparedValue TraceLSMStore::PrepareValue(const std::string& value, uint64_t sequence) {
  return Finalize(PrepareValueAsync(value, sequence));
}

PendingPreparedValue TraceLSMStore::PrepareValueAsync(const std::string& value, uint64_t sequence) {
  PendingPreparedValue out;
  PreparedValue& prepared = out.prepared;
  prepared.value = value;
  if (metrics_ != nullptr) metrics_->original_value_bytes += value.size();
  if (object_store_ == nullptr || config_.inline_payload_threshold == 0) {
    if (metrics_ != nullptr) metrics_->compact_value_bytes += prepared.value.size();
    return out;
  }

  std::string trace_id;
  std::string span_id;
  std::string kind;
  ExtractStringField(value, "trace_id", &trace_id);
  ExtractStringField(value, "span_id", &span_id);
  ExtractStringField(value, "payload_kind", &kind);
  if (kind.empty()) ExtractStringField(value, "kind", &kind);
  trace_id = CleanPathPart(trace_id, "unknown_trace");
  span_id = CleanPathPart(span_id, "unknown_span");
  kind = CleanPathPart(kind, "text");

  std::vector<StringFieldMatch> matches = FindStringFieldMatches(value, "text");
  // Iterate from tail to head so earlier indices remain valid after in-place replacement.
  uint64_t ordinal = 0;
  for (auto it = matches.rbegin(); it != matches.rend(); ++it) {
    const StringFieldMatch& match = *it;
    if (match.decoded.size() < config_.inline_payload_threshold) continue;

    const std::string payload_id = "pay_" + HexU64(sequence) + "_" + HexU64(++ordinal) + "_" + HexU64(Fnv1a64(match.decoded));
    const std::string payload_ref = BuildPayloadRef(trace_id, span_id, kind, payload_id);
    const std::string object_key = BuildObjectKey(config_.object_key_prefix, trace_id, span_id, kind, payload_id);

    out.pending.push_back(object_store_->PutAsync(object_key, match.decoded));

    const std::string digest = "fnv64:" + HexU64(Fnv1a64(match.decoded));
    const std::string replacement =
        "\"payload_ref\":\"" + EscapeJson(payload_ref) + "\","
        "\"object_key\":\"" + EscapeJson(object_key) + "\","
        "\"backend_uri\":\"" + EscapeJson(object_store_->Uri(object_key)) + "\","
        "\"payload_state\":\"" + EscapeJson(object_store_->Name()) + "\","
        "\"payload_digest\":\"" + EscapeJson(digest) + "\","
        "\"offloaded_text\":true";
    prepared.value.replace(match.field_begin, match.value_end - match.field_begin + 1, replacement);
    prepared.offloaded = true;
    if (metrics_ != nullptr) {
      ++metrics_->object_puts;
      ++metrics_->offloaded_fields;
      metrics_->object_bytes += match.decoded.size();
    }
  }

  if (prepared.offloaded && metrics_ != nullptr) ++metrics_->offloaded_values;
  if (metrics_ != nullptr) metrics_->compact_value_bytes += prepared.value.size();
  return out;
}

PreparedValue TraceLSMStore::Finalize(PendingPreparedValue pending) {
  PreparedValue prepared = std::move(pending.prepared);
  for (auto& fut : pending.pending) {
    Status s = fut.get();
    if (!s.ok) {
      if (metrics_ != nullptr) ++metrics_->object_errors;
      prepared.ok = false;
      prepared.error = s.message;
    }
  }
  return prepared;
}

}  // namespace tracelsm
