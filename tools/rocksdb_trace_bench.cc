#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "rocksdb/db.h"
#include "rocksdb/options.h"
#include "rocksdb/slice.h"

namespace {

using Clock = std::chrono::steady_clock;

struct Config {
  std::string operations_path;
  std::string db_path = "./bench-results/rocksdb-trace-bench-db";
  std::string layout = "trace_first_with_indexes";
  bool destroy_db = true;
  bool disable_wal = false;
  bool sync = false;
  bool scan_events = true;
  uint64_t max_ops = 0;
  uint64_t progress_interval = 100000;
};

struct ScanResult {
  uint64_t keys = 0;
  uint64_t bytes = 0;
  uint64_t matched_rows = 0;
  uint64_t returned_bytes = 0;
};

struct OpStats {
  uint64_t count = 0;
  uint64_t scanned_keys = 0;
  uint64_t scanned_bytes = 0;
  uint64_t matched_rows = 0;
  uint64_t returned_bytes = 0;
  std::vector<uint64_t> lat_us;
};

struct Metrics {
  uint64_t lines = 0;
  uint64_t writes = 0;
  uint64_t logical_writes = 0;
  uint64_t index_writes = 0;
  uint64_t inserts = 0;
  uint64_t updates = 0;
  uint64_t appends = 0;
  uint64_t queries = 0;
  uint64_t write_errors = 0;
  uint64_t parse_errors = 0;
  uint64_t scanned_keys = 0;
  uint64_t scanned_bytes = 0;
  uint64_t write_ns = 0;
  uint64_t logical_input_bytes = 0;
  std::vector<uint64_t> query_lat_us;
  std::vector<uint64_t> ingest_to_visible_us;
  std::unordered_map<std::string, uint64_t> op_counts;
  std::unordered_map<std::string, OpStats> op_stats;
};

bool StartsWith(const rocksdb::Slice& s, const std::string& prefix) {
  return s.size() >= prefix.size() && std::memcmp(s.data(), prefix.data(), prefix.size()) == 0;
}

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

std::string PaddedU64(uint64_t value) {
  std::ostringstream oss;
  oss << std::setw(20) << std::setfill('0') << value;
  return oss.str();
}

std::string LowerAscii(std::string value) {
  for (char& c : value) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return value;
}

size_t FindFieldValue(const std::string& json, const std::string& field) {
  const std::string needle = "\"" + field + "\":";
  return json.find(needle);
}

bool ExtractStringField(const std::string& json, const std::string& field, std::string* out) {
  const size_t pos = FindFieldValue(json, field);
  if (pos == std::string::npos) return false;
  size_t i = pos + field.size() + 3;
  while (i < json.size() && json[i] == ' ') ++i;
  if (json.compare(i, 4, "null") == 0) {
    out->clear();
    return true;
  }
  if (i >= json.size() || json[i] != '"') return false;
  ++i;
  std::string value;
  bool escaped = false;
  for (; i < json.size(); ++i) {
    const char c = json[i];
    if (escaped) {
      value.push_back(c);
      escaped = false;
    } else if (c == '\\') {
      escaped = true;
    } else if (c == '"') {
      *out = std::move(value);
      return true;
    } else {
      value.push_back(c);
    }
  }
  return false;
}

bool ExtractU64Field(const std::string& json, const std::string& field, uint64_t* out) {
  const size_t pos = FindFieldValue(json, field);
  if (pos == std::string::npos) return false;
  size_t i = pos + field.size() + 3;
  while (i < json.size() && (json[i] == ' ' || json[i] == '"')) ++i;
  uint64_t value = 0;
  bool seen = false;
  for (; i < json.size(); ++i) {
    const char c = json[i];
    if (c < '0' || c > '9') break;
    seen = true;
    value = value * 10 + static_cast<uint64_t>(c - '0');
  }
  if (!seen) return false;
  *out = value;
  return true;
}

bool ContainsStringFieldValue(const std::string& json, const std::string& field, const std::string& expected) {
  std::string value;
  return ExtractStringField(json, field, &value) && value == expected;
}

bool LayoutUsesIndexes(const Config& cfg) {
  return cfg.layout == "trace_first_with_indexes" || cfg.layout == "update_heavy" || cfg.layout == "time_first" ||
         cfg.layout == "tracelsm_segment";
}

bool LayoutAppendOnly(const Config& cfg) {
  return cfg.layout == "append_only";
}

std::string KeyTrace(const std::string& trace_id) { return "T#" + trace_id; }
std::string KeySpanByTrace(const std::string& trace_id, const std::string& span_id) { return "S#" + trace_id + "#" + span_id; }
std::string PrefixSpanByTrace(const std::string& trace_id) { return "S#" + trace_id + "#"; }
std::string KeyEventByTrace(const std::string& trace_id, uint64_t timestamp_ns, const std::string& event_id) {
  return "E#" + trace_id + "#" + PaddedU64(timestamp_ns) + "#" + event_id;
}
std::string PrefixEventByTrace(const std::string& trace_id) { return "E#" + trace_id + "#"; }
std::string KeyChildByParent(const std::string& trace_id, const std::string& parent_span_id, const std::string& span_id) {
  return "C#" + trace_id + "#" + parent_span_id + "#" + span_id;
}
std::string KeyRunningTrace(uint64_t start_ns, const std::string& trace_id) { return "R#" + PaddedU64(start_ns) + "#" + trace_id; }
std::string KeyErrorSpan(uint64_t timestamp_ns, const std::string& trace_id, const std::string& span_id) {
  return "X#" + PaddedU64(timestamp_ns) + "#" + trace_id + "#" + span_id;
}
std::string KeySpanId(const std::string& span_id) { return "B#" + span_id; }
std::string KeyKindIndex(const std::string& kind, uint64_t ts, const std::string& trace_id, const std::string& span_id) {
  return "K#" + kind + "#" + PaddedU64(ts) + "#" + trace_id + "#" + span_id;
}
std::string PrefixKindIndex(const std::string& kind) { return "K#" + kind + "#"; }
std::string KeyModelIndex(const std::string& model, uint64_t ts, const std::string& trace_id, const std::string& span_id) {
  return "M#" + model + "#" + PaddedU64(ts) + "#" + trace_id + "#" + span_id;
}
std::string PrefixModelIndex(const std::string& model) { return "M#" + model + "#"; }
std::string KeyToolIndex(const std::string& tool, uint64_t ts, const std::string& trace_id, const std::string& span_id) {
  return "U#" + tool + "#" + PaddedU64(ts) + "#" + trace_id + "#" + span_id;
}
std::string PrefixToolIndex(const std::string& tool) { return "U#" + tool + "#"; }
std::string LatencyBucket(uint64_t latency_ms) {
  if (latency_ms < 100) return "000_lt100";
  if (latency_ms < 1000) return "001_100_999";
  if (latency_ms < 5000) return "002_1s_5s";
  if (latency_ms < 30000) return "003_5s_30s";
  return "004_30s_plus";
}
std::string KeyLatencyIndex(const std::string& kind, uint64_t latency_ms, uint64_t ts, const std::string& trace_id,
                            const std::string& span_id) {
  return "L#" + kind + "#" + LatencyBucket(latency_ms) + "#" + PaddedU64(ts) + "#" + trace_id + "#" + span_id;
}
std::string PrefixLatencyKindIndex(const std::string& kind) { return "L#" + kind + "#"; }
std::string KeyThreadIndex(const std::string& thread_id, uint64_t turn, const std::string& trace_id) {
  return "D#" + thread_id + "#" + PaddedU64(turn) + "#" + trace_id;
}
std::string PrefixThreadIndex(const std::string& thread_id) { return "D#" + thread_id + "#"; }
std::string KeySessionIndex(const std::string& session_id, uint64_t start_ns, const std::string& trace_id) {
  return "S2#" + session_id + "#" + PaddedU64(start_ns) + "#" + trace_id;
}
std::string PrefixSessionIndex(const std::string& session_id) { return "S2#" + session_id + "#"; }
std::string KeyWordIndex(const std::string& token, uint64_t ts, const std::string& trace_id, const std::string& span_id,
                         const std::string& event_id) {
  return "W#" + LowerAscii(token) + "#" + PaddedU64(ts) + "#" + trace_id + "#" + span_id + "#" + event_id;
}
std::string PrefixWordIndex(const std::string& token) { return "W#" + LowerAscii(token) + "#"; }
std::string KeyAppendLog(uint64_t ts, uint64_t seq) { return "O#" + PaddedU64(ts) + "#" + PaddedU64(seq); }
std::string KeyTimeFirst(uint64_t ts, uint64_t seq) { return "Z#" + PaddedU64(ts) + "#" + PaddedU64(seq); }
std::string KeyTraceSegment(const std::string& trace_id) { return "G#" + trace_id; }

bool Put(rocksdb::DB* db, const rocksdb::WriteOptions& write_options, const std::string& key, const std::string& value,
         Metrics* metrics, bool index_write = false) {
  const auto begin = Clock::now();
  rocksdb::Status s = db->Put(write_options, key, value);
  const auto end = Clock::now();
  metrics->write_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(end - begin).count();
  if (!s.ok()) {
    ++metrics->write_errors;
    std::cerr << "write error: " << s.ToString() << "\n";
    return false;
  }
  ++metrics->writes;
  if (index_write) ++metrics->index_writes;
  return true;
}

void DeleteKey(rocksdb::DB* db, const rocksdb::WriteOptions& write_options, const std::string& key, Metrics* metrics) {
  const auto begin = Clock::now();
  rocksdb::Status s = db->Delete(write_options, key);
  const auto end = Clock::now();
  metrics->write_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(end - begin).count();
  if (!s.ok()) ++metrics->write_errors;
}

std::vector<std::string> TokensFromText(const std::string& text, size_t max_tokens = 16) {
  std::vector<std::string> tokens;
  std::unordered_set<std::string> seen;
  std::string cur;
  auto flush = [&]() {
    if (cur.size() >= 3 && cur.size() <= 32 && !seen.count(cur)) {
      seen.insert(cur);
      tokens.push_back(cur);
    }
    cur.clear();
  };
  for (char ch : text) {
    unsigned char c = static_cast<unsigned char>(ch);
    if (std::isalnum(c) || ch == '_') {
      cur.push_back(static_cast<char>(std::tolower(c)));
    } else {
      flush();
      if (tokens.size() >= max_tokens) return tokens;
    }
  }
  flush();
  if (tokens.size() > max_tokens) tokens.resize(max_tokens);
  return tokens;
}

void IndexTextFields(rocksdb::DB* db, const rocksdb::WriteOptions& write_options, const std::string& line, uint64_t ts,
                     const std::string& trace_id, const std::string& span_id, const std::string& event_id, Metrics* metrics) {
  std::vector<std::string> fields = {"name", "text", "message", "error_type", "tool_name", "event_type", "query"};
  std::unordered_set<std::string> emitted;
  for (const std::string& field : fields) {
    std::string value;
    if (!ExtractStringField(line, field, &value) || value.empty()) continue;
    for (const std::string& token : TokensFromText(value, 8)) {
      if (emitted.insert(token).second) {
        Put(db, write_options, KeyWordIndex(token, ts, trace_id, span_id, event_id), "", metrics, true);
      }
    }
  }
}

void IndexTrace(rocksdb::DB* db, const rocksdb::WriteOptions& write_options, const std::string& line,
                const std::string& trace_id, uint64_t start_ns, Metrics* metrics) {
  std::string thread_id;
  std::string session_id;
  uint64_t turn = 0;
  if (ExtractStringField(line, "thread_id", &thread_id)) {
    ExtractU64Field(line, "conversation_turn", &turn);
    Put(db, write_options, KeyThreadIndex(thread_id, turn, trace_id), "", metrics, true);
  }
  if (ExtractStringField(line, "session_id", &session_id)) {
    Put(db, write_options, KeySessionIndex(session_id, start_ns, trace_id), "", metrics, true);
  }
}

void IndexSpan(rocksdb::DB* db, const rocksdb::WriteOptions& write_options, const std::string& line,
               const std::string& trace_id, const std::string& span_id, uint64_t ts, Metrics* metrics) {
  std::string kind;
  std::string model;
  std::string tool;
  uint64_t latency_ms = 0;
  Put(db, write_options, KeySpanId(span_id), trace_id, metrics, true);
  if (ExtractStringField(line, "kind", &kind) && !kind.empty()) {
    Put(db, write_options, KeyKindIndex(kind, ts, trace_id, span_id), "", metrics, true);
    if (ExtractU64Field(line, "latency_ms", &latency_ms)) {
      Put(db, write_options, KeyLatencyIndex(kind, latency_ms, ts, trace_id, span_id), "", metrics, true);
    }
  }
  if (ExtractStringField(line, "model", &model) && !model.empty()) {
    Put(db, write_options, KeyModelIndex(model, ts, trace_id, span_id), "", metrics, true);
  }
  if (ExtractStringField(line, "tool_name", &tool) && !tool.empty()) {
    Put(db, write_options, KeyToolIndex(tool, ts, trace_id, span_id), "", metrics, true);
  }
  IndexTextFields(db, write_options, line, ts, trace_id, span_id, "", metrics);
}

ScanResult ScanPrefix(rocksdb::DB* db, const rocksdb::ReadOptions& read_options, const std::string& prefix,
                      uint64_t limit = 0, const std::string& contains = "") {
  ScanResult result;
  std::unique_ptr<rocksdb::Iterator> it(db->NewIterator(read_options));
  for (it->Seek(prefix); it->Valid() && StartsWith(it->key(), prefix); it->Next()) {
    ++result.keys;
    result.bytes += it->key().size() + it->value().size();
    const bool matched = contains.empty() || it->value().ToString().find(contains) != std::string::npos ||
                         it->key().ToString().find(contains) != std::string::npos;
    if (matched) {
      ++result.matched_rows;
      result.returned_bytes += it->value().size();
      if (limit != 0 && result.matched_rows >= limit) break;
    }
  }
  return result;
}

void RecordOp(Metrics* metrics, const std::string& op, uint64_t latency_us, const ScanResult& scan,
              uint64_t returned_bytes = 0, bool freshness = false) {
  OpStats& stats = metrics->op_stats[op];
  ++stats.count;
  stats.lat_us.push_back(latency_us);
  stats.scanned_keys += scan.keys;
  stats.scanned_bytes += scan.bytes;
  stats.matched_rows += scan.matched_rows;
  stats.returned_bytes += scan.returned_bytes + returned_bytes;
  metrics->query_lat_us.push_back(latency_us);
  metrics->scanned_keys += scan.keys;
  metrics->scanned_bytes += scan.bytes;
  ++metrics->queries;
  if (freshness) metrics->ingest_to_visible_us.push_back(latency_us);
}

bool QueryTraceTree(rocksdb::DB* db, const rocksdb::ReadOptions& read_options, const Config& cfg,
                    const std::string& trace_id, const std::string& op_name, Metrics* metrics) {
  const auto begin = Clock::now();
  ScanResult total;
  uint64_t returned = 0;
  if (LayoutAppendOnly(cfg)) {
    total = ScanPrefix(db, read_options, "O#", 0, trace_id);
  } else {
    std::string value;
    db->Get(read_options, KeyTrace(trace_id), &value).PermitUncheckedError();
    returned += value.size();
    ScanResult spans = ScanPrefix(db, read_options, PrefixSpanByTrace(trace_id));
    total.keys += spans.keys;
    total.bytes += spans.bytes;
    total.matched_rows += spans.matched_rows;
    total.returned_bytes += spans.returned_bytes;
    if (cfg.scan_events) {
      ScanResult events = ScanPrefix(db, read_options, PrefixEventByTrace(trace_id));
      total.keys += events.keys;
      total.bytes += events.bytes;
      total.matched_rows += events.matched_rows;
      total.returned_bytes += events.returned_bytes;
    }
  }
  const auto end = Clock::now();
  RecordOp(metrics, op_name, static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count()), total, returned);
  return true;
}

