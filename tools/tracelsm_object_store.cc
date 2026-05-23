#include "tracelsm_object_store.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <utility>

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

}  // namespace

Status Status::OK() { return Status{true, ""}; }

Status Status::Error(std::string message) { return Status{false, std::move(message)}; }

std::unique_ptr<ObjectStore> NewLocalFileObjectStore(std::string root) {
  return std::make_unique<LocalFileObjectStore>(std::move(root));
}

}  // namespace tracelsm
