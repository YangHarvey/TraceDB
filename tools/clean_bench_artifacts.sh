#!/usr/bin/env bash
# clean_bench_artifacts.sh - 清理 TraceDB benchmark 中间产物
#
# 用法：
#   tools/clean_bench_artifacts.sh           # 默认清 smoke / 临时 cos run / 空目录，不动 10GB+ 历史结果
#   tools/clean_bench_artifacts.sh --all     # 也清 100GB+ 历史结果（询问确认）
#   tools/clean_bench_artifacts.sh --dry-run # 只打印将要删除的内容，不真删
#   tools/clean_bench_artifacts.sh --cos     # 同步清 COS 上 tracelsm-cpp-* / tracelsm-cos-* 前缀对象
#
# 安全策略：
#   1. 只清 bench-results/ 下生成的中间 *-db 目录、*-tracelsm-objectstore 目录、空 run.log/time。
#   2. 默认保留 operations.jsonl / payloads.jsonl / *.json summary，方便事后查看。
#   3. >5GB 的历史目录默认跳过，需要 --all 才会列入候选并交互确认。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BENCH_DIR="${REPO_ROOT}/bench-results"
DRY_RUN=false
ALL=false
CLEAN_COS=false

usage() {
  grep -E '^#( |$)' "$0" | sed 's/^# \?//'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --all) ALL=true ;;
    --cos) CLEAN_COS=true ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

echo "[clean] repo=$REPO_ROOT bench=$BENCH_DIR dry_run=$DRY_RUN all=$ALL cos=$CLEAN_COS"
echo ""

if [[ ! -d "$BENCH_DIR" ]]; then
  echo "[clean] no bench-results directory, nothing to do."
  exit 0
fi

# Helper: human readable size
human_size() {
  du -sh "$1" 2>/dev/null | awk '{print $1}'
}

remove_path() {
  local path="$1"
  local size
  size=$(human_size "$path")
  if $DRY_RUN; then
    echo "  [dry-run] rm -rf $path  ($size)"
  else
    echo "  rm -rf $path  ($size)"
    rm -rf "$path"
  fi
}

echo "== 1) 生成的 RocksDB / 对象存储 中间目录 =="
shopt -s nullglob
# 中间产物：*-db / tracelsm-objectstore（这些只是数据库/对象 dump，不影响 summary 复盘）
for d in "$BENCH_DIR"/*/rocksdb-*-db "$BENCH_DIR"/*/tracelsm-objectstore "$BENCH_DIR"/*/objectstore; do
  [[ -d "$d" ]] || continue
  remove_path "$d"
done

# smoke 目录下的可重生成数据（保留 summary json）
for d in "$BENCH_DIR"/smoke "$BENCH_DIR"/smoke-tracelsm-object "$BENCH_DIR"/smoke-layouts; do
  [[ -d "$d" ]] || continue
  remove_path "$d"
done

echo ""
echo "== 2) 临时 / 失败的 COS run 目录（包含 0 字节 summary 的）=="
for d in "$BENCH_DIR"/*-tracelsm-cos-* "$BENCH_DIR"/rocksdb-vs-tracelsm-cos-*; do
  [[ -d "$d" ]] || continue
  summary=$(ls "$d"/*.json 2>/dev/null | head -1)
  if [[ -z "$summary" || ! -s "$summary" ]]; then
    remove_path "$d"
  else
    echo "  [keep] $d (summary=$summary, size=$(human_size "$d"))"
  fi
done

echo ""
echo "== 3) 大型历史目录（>5GB）=="
for d in "$BENCH_DIR"/*; do
  [[ -d "$d" ]] || continue
  bytes=$(du -sb "$d" 2>/dev/null | awk '{print $1}')
  bytes=${bytes:-0}
  if (( bytes > 5*1024*1024*1024 )); then
    if $ALL; then
      echo "  candidate: $d ($(human_size "$d"))"
      if ! $DRY_RUN; then
        read -r -p "    delete? [y/N] " ans
        if [[ "$ans" =~ ^[Yy]$ ]]; then
          remove_path "$d"
        else
          echo "    [skip]"
        fi
      else
        echo "    [dry-run] would prompt for deletion"
      fi
    else
      echo "  [skip-large] $d ($(human_size "$d"))  -- pass --all to clean"
    fi
  fi
done

if $CLEAN_COS; then
  echo ""
  echo "== 4) COS 上 tracelsm-cpp-* / tracelsm-cos-* 前缀对象 =="
  if [[ ! -f "$REPO_ROOT/.env.cos.local" ]]; then
    echo "  [warn] .env.cos.local not found, skip COS cleanup"
  else
    # shellcheck disable=SC1091
    set -a; source "$REPO_ROOT/.env.cos.local"; set +a
    python3 - <<PY
import os, sys
try:
    from qcloud_cos import CosConfig, CosS3Client
except ImportError:
    print("  [warn] qcloud_cos not installed (pip3 install cos-python-sdk-v5), skip")
    sys.exit(0)

region = os.environ.get("COS_REGION", "")
bucket = os.environ.get("COS_BUCKET", "")
sid = os.environ.get("COS_SECRET_ID", "")
skey = os.environ.get("COS_SECRET_KEY", "")
endpoint = os.environ.get("COS_ENDPOINT") or f"cos-internal.{region}.tencentcos.cn"
dry = ${DRY_RUN}

cfg = CosConfig(Region=region, SecretId=sid, SecretKey=skey, Endpoint=endpoint)
c = CosS3Client(cfg)
prefixes = ["tracelsm-cpp-", "tracelsm-cos-"]
for prefix in prefixes:
    marker = ""
    total = 0
    while True:
        resp = c.list_objects(Bucket=bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
        items = resp.get("Contents", []) or []
        if not items:
            break
        keys = [{"Key": o["Key"]} for o in items]
        total += len(keys)
        if dry:
            print(f"  [dry-run] would delete {len(keys)} objects under prefix={prefix}")
        else:
            c.delete_objects(Bucket=bucket, Delete={"Object": keys, "Quiet": True})
            print(f"  deleted {len(keys)} objects under prefix={prefix}")
        if not resp.get("IsTruncated") or resp.get("IsTruncated") in ("false", "False", False):
            break
        marker = resp.get("NextMarker") or items[-1]["Key"]
    print(f"  total under prefix={prefix}: {total}")
PY
  fi
fi

echo ""
echo "[clean] done."
