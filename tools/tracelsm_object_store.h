#pragma once

#include <cstdint>
#include <future>
#include <memory>
#include <string>
#include <string_view>

namespace tracelsm {

struct Status {
  bool ok = true;
  std::string message;

  static Status OK();
  static Status Error(std::string message);
};

struct ObjectHead {
  bool exists = false;
  uint64_t bytes = 0;
};

struct CosObjectStoreOptions {
  std::string secret_id;
  std::string secret_key;
  std::string bucket;
  std::string region;
  std::string endpoint;
  uint64_t timeout_sec = 30;
  int max_retries = 5;
  int concurrency = 1;  // number of background workers for async PUT
};

class ObjectStore {
 public:
  virtual ~ObjectStore() = default;

  virtual Status Put(std::string_view key, std::string_view data) = 0;
  virtual Status Get(std::string_view key, std::string* data) = 0;
  virtual Status Head(std::string_view key, ObjectHead* head) = 0;
  virtual std::string Uri(std::string_view key) const = 0;
  virtual std::string Name() const = 0;

  // Async PUT. Default implementation runs synchronously on the caller thread
  // and returns a ready future. Backends with worker pools (e.g. COS) override
  // this to overlap multiple in-flight PUTs.
  virtual std::future<Status> PutAsync(std::string key, std::string data);
};

std::unique_ptr<ObjectStore> NewLocalFileObjectStore(std::string root);
std::unique_ptr<ObjectStore> NewCosObjectStore(CosObjectStoreOptions options);

}  // namespace tracelsm
