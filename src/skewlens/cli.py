"""Command-line interface for skewlens."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .report import Risk, write_reports
from .scan import ScannerError, scan
from .synth import generate

EXIT_OK = 0
EXIT_ERROR = 2


def _print_summary(profile: dict[str, Any], risks: list[Risk], report_path: Path) -> None:
    rows = int(profile.get("rows") or 0)
    size_mb = float(profile.get("bytes") or 0) / 1048576
    print(
        f"skewlens {__version__}  {rows:,} rows  {size_mb:.1f} MB  "
        f"{profile.get('elapsed_s')} s  {profile.get('mb_per_s')} MB/s  "
        f"{profile.get('workers')} worker(s)"
    )
    print(f"{'#':>2}  {'column':<16}{'score':>6}  {'grade':<9}{'top share':>10}{'nulls':>8}")
    for index, risk in enumerate(risks, start=1):
        column = risk.profile
        top = column.get("top") or []
        share = f"{float(top[0]['share']):.1%}" if top else "-"
        count = int(column.get("count") or 0)
        null_rate = f"{int(column.get('nulls') or 0) / count:.1%}" if count else "-"
        print(
            f"{index:>2}  {risk.column:<16}{risk.score:>6.2f}  {risk.grade:<9}"
            f"{share:>10}{null_rate:>8}"
        )
    print(f"report: {report_path}")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return payload


def cmd_synth(args: argparse.Namespace) -> int:
    truth = generate(args.rows, Path(args.out), Path(args.truth), seed=args.seed)
    print(
        f"wrote {args.out}: {truth.rows:,} rows, {truth.bytes / 1048576:.1f} MB, "
        f"truth at {args.truth}"
    )
    return EXIT_OK


def cmd_scan(args: argparse.Namespace) -> int:
    profile = scan(Path(args.input), workers=args.workers)
    payload = json.dumps(profile, indent=2, ensure_ascii=False) + "\n"
    if args.profile == "-":
        sys.stdout.write(payload)
    else:
        target = Path(args.profile)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8", newline="\n")
        print(
            f"profiled {profile.get('rows'):,} rows in {profile.get('elapsed_s')} s "
            f"({profile.get('mb_per_s')} MB/s) -> {target}"
        )
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    profile = _read_json(Path(args.profile))
    truth = _read_json(Path(args.truth)) if args.truth else None
    json_path = Path(args.json) if args.json else None
    risks = write_reports(profile, truth, Path(args.out), json_path)
    _print_summary(profile, risks, Path(args.out))
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    data = workdir / "data.jsonl"
    truth_path = workdir / "truth.json"
    profile_path = workdir / "profile.json"
    report_path = workdir / "report.md"

    generate(args.rows, data, truth_path, seed=args.seed)
    profile = scan(data, workers=args.workers)
    profile_path.write_text(
        json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    risks = write_reports(profile, _read_json(truth_path), report_path, workdir / "report.json")
    _print_summary(profile, risks, report_path)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skewlens",
        description="Find the columns that will break your joins: skew, nulls, and cardinality.",
    )
    parser.add_argument("--version", action="version", version=f"skewlens {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    synth = subparsers.add_parser("synth", help="write a synthetic dataset with ground truth")
    synth.add_argument("--rows", type=int, default=100_000, help="number of rows")
    synth.add_argument("--out", default="data.jsonl", help="JSONL output path")
    synth.add_argument("--truth", default="truth.json", help="ground-truth output path")
    synth.add_argument("--seed", type=int, default=11, help="random seed")
    synth.set_defaults(func=cmd_synth)

    scan_cmd = subparsers.add_parser("scan", help="profile a JSONL file with the Node scanner")
    scan_cmd.add_argument("--input", required=True, help="JSONL file to profile")
    scan_cmd.add_argument("--profile", default="-", help="profile output path, or - for stdout")
    scan_cmd.add_argument("--workers", type=int, default=4, help="worker threads")
    scan_cmd.set_defaults(func=cmd_scan)

    report = subparsers.add_parser("report", help="rank risks and write a report")
    report.add_argument("--profile", required=True, help="profile JSON produced by scan")
    report.add_argument("--truth", default=None, help="optional ground truth for accuracy checks")
    report.add_argument("--out", default="report.md", help="markdown output path")
    report.add_argument("--json", default=None, help="optional JSON output path")
    report.set_defaults(func=cmd_report)

    run = subparsers.add_parser("run", help="generate, scan, and report in one step")
    run.add_argument("--rows", type=int, default=100_000, help="number of rows")
    run.add_argument("--workdir", default="skewlens-run", help="directory for generated files")
    run.add_argument("--workers", type=int, default=4, help="worker threads")
    run.add_argument("--seed", type=int, default=11, help="random seed")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ScannerError, ValueError, OSError) as error:
        print(f"skewlens: {error}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
