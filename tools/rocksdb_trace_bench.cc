#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <unordered_map>
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
  std::string layout = "trace_first";
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
};

struct Metrics {
  uint64_t lines = 0;
  uint64_t writes = 0;
  uint64_t inserts = 0;
  uint64_t updates = 0;
  uint64_t appends = 0;
  uint64_t queries = 0;
  uint64_t query_failed_spans = 0;
  uint64_t write_errors = 0;
  uint64_t parse_errors = 0;
  uint64_t scanned_keys = 0;
  uint64_t scanned_bytes = 0;
  uint64_t write_ns = 0;
  std::vector<uint64_t> query_lat_us;
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

std::string KeyTrace(const std::string& trace_id) {
  return "T#" + trace_id;
}

std::string KeySpanByTrace(const std::string& trace_id, const std::string& span_id) {
  return "S#" + trace_id + "#" + span_id;
}

std::string PrefixSpanByTrace(const std::string& trace_id) {
  return "S#" + trace_id + "#";
}

std::string KeyEventByTrace(const std::string& trace_id, uint64_t timestamp_ns, const std::string& event_id) {
  return "E#" + trace_id + "#" + PaddedU64(timestamp_ns) + "#" + event_id;
}

std::string PrefixEventByTrace(const std::string& trace_id) {
  return "E#" + trace_id + "#";
}

std::string KeyChildByParent(const std::string& trace_id, const std::string& parent_span_id, const std::string& span_id) {
  return "C#" + trace_id + "#" + parent_span_id + "#" + span_id;
}

std::string KeyRunningTrace(uint64_t start_ns, const std::string& trace_id) {
  return "R#" + PaddedU64(start_ns) + "#" + trace_id;
}

std::string KeyErrorSpan(uint64_t timestamp_ns, const std::string& trace_id, const std::string& span_id) {
  return "X#" + PaddedU64(timestamp_ns) + "#" + trace_id + "#" + span_id;
}

bool Put(rocksdb::DB* db, const rocksdb::WriteOptions& write_options, const std::string& key,
         const std::string& value, Metrics* metrics) {
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
  return true;
}

ScanResult ScanPrefix(rocksdb::DB* db, const rocksdb::ReadOptions& read_options, const std::string& prefix) {
  ScanResult result;
  std::unique_ptr<rocksdb::Iterator> it(db->NewIterator(read_options));
  for (it->Seek(prefix); it->Valid() && StartsWith(it->key(), prefix); it->Next()) {
    ++result.keys;
    result.bytes += it->key().size() + it->value().size();
  }
  return result;
}

bool QueryTraceTree(rocksdb::DB* db, const rocksdb::ReadOptions& read_options, const std::string& trace_id,
                    bool scan_events, Metrics* metrics) {
  const auto begin = Clock::now();
  std::string value;
  db->Get(read_options, KeyTrace(trace_id), &value).PermitUncheckedError();

  const ScanResult spans = ScanPrefix(db, read_options, PrefixSpanByTrace(trace_id));
  ScanResult events;
  if (scan_events) {
    events = ScanPrefix(db, read_options, PrefixEventByTrace(trace_id));
  }
  const auto end = Clock::now();
  const uint64_t latency_us =
      static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count());
  metrics->query_lat_us.push_back(latency_us);
  metrics->scanned_keys += spans.keys + events.keys;
  metrics->scanned_bytes += spans.bytes + events.bytes + value.size();
  ++metrics->queries;
  return true;
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
      << "  --layout trace_first         Current supported layout\n"
      << "  --destroy_db true|false      Destroy DB before run, default true\n"
      << "  --disable_wal true|false     Disable RocksDB WAL, default false\n"
      << "  --sync true|false            Sync writes, default false\n"
      << "  --scan_events true|false     Include event scan in get_trace_tree, default true\n"
      << "  --max_ops <n>                Stop after n operations, default unlimited\n"
      << "  --progress_interval <n>      Progress log interval, default 100000\n";
}

bool ParseBool(const std::string& v) {
  return v == "true" || v == "1" || v == "yes" || v == "on";
}

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
  if (cfg->layout != "trace_first") {
    std::cerr << "only --layout trace_first is supported in this version\n";
    return false;
  }
  return true;
}

