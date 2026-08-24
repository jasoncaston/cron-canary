from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .models import InspectionError, JobAssessment, SourceReadError
from .parsers import SOURCE_TYPES, inspect_source


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cron-canary",
        description="Inspect scheduled-job definitions for silent-failure risk.",
    )
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="TYPE:PATH",
        help="source type and file/directory path; repeatable",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--now", help="ISO-8601 time used for overdue checks")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
    except ValueError as exc:
        build_parser().error(f"invalid --now value: {exc}")

    jobs: list[JobAssessment] = []
    errors: list[InspectionError] = []
    for spec in args.source:
        try:
            source_type, path = _parse_source_spec(spec)
            jobs.extend(inspect_source(source_type, path, now=now))
        except (SourceReadError, ValueError) as exc:
            if isinstance(exc, SourceReadError):
                errors.append(exc.as_error())
            else:
                errors.append(InspectionError("unknown", spec, str(exc)))

    if args.json:
        print(
            json.dumps(
                {
                    "jobs": [job.to_dict() for job in jobs],
                    "errors": [error.to_dict() for error in errors],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        if jobs:
            print(render_table(jobs))
        else:
            print("No jobs inspected.")
        for error in errors:
            print(
                f"ERROR: could not inspect {error.source_type} source {error.source}: {error.message}",
                file=sys.stderr,
            )

    if errors:
        return 2
    return 1 if any(not job.healthy for job in jobs) else 0


def _parse_source_spec(spec: str) -> tuple[str, Path]:
    if ":" not in spec:
        raise ValueError("source must use TYPE:PATH")
    source_type, raw_path = spec.split(":", 1)
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unsupported source type: {source_type}")
    if not raw_path:
        raise ValueError("source path is empty")
    return source_type, Path(raw_path)


def render_table(jobs: list[JobAssessment]) -> str:
    headers = ("JOB", "SOURCE", "ALERT", "LAST OUTPUT", "INTERVAL", "OVERDUE", "RISK")
    rows = []
    for job in jobs:
        rows.append(
            (
                job.name,
                job.source_type,
                "yes" if job.has_failure_notification else "no",
                job.last_output.isoformat(timespec="seconds") if job.last_output else "unknown",
                str(job.expected_interval_seconds) if job.expected_interval_seconds else "unknown",
                "unknown" if job.overdue is None else ("yes" if job.overdue else "no"),
                ",".join(job.risks) if job.risks else "none",
            )
        )
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) for index in range(len(headers))]
    line = "  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))
    divider = "  ".join("-" * width for width in widths)
    body = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows]
    return "\n".join([line, divider, *body])


if __name__ == "__main__":
    raise SystemExit(main())