void QueryGetSpan(rocksdb::DB* db, const rocksdb::ReadOptions& read_options, const Config& cfg, const std::string& line,
                  Metrics* metrics, bool freshness = false) {
  const auto begin = Clock::now();
  std::string trace_id;
  std::string span_id;
  std::string value;
  ScanResult scan;
  uint64_t returned = 0;
  ExtractStringField(line, "trace_id", &trace_id);
  ExtractStringField(line, "span_id", &span_id);
  if (LayoutAppendOnly(cfg)) {
    scan = ScanPrefix(db, read_options, "O#", 1, span_id);
  } else if (!trace_id.empty()) {
    db->Get(read_options, KeySpanByTrace(trace_id, span_id), &value).PermitUncheckedError();
    returned = value.size();
  } else if (LayoutUsesIndexes(cfg)) {
    db->Get(read_options, KeySpanId(span_id), &trace_id).PermitUncheckedError();
    db->Get(read_options, KeySpanByTrace(trace_id, span_id), &value).PermitUncheckedError();
    returned = value.size();
  } else {
    scan = ScanPrefix(db, read_options, "S#", 1, span_id);
  }
  const auto end = Clock::now();
  RecordOp(metrics, freshness ? "freshness_probe" : "get_span",
           static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count()), scan, returned,
           freshness);
}

