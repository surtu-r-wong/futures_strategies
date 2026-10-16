"""Bounded, public-only evidence download. No credentials, DB or account code."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
ALLOWED = (
    "shfe.com.cn",
    "shfe.cn",
    "ine.cn",
    "dce.com.cn",
    "czce.com.cn",
    "gfex.com.cn",
)


def fetch(source_id, url, *, direct=False):
    host = urllib.parse.urlsplit(url).hostname or ""
    if not any(host == d or host.endswith("." + d) for d in ALLOWED):
        raise ValueError("only scoped public exchange hosts allowed")
    started = datetime.now(timezone.utc).isoformat()
    record = {
        "source_id": source_id,
        "url": url,
        "started_at": started,
        "original_published_at": None,
        "economic_valid_from": None,
        "economic_valid_to": None,
        "transport": "direct_public" if direct else "default_public",
    }
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
        )
        opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if direct
            else urllib.request.build_opener()
        )
        with opener.open(req, timeout=18) as response:
            raw = response.read(3_000_001)
            if len(raw) > 3_000_000:
                raise ValueError("public document exceeds scoped 3MB limit")
            suffix = (
                ".json"
                if "json" in response.headers.get("Content-Type", "")
                else ".html"
            )
            path = ROOT / "sources" / (source_id + suffix)
            path.parent.mkdir(exist_ok=True)
            if path.exists():
                raise ValueError("source id already captured; use a new id")
            path.write_bytes(raw)
            record.update(
                status=response.status,
                final_url=response.url,
                content_type=response.headers.get("Content-Type"),
                http_last_modified=response.headers.get("Last-Modified"),
                path=str(path.relative_to(ROOT)),
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
    except Exception as exc:
        record.update(
            error_type=type(exc).__name__,
            status="fetch_failed",
            http_error_status=getattr(exc, "code", None),
        )
    record["acquired_at"] = datetime.now(timezone.utc).isoformat()
    with (ROOT / "acquisitions.jsonl").open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source_id")
    parser.add_argument("url")
    parser.add_argument("--direct", action="store_true")
    args = parser.parse_args()
    fetch(args.source_id, args.url, direct=args.direct)
