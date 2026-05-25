#include "tracelsm_object_store.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <map>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include <curl/curl.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/sha.h>
#include <unistd.h>

namespace tracelsm {
namespace {

std::string SanitizeKey(std::string_view key) {
  std::string clean(key);
  while (!clean.empty() && clean.front() == '/') clean.erase(clean.begin());
  if (clean.find("..") != std::string::npos) {
    for (char& c : clean) {
      if (c == '.') c = '_';
    }
  }
  return clean;
}

std::string LowerAscii(std::string value) {
  for (char& c : value) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return value;
}

std::string Trim(std::string value) {
  while (!value.empty() && std::isspace(static_cast<unsigned char>(value.front()))) value.erase(value.begin());
  while (!value.empty() && std::isspace(static_cast<unsigned char>(value.back()))) value.pop_back();
  return value;
}

std::string StripEndpoint(std::string endpoint) {
  const std::string https = "https://";
  const std::string http = "http://";
  if (endpoint.rfind(https, 0) == 0) endpoint.erase(0, https.size());
  if (endpoint.rfind(http, 0) == 0) endpoint.erase(0, http.size());
  while (!endpoint.empty() && endpoint.back() == '/') endpoint.pop_back();
  return endpoint;
}

std::string HexBytes(const unsigned char* data, unsigned int len) {
  std::ostringstream oss;
  oss << std::hex << std::setfill('0');
  for (unsigned int i = 0; i < len; ++i) oss << std::setw(2) << static_cast<unsigned int>(data[i]);
  return oss.str();
}

std::string Sha1Hex(std::string_view data) {
  unsigned char digest[SHA_DIGEST_LENGTH];
  SHA1(reinterpret_cast<const unsigned char*>(data.data()), data.size(), digest);
  return HexBytes(digest, SHA_DIGEST_LENGTH);
}

std::string HmacSha1Hex(std::string_view key, std::string_view data) {
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int len = 0;
  HMAC(EVP_sha1(), key.data(), static_cast<int>(key.size()), reinterpret_cast<const unsigned char*>(data.data()), data.size(), digest, &len);
  return HexBytes(digest, len);
}

std::string UrlEncode(std::string_view value) {
  static constexpr char kHex[] = "0123456789ABCDEF";
  std::string out;
  out.reserve(value.size());
  for (const unsigned char c : value) {
    if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
      out.push_back(static_cast<char>(c));
    } else {
      out.push_back('%');
      out.push_back(kHex[c >> 4]);
      out.push_back(kHex[c & 0x0f]);
    }
  }
  return out;
}

std::string UrlEncodePath(std::string_view path) {
  std::string out;
  out.reserve(path.size());
  size_t begin = 0;
  while (begin < path.size()) {
    const size_t slash = path.find('/', begin);
    const size_t end = slash == std::string_view::npos ? path.size() : slash;
    out += UrlEncode(path.substr(begin, end - begin));
    if (slash == std::string_view::npos) break;
    out.push_back('/');
    begin = slash + 1;
  }
  return out;
}

void CurlGlobalInitOnce() {
  static const int unused = [] {
    curl_global_init(CURL_GLOBAL_DEFAULT);
    return 0;
  }();
  (void)unused;
}

size_t WriteCallback(char* ptr, size_t size, size_t nmemb, void* userdata) {
  auto* out = static_cast<std::string*>(userdata);
  out->append(ptr, size * nmemb);
  return size * nmemb;
}

size_t HeaderCallback(char* buffer, size_t size, size_t nitems, void* userdata) {
  const size_t total = size * nitems;
  std::string line(buffer, total);
  const size_t colon = line.find(':');
  if (colon != std::string::npos) {
    std::string key = LowerAscii(Trim(line.substr(0, colon)));
    std::string value = Trim(line.substr(colon + 1));
    auto* headers = static_cast<std::map<std::string, std::string>*>(userdata);
    (*headers)[std::move(key)] = std::move(value);
  }
  return total;
}

struct HttpResult {
  CURLcode curl_code = CURLE_OK;
  long status_code = 0;
  std::string body;
  std::map<std::string, std::string> headers;
  std::string error;
};

bool Retryable(long status_code) {
  return status_code == 429 || status_code == 500 || status_code == 502 || status_code == 503 || status_code == 504;
}

class LocalFileObjectStore final : public ObjectStore {
 public:
  explicit LocalFileObjectStore(std::string root) : root_(std::move(root)) {
    std::filesystem::create_directories(root_);
  }