void QueryFilterSpans(rocksdb::DB* db, const rocksdb::ReadOptions& read_options, const Config& cfg, const std::string& line,
                      Metrics* metrics) {
  const auto begin = Clock::now();
  uint64_t limit = 100;
  ExtractU64Field(line, "limit", &limit);
  std::string kind;
  std::string model;
  std::string tool;
  std::string status;
  ExtractStringField(line, "kind", &kind);
  ExtractStringField(line, "model", &model);
  ExtractStringField(line, "tool_name", &tool);
  ExtractStringField(line, "status", &status);

  std::string prefix = "S#";
  std::string contains;
  if (LayoutAppendOnly(cfg)) {
    prefix = "O#";
    contains = !model.empty() ? model : !tool.empty() ? tool : !kind.empty() ? "\"kind\":\"" + kind + "\"" : status;
  } else if (LayoutUsesIndexes(cfg)) {
    if (!model.empty()) prefix = PrefixModelIndex(model);
    else if (!tool.empty()) prefix = PrefixToolIndex(tool);
    else if (status == "error") prefix = "X#";
    else if (!kind.empty() && line.find("min_latency_ms") != std::string::npos) prefix = PrefixLatencyKindIndex(kind);
    else if (!kind.empty()) prefix = PrefixKindIndex(kind);
    else prefix = "K#";
  } else {
    contains = !model.empty() ? model : !tool.empty() ? tool : !kind.empty() ? "\"kind\":\"" + kind + "\"" : status;
  }
  ScanResult result = ScanPrefix(db, read_options, prefix, limit, contains);
  const auto end = Clock::now();
  RecordOp(metrics, "filter_spans", static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count()), result);
}

