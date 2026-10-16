"""Lossless raw-source archive and receipt assessment; no acquisition/retries."""

from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def main():
    manifest = []
    for path in sorted((ROOT / "sources").iterdir()):
        if path.suffix == ".gz":
            continue
        original = path.read_bytes()
        archive = path.with_name(path.name + ".gz")
        archive.write_bytes(gzip.compress(original, mtime=0))
        path.unlink()  # Only this task's captured public copies; byte-exact gzip kept.
    for archive in sorted((ROOT / "sources").glob("*.gz")):
        original = gzip.decompress(archive.read_bytes())
        manifest.append(
            {
                "original_path": str(archive.relative_to(ROOT))[:-3],
                "archive_path": str(archive.relative_to(ROOT)),
                "raw_sha256": hashlib.sha256(original).hexdigest(),
                "raw_bytes": len(original),
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "archive_bytes": archive.stat().st_size,
            }
        )
    (ROOT / "source-archives.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    for filename in ("calendar.disabled.json", "reused-sources.json"):
        path = ROOT / filename
        value = json.loads(path.read_text())
        if filename == "calendar.disabled.json":
            value["official_holiday_evidence"] = [
                p if p.endswith(".gz") else p + ".gz"
                for p in value["official_holiday_evidence"]
            ]
            p = value["official_session_evidence"]
            value["official_session_evidence"] = p if p.endswith(".gz") else p + ".gz"
        else:
            for source in value:
                if source.get("path", "").startswith("sources/"):
                    source["archive_path"] = source["path"] + ".gz"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    captures = [
        json.loads(x) for x in (ROOT / "acquisitions.jsonl").read_text().splitlines()
    ]
    assessed = []
    for row in captures:
        r = dict(row)
        r["hostname"] = urlsplit(r["url"]).hostname
        if r["status"] == 200:
            r["archive_path"] = r["path"] + ".gz"
            original = gzip.decompress((ROOT / r["archive_path"]).read_bytes())
            assert hashlib.sha256(original).hexdigest() == r["sha256"]
            r["content_assessment"] = (
                "rejected_WAF_not_business_data"
                if "WEB 应用防火墙" in original.decode("utf-8", errors="replace")
                else "captured_public_source_see_field_validation"
            )
        else:
            r["content_assessment"] = "no_business_document_obtained"
            r.setdefault("http_error_status", None)
        assessed.append(r)
    result = {
        "http_capture_attempts": len(captures),
        "counts_by_host": dict(Counter(r["hostname"] for r in assessed)),
        "status_counts": dict(Counter(str(r["status"]) for r in assessed)),
        "attempts": assessed,
        "limits": "explicit exchange URL allowlist; 18-second socket timeout; 3MB/body; 2GB process virtual memory; at most 3 download workers",
        "http_error_status_note": "early failed captures retained exception type but not numeric HTTP error status; null means not captured, never assumed",
        "web_search_note": "separate web search/tool retrieval used only for link discovery; cached results were not certified as original response bytes",
        "browser_launched": False,
        "paid_source_or_authenticated_vendor_used": False,
    }
    (ROOT / "acquisition-assessment.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k in ("http_capture_attempts", "counts_by_host", "status_counts")
            }
        )
    )


if __name__ == "__main__":
    main()