  Status Put(std::string_view key, std::string_view data) override {
    const std::filesystem::path dest = PathFor(key);
    std::error_code ec;
    std::filesystem::create_directories(dest.parent_path(), ec);
    if (ec) return Status::Error("create object directory failed: " + ec.message());

    const std::filesystem::path tmp = dest.parent_path() / (".tmp_" + dest.filename().string() + "." + std::to_string(::getpid()));
    {
      std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
      if (!out) return Status::Error("open temp object failed: " + tmp.string());
      out.write(data.data(), static_cast<std::streamsize>(data.size()));
      if (!out) {
        std::filesystem::remove(tmp, ec);
        return Status::Error("write temp object failed: " + tmp.string());
      }
    }
    std::filesystem::rename(tmp, dest, ec);
    if (ec) {
      std::filesystem::remove(tmp, ec);
      return Status::Error("rename object failed: " + ec.message());
    }
    return Status::OK();
  }

  Status Get(std::string_view key, std::string* data) override {
    const std::filesystem::path path = PathFor(key);
    std::ifstream in(path, std::ios::binary);
    if (!in) return Status::Error("open object failed: " + path.string());
    std::ostringstream ss;
    ss << in.rdbuf();
    *data = ss.str();
    return Status::OK();
  }

  Status Head(std::string_view key, ObjectHead* head) override {
    const std::filesystem::path path = PathFor(key);
    std::error_code ec;
    const bool exists = std::filesystem::exists(path, ec);
    if (ec) return Status::Error("stat object failed: " + ec.message());
    head->exists = exists;
    head->bytes = exists ? static_cast<uint64_t>(std::filesystem::file_size(path, ec)) : 0;
    if (ec) return Status::Error("size object failed: " + ec.message());
    return Status::OK();
  }

  std::string Uri(std::string_view key) const override {
    return "local://" + (root_ / SanitizeKey(key)).string();
  }

  std::string Name() const override { return "local"; }

 private:
  std::filesystem::path PathFor(std::string_view key) const { return root_ / SanitizeKey(key); }

  std::filesystem::path root_;
};

class CosObjectStore final : public ObjectStore {
 public:
  explicit CosObjectStore(CosObjectStoreOptions options) : options_(std::move(options)) {
    options_.endpoint = StripEndpoint(options_.endpoint);
    if (options_.endpoint.empty() && !options_.bucket.empty() && !options_.region.empty()) {
      // Default to Tencent Cloud internal endpoint; this binary is expected to run inside the same VPC.
      options_.endpoint = "cos-internal." + options_.region + ".tencentcos.cn";
    }
    // Region-level endpoint (e.g. cos-internal.ap-beijing.tencentcos.cn) -> prepend bucket for vhost-style addressing.
    if (!options_.bucket.empty() && !options_.endpoint.empty()) {
      const std::string bucket_prefix = options_.bucket + ".";
      if (options_.endpoint.rfind(bucket_prefix, 0) != 0) {
        options_.endpoint = bucket_prefix + options_.endpoint;
      }
    }
    if (options_.max_retries <= 0) options_.max_retries = 1;
    if (options_.timeout_sec == 0) options_.timeout_sec = 30;
    if (options_.concurrency < 1) options_.concurrency = 1;
    CurlGlobalInitOnce();
    StartWorkers();
  }

  ~CosObjectStore() override { StopWorkers(); }

  Status Put(std::string_view key, std::string_view data) override {
    return DoRequest("PUT", key, data);
  }

  std::future<Status> PutAsync(std::string key, std::string data) override {
    auto promise = std::make_shared<std::promise<Status>>();
    auto fut = promise->get_future();
    {
      std::lock_guard<std::mutex> lk(mu_);
      queue_.emplace_back(Job{std::move(key), std::move(data), promise});
    }
    cv_.notify_one();
    return fut;
  }

  Status Get(std::string_view key, std::string* data) override {
    HttpResult result;
    Status s = DoRequestImpl("GET", key, std::nullopt, nullptr, &result);
    if (!s.ok) return s;
    *data = std::move(result.body);
    return Status::OK();
  }

