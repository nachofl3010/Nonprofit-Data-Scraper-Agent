"""Write results: output/<slug>.json (profile + views), output/profiles.csv (upserted),
and output/debug/<domain>/ (what the crawler found and what the LLM saw)."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from src.schema import NonprofitProfile
from src.views import CSV_COLUMNS

OUT_DIR = Path("output")


def slug(resolved_url: str | None, input_: str) -> str:
    """Domain for resolved sites (charitywater-org), else the raw input."""
    src = urlparse(resolved_url).netloc.removeprefix("www.") if resolved_url else input_
    return re.sub(r"[^a-z0-9]+", "-", src.lower()).strip("-")[:60] or "unnamed"


def debug_dir(resolved_url: str | None, input_: str, out_dir: Path = OUT_DIR) -> Path:
    d = out_dir / "debug" / slug(resolved_url, input_)
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_profile(p: NonprofitProfile, views: dict, out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug(p.resolved_url, p.input)}.json"
    doc = {"profile": p.model_dump(mode="json"), "views": views}
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    return path


def upsert_csv(row: dict[str, str], path: Path = OUT_DIR / "profiles.csv") -> Path:
    """Replace the row for the same website (or input when unresolved); append otherwise.
    Re-running an org updates its row instead of duplicating it."""
    key = row["website"] or row["profile_json"]
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if (r.get("website") or r.get("profile_json")) != key]
    rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