void QuerySearchText(rocksdb::DB* db, const rocksdb::ReadOptions& read_options, const Config& cfg, const std::string& line,
                     Metrics* metrics) {
  const auto begin = Clock::now();
  uint64_t limit = 100;
  std::string query;
  ExtractU64Field(line, "limit", &limit);
  ExtractStringField(line, "query", &query);
  query = LowerAscii(query);
  ScanResult result;
  if (LayoutUsesIndexes(cfg)) {
    result = ScanPrefix(db, read_options, PrefixWordIndex(query), limit);
  } else if (LayoutAppendOnly(cfg)) {
    result = ScanPrefix(db, read_options, "O#", limit, query);
  } else {
    ScanResult spans = ScanPrefix(db, read_options, "S#", limit, query);
    ScanResult events = ScanPrefix(db, read_options, "E#", limit, query);
    result.keys = spans.keys + events.keys;
    result.bytes = spans.bytes + events.bytes;
    result.matched_rows = spans.matched_rows + events.matched_rows;
    result.returned_bytes = spans.returned_bytes + events.returned_bytes;
  }
  const auto end = Clock::now();
  RecordOp(metrics, "search_text", static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count()), result);
}

void QueryPrefixOp(rocksdb::DB* db, const rocksdb::ReadOptions& read_options, const std::string& op_name,
                   const std::string& prefix, uint64_t limit, Metrics* metrics, const std::string& contains = "") {
  const auto begin = Clock::now();
  ScanResult result = ScanPrefix(db, read_options, prefix, limit, contains);
  const auto end = Clock::now();
  RecordOp(metrics, op_name, static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count()), result);
}

