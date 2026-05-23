#!/usr/bin/env python3
"""TraceLSM Object Store MVP.

This tool externalizes large payloads from a synthetic-agent-trace-generator
dataset (``payloads.jsonl``) into either a local object directory or Tencent
Cloud Object Storage (COS) using a small subset of the COS XML API. It is the
first piece of the TraceLSM design: separate large payload objects from the
local LSM metadata, and produce an ``object_manifest.jsonl`` that future
modules (TraceSegment builder, payload hydration) can consume.

Semantic compaction, trace segment building and query-time hydration are
intentionally NOT implemented here -- this is the minimal Object Storage
foundation only.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


# ----------------------------- payload parsing -----------------------------


def parse_payload_ref(payload_ref: str) -> Tuple[str, str, str, str]:
    """Return (trace_id, span_id, kind, payload_id) from an ``obj://`` ref.

    Generator produces refs like::

        obj://payloads/{trace_id}/{span_id}/{kind}/{payload_id}
    """
    if not payload_ref.startswith("obj://"):
        raise ValueError(f"unexpected payload_ref scheme: {payload_ref!r}")
    rest = payload_ref[len("obj://"):]
    parts = rest.split("/")
    if len(parts) < 5 or parts[0] != "payloads":
        raise ValueError(f"unexpected payload_ref shape: {payload_ref!r}")
    return parts[1], parts[2], parts[3], parts[4]


def derive_object_key(prefix: str, payload_ref: str) -> str:
    trace_id, span_id, kind, payload_id = parse_payload_ref(payload_ref)
    clean_prefix = prefix.strip("/")
    head = f"{clean_prefix}/" if clean_prefix else ""
    return f"{head}payloads/{trace_id}/{span_id}/{kind}/{payload_id}.payload"


def iter_payload_rows(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


# ----------------------------- backends -----------------------------


class ObjectStoreBackend:
    name = "base"

    def put_bytes(self, key: str, data: bytes, content_sha256: str) -> str:
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def head(self, key: str) -> Dict[str, Any]:
        raise NotImplementedError

    def uri(self, key: str) -> str:
        raise NotImplementedError


class LocalFileObjectStore(ObjectStoreBackend):
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key

    def put_bytes(self, key: str, data: bytes, content_sha256: str) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # write to temp then rename to avoid half-written objects
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=str(dest.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_path, dest)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return self.uri(key)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def head(self, key: str) -> Dict[str, Any]:
        p = self._path(key)
        st = p.stat()
        return {"bytes": st.st_size, "exists": True}

    def uri(self, key: str) -> str:
        return f"local://{self.root}/{key}"


class CosObjectStore(ObjectStoreBackend):
    """Minimal Tencent COS XML API client (PUT/GET/HEAD).

    Implements the COS request signature v3 documented at
    https://cloud.tencent.com/document/product/436/7778. Only the request
    paths used by this MVP are exercised.
    """

    name = "cos"

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        bucket: str,
        region: str,
        endpoint: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 5,
    ) -> None:
        if not secret_id or not secret_key:
            raise ValueError("COS secret_id and secret_key must not be empty")
        if not bucket:
            raise ValueError("COS bucket must not be empty")
        if not region and not endpoint:
            raise ValueError("either COS region or endpoint must be provided")
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        self.host = endpoint or f"{bucket}.cos.{region}.myqcloud.com"
        self.host = self.host.replace("https://", "").replace("http://", "").strip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)

    # --- signing ---
    def _sign(self, method: str, path: str, headers: Dict[str, str], expire: int = 3600) -> str:
        # Path must start with /
        if not path.startswith("/"):
            path = "/" + path
        method = method.lower()
        now = int(time.time())
        key_time = f"{now};{now + expire}"
        sign_key = hmac.new(self.secret_key.encode("utf-8"), key_time.encode("utf-8"), hashlib.sha1).hexdigest()

        # We use no URL query params here.
        header_pairs = []
        header_keys: List[str] = []
        for k in sorted(headers.keys(), key=lambda x: x.lower()):
            lk = k.lower()
            v = headers[k]
            header_pairs.append(f"{urllib.parse.quote(lk, safe='')}={urllib.parse.quote(str(v), safe='')}")
            header_keys.append(lk)
        header_string = "&".join(header_pairs)
        header_list = ";".join(header_keys)

        http_string = f"{method}\n{path}\n\n{header_string}\n"
        string_to_sign = (
            f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode('utf-8')).hexdigest()}\n"
        )
        signature = hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).hexdigest()
        return (
            "q-sign-algorithm=sha1"
            f"&q-ak={self.secret_id}"
            f"&q-sign-time={key_time}"
            f"&q-key-time={key_time}"
            f"&q-header-list={header_list}"
            "&q-url-param-list="
            f"&q-signature={signature}"
        )

    def _request(self, method: str, key: str, data: Optional[bytes], extra_headers: Optional[Dict[str, str]] = None) -> Tuple[int, bytes, Dict[str, str]]:
        path = "/" + key.lstrip("/")
        headers: Dict[str, str] = {"Host": self.host}
        if data is not None:
            headers["Content-Length"] = str(len(data))
            headers["Content-Type"] = "application/octet-stream"
        if extra_headers:
            headers.update(extra_headers)

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            signing_headers = dict(headers)
            authorization = self._sign(method, path, signing_headers)
            signing_headers["Authorization"] = authorization

            url = f"https://{self.host}{urllib.parse.quote(path, safe='/')}"
            req = urllib.request.Request(url, data=data, method=method, headers=signing_headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    return resp.status, body, dict(resp.headers.items())
            except urllib.error.HTTPError as e:
                body = e.read() if e.fp else b""
                if e.code in (429, 500, 502, 503, 504) and attempt + 1 < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    last_err = e
                    continue
                raise RuntimeError(f"COS {method} {path} failed: {e.code} {body[:256]!r}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt + 1 < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    last_err = e
                    continue
                raise
        raise RuntimeError(f"COS {method} {path} exhausted retries: {last_err}")

    def put_bytes(self, key: str, data: bytes, content_sha256: str) -> str:
        extra = {"x-cos-meta-sha256": content_sha256}
        status, _, _ = self._request("PUT", key, data, extra_headers=extra)
        if status not in (200, 201):
            raise RuntimeError(f"COS PUT {key} unexpected status {status}")
        return self.uri(key)

    def get_bytes(self, key: str) -> bytes:
        status, body, _ = self._request("GET", key, None)
        if status != 200:
            raise RuntimeError(f"COS GET {key} unexpected status {status}")
        return body

    def head(self, key: str) -> Dict[str, Any]:
        status, _, headers = self._request("HEAD", key, None)
        if status != 200:
            return {"bytes": 0, "exists": False}
        return {
            "bytes": int(headers.get("Content-Length", "0") or 0),
            "exists": True,
            "etag": headers.get("ETag", ""),
        }

    def uri(self, key: str) -> str:
        return f"cos://{self.bucket}/{key.lstrip('/')}"


# ----------------------------- driver -----------------------------


def build_backend(args: argparse.Namespace) -> ObjectStoreBackend:
    if args.backend == "local":
        root = Path(args.local_root).resolve()
        return LocalFileObjectStore(root)
    if args.backend == "cos":
        secret_id = args.cos_secret_id or os.environ.get("COS_SECRET_ID", "")
        secret_key = args.cos_secret_key or os.environ.get("COS_SECRET_KEY", "")
        bucket = args.cos_bucket or os.environ.get("COS_BUCKET", "")
        region = args.cos_region or os.environ.get("COS_REGION", "")
        endpoint = args.cos_endpoint or os.environ.get("COS_ENDPOINT", "")
        return CosObjectStore(
            secret_id=secret_id,
            secret_key=secret_key,
            bucket=bucket,
            region=region,
            endpoint=endpoint or None,
            timeout=args.cos_timeout,
            max_retries=args.cos_retries,
        )
    raise ValueError(f"unknown backend: {args.backend}")


def upload_one(
    backend: ObjectStoreBackend,
    row: Dict[str, Any],
    key_prefix: str,
    dry_run: bool,
) -> Dict[str, Any]:
    payload_ref = row["payload_ref"]
    text = row.get("text", "")
    if isinstance(text, str):
        data = text.encode("utf-8")
    else:
        data = bytes(text)
    sha = hashlib.sha256(data).hexdigest()
    object_key = derive_object_key(key_prefix, payload_ref)
    backend_uri = backend.uri(object_key)
    state = "skipped"
    error: Optional[str] = None
    if not dry_run:
        try:
            backend.put_bytes(object_key, data, sha)
            state = "remote" if backend.name == "cos" else "local"
        except Exception as e:  # noqa: BLE001
            state = "error"
            error = str(e)[:256]
    record = {
        "payload_ref": payload_ref,
        "trace_id": row.get("trace_id"),
        "span_id": row.get("span_id"),
        "payload_kind": row.get("kind"),
        "object_key": object_key,
        "backend_uri": backend_uri,
        "backend": backend.name,
        "bytes": len(data),
        "declared_bytes": row.get("bytes"),
        "sha256": sha,
        "storage_state": state,
        "uploaded_at_ns": time.time_ns() if not dry_run else 0,
    }
    if error:
        record["error"] = error
    return record


def run_upload(args: argparse.Namespace) -> Dict[str, Any]:
    payloads_path = Path(args.input)
    if not payloads_path.is_file():
        raise FileNotFoundError(f"payloads file not found: {payloads_path}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "object_manifest.jsonl"
    summary_path = out_dir / "object_store_summary.json"

    backend = build_backend(args)

    started = time.time()
    rows_iter = iter_payload_rows(payloads_path)
    if args.sample_limit and args.sample_limit > 0:
        rows_iter = (row for i, row in enumerate(rows_iter) if i < args.sample_limit)

    total = 0
    ok = 0
    err = 0
    total_bytes = 0

    with manifest_path.open("w", encoding="utf-8") as mf:
        if args.concurrency <= 1:
            for row in rows_iter:
                record = upload_one(backend, row, args.key_prefix, args.dry_run)
                mf.write(json.dumps(record, ensure_ascii=False) + "\n")
                total += 1
                total_bytes += record["bytes"]
                if record["storage_state"] == "error":
                    err += 1
                else:
                    ok += 1
                if args.progress_interval and total % args.progress_interval == 0:
                    print(f"[upload] processed={total} ok={ok} err={err}", file=sys.stderr)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = []
                for row in rows_iter:
                    futures.append(pool.submit(upload_one, backend, row, args.key_prefix, args.dry_run))
                    if len(futures) >= args.concurrency * 8:
                        for fut in concurrent.futures.as_completed(futures):
                            record = fut.result()
                            mf.write(json.dumps(record, ensure_ascii=False) + "\n")
                            total += 1
                            total_bytes += record["bytes"]
                            if record["storage_state"] == "error":
                                err += 1
                            else:
                                ok += 1
                            if args.progress_interval and total % args.progress_interval == 0:
                                print(f"[upload] processed={total} ok={ok} err={err}", file=sys.stderr)
                        futures = []
                for fut in concurrent.futures.as_completed(futures):
                    record = fut.result()
                    mf.write(json.dumps(record, ensure_ascii=False) + "\n")
                    total += 1
                    total_bytes += record["bytes"]
                    if record["storage_state"] == "error":
                        err += 1
                    else:
                        ok += 1

    elapsed = time.time() - started
    summary = {
        "command": "upload",
        "backend": backend.name,
        "input": str(payloads_path),
        "manifest": str(manifest_path),
        "key_prefix": args.key_prefix,
        "dry_run": bool(args.dry_run),
        "sample_limit": args.sample_limit,
        "concurrency": args.concurrency,
        "objects": total,
        "ok": ok,
        "errors": err,
        "bytes": total_bytes,
        "elapsed_sec": round(elapsed, 4),
        "objects_per_sec": round(total / elapsed, 2) if elapsed > 0 else 0.0,
        "bytes_per_sec": round(total_bytes / elapsed, 2) if elapsed > 0 else 0.0,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def run_verify(args: argparse.Namespace) -> Dict[str, Any]:
    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")

    backend = build_backend(args)

    started = time.time()
    sampled = 0
    matched = 0
    mismatched = 0
    missing = 0
    errors = 0

    sample_limit = args.sample_limit if args.sample_limit and args.sample_limit > 0 else None

    with manifest_path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if sample_limit is not None and sampled >= sample_limit:
                break
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if args.stride > 1 and (i % args.stride) != 0:
                continue
            sampled += 1
            object_key = record["object_key"]
            expected_sha = record["sha256"]
            expected_bytes = record["bytes"]
            try:
                head = backend.head(object_key)
                if not head.get("exists"):
                    missing += 1
                    continue
                if args.deep:
                    data = backend.get_bytes(object_key)
                    actual_sha = hashlib.sha256(data).hexdigest()
                    if actual_sha == expected_sha and len(data) == expected_bytes:
                        matched += 1
                    else:
                        mismatched += 1
                else:
                    if int(head.get("bytes", 0)) == int(expected_bytes):
                        matched += 1
                    else:
                        mismatched += 1
            except Exception:  # noqa: BLE001
                errors += 1

    elapsed = time.time() - started
    summary = {
        "command": "verify",
        "backend": backend.name,
        "manifest": str(manifest_path),
        "sampled": sampled,
        "matched": matched,
        "mismatched": mismatched,
        "missing": missing,
        "errors": errors,
        "deep": bool(args.deep),
        "stride": args.stride,
        "elapsed_sec": round(elapsed, 4),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


# ----------------------------- CLI -----------------------------


def add_backend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", choices=["local", "cos"], default="local")
    parser.add_argument("--local-root", default="bench-results/objectstore", help="Root directory for local backend")
    parser.add_argument("--cos-secret-id", default="", help="Tencent COS SecretId (or env COS_SECRET_ID)")
    parser.add_argument("--cos-secret-key", default="", help="Tencent COS SecretKey (or env COS_SECRET_KEY)")
    parser.add_argument("--cos-bucket", default="", help="Tencent COS bucket name including appid (or env COS_BUCKET)")
    parser.add_argument("--cos-region", default="", help="Tencent COS region, e.g. ap-shanghai (or env COS_REGION)")
    parser.add_argument("--cos-endpoint", default="", help="Override COS endpoint host (or env COS_ENDPOINT)")
    parser.add_argument("--cos-timeout", type=float, default=30.0)
    parser.add_argument("--cos-retries", type=int, default=5)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="TraceLSM Object Store MVP")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", help="Upload payloads.jsonl into the object store")
    up.add_argument("--input", required=True, help="Path to payloads.jsonl")
    up.add_argument("--out-dir", required=True, help="Directory for object_manifest.jsonl and summary")
    up.add_argument("--key-prefix", default="", help="Optional key prefix inside the object store")
    up.add_argument("--sample-limit", type=int, default=0, help="Only upload the first N payloads (0 = all)")
    up.add_argument("--concurrency", type=int, default=4)
    up.add_argument("--dry-run", action="store_true", help="Do not actually upload, only compute keys/sha")
    up.add_argument("--progress-interval", type=int, default=1000)
    add_backend_args(up)

    vf = sub.add_parser("verify", help="Verify object manifest against the backend")
    vf.add_argument("--manifest", required=True, help="Path to object_manifest.jsonl")
    vf.add_argument("--sample-limit", type=int, default=200, help="Only verify the first N rows after striding")
    vf.add_argument("--stride", type=int, default=1, help="Process every Nth row (after sample-limit)")
    vf.add_argument("--deep", action="store_true", help="Download and recompute sha256 instead of HEAD-only")
    add_backend_args(vf)

    args = parser.parse_args(argv)
    if args.command == "upload":
        run_upload(args)
        return 0
    if args.command == "verify":
        run_verify(args)
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
