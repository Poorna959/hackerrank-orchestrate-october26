#!/usr/bin/env python3
"""Ghost Data Still Exists - deletion-audit agent.

Run from the repository root:

    python3 code/main.py

Reads every participant-facing input from ``dataset/`` and writes the completed
prediction file to ``output.csv`` in the repository root.  The run is fully
deterministic and offline: no network access and no model calls are required.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from audit.engine import AuditEngine  # noqa: E402
from audit.validator import validate_output  # noqa: E402


def _repo_root() -> Path:
    return _THIS_DIR.parent


def _write_output(rows, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["case_id", "deletion_status", "steps_json"],
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit personal-data deletion requests and produce output.csv"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="dataset directory (default: <repo>/dataset)",
    )
    parser.add_argument(
        "--output",
        "--out",
        dest="output",
        type=Path,
        default=None,
        help="output CSV path (default: <repo>/output.csv); --out is an alias",
    )
    parser.add_argument(
        "--requests",
        type=Path,
        default=None,
        help="optional alternate request file (default: dataset/deletion_requests.csv)",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="skip the post-run validator (not recommended)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="reduce progress output",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help=(
            "read screenshot evidence with a local tesseract binary when one is "
            "installed (off by default so a run stays reproducible)"
        ),
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = _repo_root()
    dataset_dir = args.dataset or (repo_root / "dataset")
    output_path = args.output or (repo_root / "output.csv")
    requests_path = args.requests or (dataset_dir / "deletion_requests.csv")

    if not dataset_dir.exists():
        print(f"error: dataset directory not found: {dataset_dir}", file=sys.stderr)
        return 2

    try:
        engine = AuditEngine(dataset_dir, ocr=args.ocr)
    except Exception as exc:  # noqa: BLE001 - surface dataset problems clearly
        print(f"error: failed to load dataset: {exc}", file=sys.stderr)
        return 2

    if args.requests is not None:
        from audit.identity import DatasetError, load_requests

        try:
            engine.requests = load_requests(requests_path)
        except DatasetError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    results = engine.run()
    rows = [result.as_row() for result in sorted(results, key=lambda r: r.case_id)]
    _write_output(rows, output_path)

    if not args.quiet:
        counts = {}
        for result in results:
            counts[result.deletion_status] = counts.get(result.deletion_status, 0) + 1
        summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        print(f"wrote {len(rows)} rows to {output_path}")
        print(f"status distribution: {summary}")
        for note in engine.corpus.parse_notes[:10]:
            print(f"evidence note: {note}")

    if args.no_validate:
        return 0

    report = validate_output(
        output_path,
        requests_path,
        evidence_path=dataset_dir / "evidence_files.csv",
        activity_path=dataset_dir / "system_activity.csv",
        audit_path=dataset_dir / "user_audit_log.csv",
    )
    print(report.summary())
    for message in report.errors[:25]:
        print(f"  error: {message}", file=sys.stderr)
    for message in report.warnings[:10]:
        print(f"  warning: {message}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