uint64_t Percentile(std::vector<uint64_t> values, double p) {
  if (values.empty()) return 0;
  std::sort(values.begin(), values.end());
  const double idx = (p / 100.0) * static_cast<double>(values.size() - 1);
  return values[static_cast<size_t>(idx + 0.5)];
}

uint64_t Sum(const std::vector<uint64_t>& values) {
  uint64_t sum = 0;
  for (uint64_t v : values) sum += v;
  return sum;
}

void PrintUsage(const char* argv0) {
  std::cerr
      << "Usage: " << argv0 << " --ops <operations.jsonl> [options]\n\n"
      << "Options:\n"
      << "  --db <path>                  RocksDB path, default ./bench-results/rocksdb-trace-bench-db\n"
      << "  --layout <layout>            trace_first|trace_first_with_indexes|append_only|update_heavy|time_first|tracelsm_segment\n"
      << "  --destroy_db true|false      Destroy DB before run, default true\n"
      << "  --disable_wal true|false     Disable RocksDB WAL, default false\n"
      << "  --sync true|false            Sync writes, default false\n"
      << "  --scan_events true|false     Include event scan in query_trace_tree, default true\n"
      << "  --max_ops <n>                Stop after n operations, default unlimited\n"
      << "  --progress_interval <n>      Progress log interval, default 100000\n";
}

bool ParseBool(const std::string& v) { return v == "true" || v == "1" || v == "yes" || v == "on"; }

bool ParseArgs(int argc, char** argv, Config* cfg) {
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    auto need_value = [&](const std::string& name) -> std::string {
      if (i + 1 >= argc) {
        std::cerr << "missing value for " << name << "\n";
        std::exit(2);
      }
      return argv[++i];
    };
    if (arg == "--ops") cfg->operations_path = need_value(arg);
    else if (arg == "--db") cfg->db_path = need_value(arg);
    else if (arg == "--layout") cfg->layout = need_value(arg);
    else if (arg == "--destroy_db") cfg->destroy_db = ParseBool(need_value(arg));
    else if (arg == "--disable_wal") cfg->disable_wal = ParseBool(need_value(arg));
    else if (arg == "--sync") cfg->sync = ParseBool(need_value(arg));
    else if (arg == "--scan_events") cfg->scan_events = ParseBool(need_value(arg));
    else if (arg == "--max_ops") cfg->max_ops = std::stoull(need_value(arg));
    else if (arg == "--progress_interval") cfg->progress_interval = std::stoull(need_value(arg));
    else if (arg == "--help" || arg == "-h") return false;
    else {
      std::cerr << "unknown argument: " << arg << "\n";
      return false;
    }
  }
  if (cfg->operations_path.empty()) {
    std::cerr << "--ops is required\n";
    return false;
  }
  static const std::unordered_set<std::string> layouts = {"trace_first", "trace_first_with_indexes", "append_only",
                                                          "update_heavy", "time_first", "tracelsm_segment"};
  if (!layouts.count(cfg->layout)) {
    std::cerr << "unsupported layout: " << cfg->layout << "\n";
    return false;
  }
  return true;
}