  Status Head(std::string_view key, ObjectHead* head) override {
    HttpResult result;
    Status s = DoRequestImpl("HEAD", key, std::nullopt, nullptr, &result);
    if (!s.ok && result.status_code != 404) return s;
    if (result.status_code == 404) {
      head->exists = false;
      head->bytes = 0;
      return Status::OK();
    }
    head->exists = true;
    auto it = result.headers.find("content-length");
    head->bytes = it == result.headers.end() ? 0 : static_cast<uint64_t>(std::stoull(it->second));
    return Status::OK();
  }

  std::string Uri(std::string_view key) const override {
    return "cos://" + options_.bucket + "/" + SanitizeKey(key);
  }

  std::string Name() const override { return "cos"; }

 private:
  struct Job {
    std::string key;
    std::string data;
    std::shared_ptr<std::promise<Status>> promise;
  };

  void StartWorkers() {
    for (int i = 0; i < options_.concurrency; ++i) {
      workers_.emplace_back([this] { WorkerLoop(); });
    }
  }

  void StopWorkers() {
    {
      std::lock_guard<std::mutex> lk(mu_);
      stopping_ = true;
    }
    cv_.notify_all();
    for (auto& t : workers_) {
      if (t.joinable()) t.join();
    }
  }

  void WorkerLoop() {
    // Each worker keeps a long-lived CURL handle to enable connection keepalive.
    CURL* curl = curl_easy_init();
    while (true) {
      Job job;
      {
        std::unique_lock<std::mutex> lk(mu_);
        cv_.wait(lk, [this] { return stopping_ || !queue_.empty(); });
        if (stopping_ && queue_.empty()) break;
        job = std::move(queue_.front());
        queue_.pop_front();
      }
      Status s = DoRequestImpl("PUT", job.key, std::string_view(job.data), curl, nullptr);
      job.promise->set_value(std::move(s));
    }
    if (curl != nullptr) curl_easy_cleanup(curl);
  }

  Status DoRequest(std::string_view method, std::string_view key, std::optional<std::string_view> data) {
    return DoRequestImpl(method, key, data, nullptr, nullptr);
  }

  std::string Authorization(std::string_view method, std::string_view path, const std::map<std::string, std::string>& headers) const {
    const int64_t now = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    const std::string key_time = std::to_string(now) + ";" + std::to_string(now + 3600);
    const std::string sign_key = HmacSha1Hex(options_.secret_key, key_time);

    std::string header_string;
    std::string header_list;
    bool first = true;
    for (const auto& item : headers) {
      if (!first) {
        header_string += "&";
        header_list += ";";
      }
      first = false;
      header_string += UrlEncode(item.first) + "=" + UrlEncode(item.second);
      header_list += item.first;
    }

    std::string lower_method(method);
    lower_method = LowerAscii(std::move(lower_method));
    const std::string http_string = lower_method + "\n" + std::string(path) + "\n\n" + header_string + "\n";
    const std::string string_to_sign = "sha1\n" + key_time + "\n" + Sha1Hex(http_string) + "\n";
    const std::string signature = HmacSha1Hex(sign_key, string_to_sign);

    return "q-sign-algorithm=sha1"
           "&q-ak=" + options_.secret_id +
           "&q-sign-time=" + key_time +
           "&q-key-time=" + key_time +
           "&q-header-list=" + header_list +
           "&q-url-param-list="
           "&q-signature=" + signature;
  }