void PrintSummary(const Config& cfg, const Metrics& metrics, double elapsed_sec, rocksdb::DB* db) {
  uint64_t live_data_size = 0;
  uint64_t total_sst_size = 0;
  db->GetIntProperty("rocksdb.estimate-live-data-size", &live_data_size);
  db->GetIntProperty("rocksdb.total-sst-files-size", &total_sst_size);

  const uint64_t query_sum = Sum(metrics.query_lat_us);
  const double avg_query_us = metrics.query_lat_us.empty()
                                  ? 0.0
                                  : static_cast<double>(query_sum) / static_cast<double>(metrics.query_lat_us.size());
  const double write_sec = static_cast<double>(metrics.write_ns) / 1e9;
  const double write_ops_per_sec = write_sec > 0.0 ? static_cast<double>(metrics.writes) / write_sec : 0.0;
  const double end_to_end_ops_per_sec = elapsed_sec > 0.0 ? static_cast<double>(metrics.lines) / elapsed_sec : 0.0;

  std::cout << "{\n"
            << "  \"operations_path\": \"" << EscapeJson(cfg.operations_path) << "\",\n"
            << "  \"db_path\": \"" << EscapeJson(cfg.db_path) << "\",\n"
            << "  \"layout\": \"" << cfg.layout << "\",\n"
            << "  \"lines\": " << metrics.lines << ",\n"
            << "  \"writes\": " << metrics.writes << ",\n"
            << "  \"inserts\": " << metrics.inserts << ",\n"
            << "  \"updates\": " << metrics.updates << ",\n"
            << "  \"appends\": " << metrics.appends << ",\n"
            << "  \"queries\": " << metrics.queries << ",\n"
            << "  \"query_failed_spans\": " << metrics.query_failed_spans << ",\n"
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
            << "  \"rocksdb_estimate_live_data_size\": " << live_data_size << ",\n"
            << "  \"rocksdb_total_sst_files_size\": " << total_sst_size << "\n"
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
  if (!db_parent.empty()) {
    std::filesystem::create_directories(db_parent);
  }

  rocksdb::Options options;
  options.create_if_missing = true;
  options.IncreaseParallelism();
  options.OptimizeLevelStyleCompaction();

  if (cfg.destroy_db) {
    rocksdb::DestroyDB(cfg.db_path, options).PermitUncheckedError();
  }

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

    if (op == "insert_trace") {
      std::string trace_id;
      uint64_t start_ns = 0;
      if (!ExtractStringField(line, "trace_id", &trace_id)) {
        ++metrics.parse_errors;
        continue;
      }
      ExtractU64Field(line, "start_ns", &start_ns);
      Put(db.get(), write_options, KeyTrace(trace_id), line, &metrics);
      Put(db.get(), write_options, KeyRunningTrace(start_ns, trace_id), "", &metrics);
      ++metrics.inserts;
    } else if (op == "insert_span") {
      std::string trace_id;
      std::string span_id;
      std::string parent_span_id;
      if (!ExtractStringField(line, "trace_id", &trace_id) || !ExtractStringField(line, "span_id", &span_id)) {
        ++metrics.parse_errors;
        continue;
      }
      Put(db.get(), write_options, KeySpanByTrace(trace_id, span_id), line, &metrics);
      if (ExtractStringField(line, "parent_span_id", &parent_span_id) && !parent_span_id.empty()) {
        Put(db.get(), write_options, KeyChildByParent(trace_id, parent_span_id, span_id), "", &metrics);
      }
      ++metrics.inserts;
    } else if (op == "append_event") {
      std::string trace_id;
      std::string event_id;
      uint64_t timestamp_ns = 0;
      if (!ExtractStringField(line, "trace_id", &trace_id) || !ExtractStringField(line, "event_id", &event_id) ||
          !ExtractU64Field(line, "timestamp_ns", &timestamp_ns)) {
        ++metrics.parse_errors;
        continue;
      }
      Put(db.get(), write_options, KeyEventByTrace(trace_id, timestamp_ns, event_id), line, &metrics);
      ++metrics.appends;
    } else if (op == "update_span") {
      std::string trace_id;
      std::string span_id;
      uint64_t timestamp_ns = 0;
      if (!ExtractStringField(line, "trace_id", &trace_id) || !ExtractStringField(line, "span_id", &span_id)) {
        ++metrics.parse_errors;
        continue;
      }
      ExtractU64Field(line, "timestamp_ns", &timestamp_ns);
      Put(db.get(), write_options, KeySpanByTrace(trace_id, span_id), line, &metrics);
      if (ContainsStringFieldValue(line, "status", "error")) {
        Put(db.get(), write_options, KeyErrorSpan(timestamp_ns, trace_id, span_id), "", &metrics);
      }
      ++metrics.updates;
    } else if (op == "update_trace") {
      std::string trace_id;
      if (!ExtractStringField(line, "trace_id", &trace_id)) {
        ++metrics.parse_errors;
        continue;
      }
      Put(db.get(), write_options, KeyTrace(trace_id), line, &metrics);
      ++metrics.updates;
    } else if (op == "query_trace_tree") {
      std::string trace_id;
      if (!ExtractStringField(line, "trace_id", &trace_id)) {
        ++metrics.parse_errors;
        continue;
      }
      QueryTraceTree(db.get(), read_options, trace_id, cfg.scan_events, &metrics);
    } else if (op == "query_failed_spans") {
      const auto begin = Clock::now();
      const ScanResult errors = ScanPrefix(db.get(), read_options, "X#");
      const auto end = Clock::now();
      metrics.query_lat_us.push_back(
          static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count()));
      metrics.scanned_keys += errors.keys;
      metrics.scanned_bytes += errors.bytes;
      ++metrics.queries;
      ++metrics.query_failed_spans;
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