void PrintOpStatsJson(const Metrics& metrics) {
  std::map<std::string, OpStats> ordered(metrics.op_stats.begin(), metrics.op_stats.end());
  std::cout << "  \"ops\": {";
  bool first = true;
  for (const auto& item : ordered) {
    const OpStats& s = item.second;
    const uint64_t sum = Sum(s.lat_us);
    const double avg = s.lat_us.empty() ? 0.0 : static_cast<double>(sum) / static_cast<double>(s.lat_us.size());
    const double read_amp = s.returned_bytes == 0 ? 0.0 : static_cast<double>(s.scanned_bytes) / static_cast<double>(s.returned_bytes);
    if (!first) std::cout << ",";
    first = false;
    std::cout << "\n    \"" << EscapeJson(item.first) << "\": {"
              << "\"count\": " << s.count << ", "
              << "\"avg_us\": " << avg << ", "
              << "\"p50_us\": " << Percentile(s.lat_us, 50) << ", "
              << "\"p95_us\": " << Percentile(s.lat_us, 95) << ", "
              << "\"p99_us\": " << Percentile(s.lat_us, 99) << ", "
              << "\"scanned_keys\": " << s.scanned_keys << ", "
              << "\"scanned_bytes\": " << s.scanned_bytes << ", "
              << "\"matched_rows\": " << s.matched_rows << ", "
              << "\"returned_bytes\": " << s.returned_bytes << ", "
              << "\"read_amplification\": " << read_amp << "}";
  }
  std::cout << "\n  },\n";
}

void PrintOpCountsJson(const Metrics& metrics) {
  std::map<std::string, uint64_t> ordered(metrics.op_counts.begin(), metrics.op_counts.end());
  std::cout << "  \"op_counts\": {";
  bool first = true;
  for (const auto& item : ordered) {
    if (!first) std::cout << ",";
    first = false;
    std::cout << "\n    \"" << EscapeJson(item.first) << "\": " << item.second;
  }
  std::cout << "\n  },\n";
}

void PrintSummary(const Config& cfg, const Metrics& metrics, double elapsed_sec, rocksdb::DB* db) {
  uint64_t live_data_size = 0;
  uint64_t total_sst_size = 0;
  db->GetIntProperty("rocksdb.estimate-live-data-size", &live_data_size);
  db->GetIntProperty("rocksdb.total-sst-files-size", &total_sst_size);

  const uint64_t query_sum = Sum(metrics.query_lat_us);
  const double avg_query_us = metrics.query_lat_us.empty() ? 0.0 : static_cast<double>(query_sum) / static_cast<double>(metrics.query_lat_us.size());
  const double write_sec = static_cast<double>(metrics.write_ns) / 1e9;
  const double write_ops_per_sec = write_sec > 0.0 ? static_cast<double>(metrics.writes) / write_sec : 0.0;
  const double end_to_end_ops_per_sec = elapsed_sec > 0.0 ? static_cast<double>(metrics.lines) / elapsed_sec : 0.0;
  const double index_write_amp = metrics.logical_writes == 0 ? 0.0 : static_cast<double>(metrics.index_writes) / static_cast<double>(metrics.logical_writes);
  const double storage_amp = metrics.logical_input_bytes == 0 ? 0.0 : static_cast<double>(total_sst_size) / static_cast<double>(metrics.logical_input_bytes);

  std::cout << "{\n"
            << "  \"operations_path\": \"" << EscapeJson(cfg.operations_path) << "\",\n"
            << "  \"db_path\": \"" << EscapeJson(cfg.db_path) << "\",\n"
            << "  \"layout\": \"" << cfg.layout << "\",\n"
            << "  \"lines\": " << metrics.lines << ",\n"
            << "  \"writes\": " << metrics.writes << ",\n"
            << "  \"logical_writes\": " << metrics.logical_writes << ",\n"
            << "  \"index_writes\": " << metrics.index_writes << ",\n"
            << "  \"index_write_amplification\": " << index_write_amp << ",\n"
            << "  \"logical_input_bytes\": " << metrics.logical_input_bytes << ",\n"
            << "  \"inserts\": " << metrics.inserts << ",\n"
            << "  \"updates\": " << metrics.updates << ",\n"
            << "  \"appends\": " << metrics.appends << ",\n"
            << "  \"queries\": " << metrics.queries << ",\n"
            << "  \"parse_errors\": " << metrics.parse_errors << ",\n"
            << "  \"write_errors\": " << metrics.write_errors << ",\n"
            << "  \"elapsed_sec\": " << elapsed_sec << ",\n"
            << "  \"end_to_end_ops_per_sec\": " << end_to_end_ops_per_sec << ",\n"
            << "  \"write_time_sec\": " << write_sec << ",\n"
            << "  \"write_ops_per_sec_in_write_path\": " << write_ops_per_sec << ",\n"
            << "  \"scanned_keys\": " << metrics.scanned_keys << ",\n"
            << "  \"scanned_bytes\": " << metrics.scanned_bytes << ",\n"
            << "  \"query_avg_us\": " << avg_query_us << ",\n"
            << "  \"query_p50_us\": " << Percentile(metrics.query_lat_us, 50) << ",\n"
            << "  \"query_p95_us\": " << Percentile(metrics.query_lat_us, 95) << ",\n"
            << "  \"query_p99_us\": " << Percentile(metrics.query_lat_us, 99) << ",\n"
            << "  \"ingest_to_visible_p50_us\": " << Percentile(metrics.ingest_to_visible_us, 50) << ",\n"
            << "  \"ingest_to_visible_p95_us\": " << Percentile(metrics.ingest_to_visible_us, 95) << ",\n"
            << "  \"ingest_to_visible_p99_us\": " << Percentile(metrics.ingest_to_visible_us, 99) << ",\n";
  PrintOpCountsJson(metrics);
  PrintOpStatsJson(metrics);
  std::cout << "  \"rocksdb_estimate_live_data_size\": " << live_data_size << ",\n"
            << "  \"rocksdb_total_sst_files_size\": " << total_sst_size << ",\n"
            << "  \"storage_amplification\": " << storage_amp << "\n"
            << "}\n";
}

}  // namespace