  // Performs a single HTTP request with retries. If `reusable_curl` is non-null,
  // the same handle is reused across calls to enable TLS session/connection keepalive.
  // If `out_result` is non-null, the final HttpResult is also returned to the caller
  // (used for GET/HEAD where the body/headers are needed).
  Status DoRequestImpl(std::string_view method, std::string_view key, std::optional<std::string_view> data,
                       CURL* reusable_curl, HttpResult* out_result) const {
    const std::string clean_key = SanitizeKey(key);
    const std::string path = "/" + clean_key;
    const std::string url = "https://" + options_.endpoint + UrlEncodePath(path);

    HttpResult last;
    for (int attempt = 0; attempt < options_.max_retries; ++attempt) {
      // Authorization header is derived from the current wall-clock time, so we
      // compute it inside the retry loop to avoid expiring on long backoffs.
      std::map<std::string, std::string> signed_headers;
      signed_headers["host"] = options_.endpoint;
      if (data.has_value()) {
        signed_headers["content-length"] = std::to_string(data->size());
        signed_headers["content-type"] = "application/octet-stream";
      }
      const std::string authorization = Authorization(method, path, signed_headers);

      HttpResult result;
      CURL* curl = reusable_curl != nullptr ? reusable_curl : curl_easy_init();
      if (curl == nullptr) return Status::Error("curl_easy_init failed");
      if (reusable_curl != nullptr) curl_easy_reset(curl);

      struct curl_slist* headers = nullptr;
      headers = curl_slist_append(headers, ("Host: " + options_.endpoint).c_str());
      headers = curl_slist_append(headers, ("Authorization: " + authorization).c_str());
      if (data.has_value()) {
        headers = curl_slist_append(headers, ("Content-Length: " + std::to_string(data->size())).c_str());
        headers = curl_slist_append(headers, "Content-Type: application/octet-stream");
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "PUT");
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, data->data());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(data->size()));
      } else if (method == "HEAD") {
        curl_easy_setopt(curl, CURLOPT_NOBODY, 1L);
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "HEAD");
      } else {
        curl_easy_setopt(curl, CURLOPT_HTTPGET, 1L);
      }

      curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
      curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
      curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteCallback);
      curl_easy_setopt(curl, CURLOPT_WRITEDATA, &result.body);
      curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, HeaderCallback);
      curl_easy_setopt(curl, CURLOPT_HEADERDATA, &result.headers);
      curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
      curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, static_cast<long>(options_.timeout_sec));
      curl_easy_setopt(curl, CURLOPT_TIMEOUT, static_cast<long>(options_.timeout_sec));
      // Keep TCP/TLS connections alive for reuse on the same handle.
      curl_easy_setopt(curl, CURLOPT_TCP_KEEPALIVE, 1L);

      char error_buf[CURL_ERROR_SIZE] = {0};
      curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, error_buf);
      result.curl_code = curl_easy_perform(curl);
      curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &result.status_code);
      if (result.curl_code != CURLE_OK) result.error = error_buf[0] != '\0' ? error_buf : curl_easy_strerror(result.curl_code);

      curl_slist_free_all(headers);
      if (reusable_curl == nullptr) curl_easy_cleanup(curl);

      last = std::move(result);
      if (last.curl_code == CURLE_OK && !Retryable(last.status_code)) break;
      if (attempt + 1 < options_.max_retries) std::this_thread::sleep_for(std::chrono::seconds(std::min(8, 1 << attempt)));
    }

    if (out_result != nullptr) *out_result = last;
    if (last.curl_code != CURLE_OK) {
      return Status::Error(std::string("COS ") + std::string(method) + " failed: " + last.error);
    }
    const long code = last.status_code;
    const bool ok = (method == "PUT" && (code == 200 || code == 201)) ||
                    (method == "GET" && code == 200) ||
                    (method == "HEAD" && (code == 200 || code == 404));
    if (!ok) {
      return Status::Error(std::string("COS ") + std::string(method) +
                           " failed: HTTP " + std::to_string(code) + " " + last.body.substr(0, 256));
    }
    return Status::OK();
  }

  CosObjectStoreOptions options_;

  std::vector<std::thread> workers_;
  std::deque<Job> queue_;
  std::mutex mu_;
  std::condition_variable cv_;
  bool stopping_ = false;
};

}  // namespace

Status Status::OK() { return Status{true, ""}; }

Status Status::Error(std::string message) { return Status{false, std::move(message)}; }

std::future<Status> ObjectStore::PutAsync(std::string key, std::string data) {
  std::promise<Status> p;
  p.set_value(Put(key, data));
  return p.get_future();
}

std::unique_ptr<ObjectStore> NewLocalFileObjectStore(std::string root) {
  return std::make_unique<LocalFileObjectStore>(std::move(root));
}

std::unique_ptr<ObjectStore> NewCosObjectStore(CosObjectStoreOptions options) {
  return std::make_unique<CosObjectStore>(std::move(options));
}

}  // namespace tracelsm