int main(int argc, char** argv) {
  Config cfg;
  if (!ParseArgs(argc, argv, &cfg)) {
    PrintUsage(argv[0]);
    return 2;
  }

  const std::filesystem::path db_parent = std::filesystem::path(cfg.db_path).parent_path();
  if (!db_parent.empty()) std::filesystem::create_directories(db_parent);

  rocksdb::Options options;
  options.create_if_missing = true;
  options.IncreaseParallelism();
  options.OptimizeLevelStyleCompaction();

  if (cfg.destroy_db) rocksdb::DestroyDB(cfg.db_path, options).PermitUncheckedError();

  std::unique_ptr<rocksdb::DB> db;
  rocksdb::Status s = rocksdb::DB::Open(options, cfg.db_path, &db);
  if (!s.ok()) {
    std::cerr << "failed to open RocksDB: " << s.ToString() << "\n";
    return 1;
  }

  rocksdb::WriteOptions write_options;
  write_options.disableWAL = cfg.disable_wal;
  write_options.sync = cfg.sync;
  rocksdb::ReadOptions read_options;

  std::ifstream input(cfg.operations_path);
  if (!input) {
    std::cerr << "failed to open operations file: " << cfg.operations_path << "\n";
    return 1;
  }

  Metrics metrics;
  std::string line;
  const auto run_begin = Clock::now();
  while (std::getline(input, line)) {
    if (line.empty()) continue;
    if (cfg.max_ops != 0 && metrics.lines >= cfg.max_ops) break;
    ++metrics.lines;

    std::string op;
    if (!ExtractStringField(line, "op", &op)) {
      ++metrics.parse_errors;
      continue;
    }
    ++metrics.op_counts[op];

    uint64_t timestamp_ns = 0;
    ExtractU64Field(line, "timestamp_ns", &timestamp_ns);

    const bool write_op = op == "insert_trace" || op == "insert_span" || op == "append_event" || op == "update_span" || op == "update_trace";
    if (write_op) {
      ++metrics.logical_writes;
      metrics.logical_input_bytes += line.size();
      if (LayoutAppendOnly(cfg)) {
        Put(db.get(), write_options, KeyAppendLog(timestamp_ns, metrics.lines), line, &metrics, false);
      } else if (cfg.layout == "time_first") {
        Put(db.get(), write_options, KeyTimeFirst(timestamp_ns, metrics.lines), line, &metrics, true);
      }
    }

    if (op == "insert_trace") {
      std::string trace_id;
      uint64_t start_ns = 0;
      if (!ExtractStringField(line, "trace_id", &trace_id)) {
        ++metrics.parse_errors;
        continue;
      }
      ExtractU64Field(line, "start_ns", &start_ns);
      if (!LayoutAppendOnly(cfg)) {
        Put(db.get(), write_options, KeyTrace(trace_id), line, &metrics);
        Put(db.get(), write_options, KeyRunningTrace(start_ns, trace_id), "", &metrics, true);
        if (LayoutUsesIndexes(cfg)) IndexTrace(db.get(), write_options, line, trace_id, start_ns, &metrics);
      }
      ++metrics.inserts;
    } else if (op == "insert_span") {
      std::string trace_id;
      std::string span_id;
      std::string parent_span_id;
      if (!ExtractStringField(line, "trace_id", &trace_id) || !ExtractStringField(line, "span_id", &span_id)) {
        ++metrics.parse_errors;
        continue;
      }
      if (!LayoutAppendOnly(cfg)) {
        Put(db.get(), write_options, KeySpanByTrace(trace_id, span_id), line, &metrics);
        if (ExtractStringField(line, "parent_span_id", &parent_span_id) && !parent_span_id.empty()) {
          Put(db.get(), write_options, KeyChildByParent(trace_id, parent_span_id, span_id), "", &metrics, true);
        }
        if (LayoutUsesIndexes(cfg)) IndexSpan(db.get(), write_options, line, trace_id, span_id, timestamp_ns, &metrics);
      }
      ++metrics.inserts;
    } else if (op == "append_event") {
      std::string trace_id;
      std::string span_id;
      std::string event_id;
      if (!ExtractStringField(line, "trace_id", &trace_id) || !ExtractStringField(line, "event_id", &event_id)) {
        ++metrics.parse_errors;
        continue;
      }
      ExtractStringField(line, "span_id", &span_id);
      if (!LayoutAppendOnly(cfg)) {
        Put(db.get(), write_options, KeyEventByTrace(trace_id, timestamp_ns, event_id), line, &metrics);
        if (LayoutUsesIndexes(cfg)) IndexTextFields(db.get(), write_options, line, timestamp_ns, trace_id, span_id, event_id, &metrics);
      }
      ++metrics.appends;
    } else if (op == "update_span") {
      std::string trace_id;
      std::string span_id;
      if (!ExtractStringField(line, "trace_id", &trace_id) || !ExtractStringField(line, "span_id", &span_id)) {
        ++metrics.parse_errors;
        continue;
      }
      if (!LayoutAppendOnly(cfg)) {
        Put(db.get(), write_options, KeySpanByTrace(trace_id, span_id), line, &metrics);
        if (LayoutUsesIndexes(cfg)) {
          IndexSpan(db.get(), write_options, line, trace_id, span_id, timestamp_ns, &metrics);
          if (ContainsStringFieldValue(line, "status", "error")) {
            Put(db.get(), write_options, KeyErrorSpan(timestamp_ns, trace_id, span_id), "", &metrics, true);
          }
        }
      }
      ++metrics.updates;
    } else if (op == "update_trace") {
      std::string trace_id;
      uint64_t start_ns = 0;
      if (!ExtractStringField(line, "trace_id", &trace_id)) {
        ++metrics.parse_errors;
        continue;
      }
      ExtractU64Field(line, "start_ns", &start_ns);
      if (!LayoutAppendOnly(cfg)) {
        Put(db.get(), write_options, KeyTrace(trace_id), line, &metrics);
        if (cfg.layout == "tracelsm_segment") Put(db.get(), write_options, KeyTraceSegment(trace_id), line, &metrics, true);
        if (start_ns != 0 && !ContainsStringFieldValue(line, "status", "running")) {
          DeleteKey(db.get(), write_options, KeyRunningTrace(start_ns, trace_id), &metrics);
        }
      }
      ++metrics.updates;
    } else if (op == "query_trace_tree" || op == "query_hot_trace_tree") {
      std::string trace_id;
      if (!ExtractStringField(line, "trace_id", &trace_id)) {
        ++metrics.parse_errors;
        continue;
      }
      QueryTraceTree(db.get(), read_options, cfg, trace_id, op, &metrics);
    } else if (op == "query_failed_spans") {
      QueryPrefixOp(db.get(), read_options, "query_failed_spans", LayoutAppendOnly(cfg) ? "O#" : "X#", 0, &metrics,
                    LayoutAppendOnly(cfg) ? "error" : "");
    } else if (op == "get_span") {
      QueryGetSpan(db.get(), read_options, cfg, line, &metrics);
    } else if (op == "filter_spans") {
      QueryFilterSpans(db.get(), read_options, cfg, line, &metrics);
    } else if (op == "search_text") {
      QuerySearchText(db.get(), read_options, cfg, line, &metrics);
    } else if (op == "query_running_traces") {
      uint64_t limit = 100;
      ExtractU64Field(line, "limit", &limit);
      QueryPrefixOp(db.get(), read_options, "query_running_traces", LayoutAppendOnly(cfg) ? "O#" : "R#", limit, &metrics,
                    LayoutAppendOnly(cfg) ? "\"status\":\"running\"" : "");
    } else if (op == "get_thread") {
      uint64_t limit = 100;
      std::string thread_id;
      ExtractU64Field(line, "limit", &limit);
      ExtractStringField(line, "thread_id", &thread_id);
      const std::string prefix = LayoutUsesIndexes(cfg) ? PrefixThreadIndex(thread_id) : LayoutAppendOnly(cfg) ? "O#" : "T#";
      QueryPrefixOp(db.get(), read_options, "get_thread", prefix, limit, &metrics, LayoutUsesIndexes(cfg) ? "" : thread_id);
    } else if (op == "list_traces_by_session") {
      uint64_t limit = 100;
      std::string session_id;
      ExtractU64Field(line, "limit", &limit);
      ExtractStringField(line, "session_id", &session_id);
      const std::string prefix = LayoutUsesIndexes(cfg) ? PrefixSessionIndex(session_id) : LayoutAppendOnly(cfg) ? "O#" : "T#";
      QueryPrefixOp(db.get(), read_options, "list_traces_by_session", prefix, limit, &metrics, LayoutUsesIndexes(cfg) ? "" : session_id);
    } else if (op == "freshness_probe") {
      QueryGetSpan(db.get(), read_options, cfg, line, &metrics, true);
    }

    if (cfg.progress_interval != 0 && metrics.lines % cfg.progress_interval == 0) {
      std::cerr << "processed " << metrics.lines << " operations\n";
    }
  }

  db->Flush(rocksdb::FlushOptions()).PermitUncheckedError();
  const auto run_end = Clock::now();
  const double elapsed_sec = std::chrono::duration<double>(run_end - run_begin).count();
  PrintSummary(cfg, metrics, elapsed_sec, db.get());
  return metrics.write_errors == 0 ? 0 : 1;
}
