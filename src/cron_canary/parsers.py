from __future__ import annotations

import os
import plistlib
import re
import shlex
import stat
from datetime import datetime, timezone
from pathlib import Path

from .models import JobAssessment, SourceReadError

SOURCE_TYPES = {"crontab", "launchd", "github-actions"}
_OUTPUT_RE = re.compile(r"(?<!\d)(?:>>|>)\s*(?!&)([^\s;|]+)")
_NOTIFY_RE = re.compile(r"\b(mail|sendmail|notify|alert|pager|webhook|slack)\b", re.I)


def inspect_source(
    source_type: str,
    source: str | Path,
    *,
    now: datetime | None = None,
) -> list[JobAssessment]:
    """Inspect one file or a directory of supported source files."""
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unsupported source type: {source_type}")

    path = Path(source)
    _require_readable(source_type, path)
    now = _normalize_now(now)

    if path.is_dir():
        paths = _source_files(source_type, path)
        if not paths:
            raise SourceReadError(source_type, str(path), "no supported source files found")
    else:
        paths = [path]

    jobs: list[JobAssessment] = []
    for item in paths:
        _require_readable(source_type, item)
        if source_type == "crontab":
            jobs.extend(_parse_crontab(item, now))
        elif source_type == "launchd":
            jobs.append(_parse_launchd(item, now))
        else:
            jobs.append(_parse_github_actions(item, now))
    return jobs


def _source_files(source_type: str, directory: Path) -> list[Path]:
    suffixes = {
        "crontab": None,
        "launchd": {".plist"},
        "github-actions": {".yml", ".yaml"},
    }[source_type]
    entries = sorted(item for item in directory.iterdir() if item.is_file() and not item.name.startswith("."))
    if suffixes is None:
        return entries
    return [item for item in entries if item.suffix.lower() in suffixes]


def _require_readable(source_type: str, path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise SourceReadError(source_type, str(path), f"cannot read source: {exc}") from exc
    if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
        raise SourceReadError(source_type, str(path), "cannot read source: not a regular file or directory")
    if mode & 0o444 == 0 or not os.access(path, os.R_OK):
        raise SourceReadError(source_type, str(path), "cannot read source: permission denied")


def _read_text(source_type: str, path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SourceReadError(source_type, str(path), f"cannot read source: {exc}") from exc


def _parse_crontab(path: Path, now: datetime) -> list[JobAssessment]:
    text = _read_text("crontab", path)
    mailto: str | None = None
    jobs: list[JobAssessment] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assignment = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if assignment:
            if assignment.group(1) == "MAILTO":
                mailto = assignment.group(2).strip().strip('"\'') or None
            continue

        schedule, command = _split_cron_line(line)
        if schedule is None:
            raise SourceReadError(
                "crontab",
                str(path),
                f"cannot inspect crontab line {line_number}: invalid schedule",
            )
        interval = _cron_interval_seconds(schedule)
        output = _declared_output(command)
        notification = bool(mailto) or bool(_NOTIFY_RE.search(command) and "||" in command)
        jobs.append(
            _assessment(
                source_type="crontab",
                source=path,
                name=f"{path.name}:{line_number}",
                notification=notification,
                output=output,
                interval=interval,
                now=now,
            )
        )
    return jobs


def _split_cron_line(line: str) -> tuple[str | None, str]:
    if line.startswith("@"):
        parts = line.split(None, 1)
        return (parts[0], parts[1]) if len(parts) == 2 else (None, "")
    parts = line.split(None, 5)
    if len(parts) != 6:
        return None, ""
    return " ".join(parts[:5]), parts[5]


def _cron_interval_seconds(schedule: str) -> int | None:
    macros = {
        "@hourly": 3600,
        "@daily": 86400,
        "@midnight": 86400,
        "@weekly": 604800,
        "@monthly": 2_419_200,
        "@yearly": 31_536_000,
        "@annually": 31_536_000,
    }
    if schedule in macros:
        return macros[schedule]
    fields = schedule.strip("'\"").split()
    if len(fields) != 5:
        return None
    minute, hour, day, month, weekday = fields
    minute_step = re.fullmatch(r"\*/(\d+)", minute)
    if minute_step and all(value == "*" for value in (hour, day, month, weekday)):
        return int(minute_step.group(1)) * 60
    hour_step = re.fullmatch(r"\*/(\d+)", hour)
    if minute.isdigit() and hour_step and all(value == "*" for value in (day, month, weekday)):
        return int(hour_step.group(1)) * 3600
    if minute.isdigit() and hour == "*" and all(value == "*" for value in (day, month, weekday)):
        return 3600
    if minute.isdigit() and hour.isdigit() and all(value == "*" for value in (day, month, weekday)):
        return 86400
    if minute.isdigit() and hour.isdigit() and day == "*" and month == "*" and weekday != "*":
        return 604800
    return None


def _parse_launchd(path: Path, now: datetime) -> JobAssessment:
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise SourceReadError("launchd", str(path), f"cannot read source: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceReadError("launchd", str(path), "cannot inspect plist: root is not a dictionary")

    name = str(data.get("Label") or path.stem)
    interval = _launchd_interval(data)
    output = data.get("StandardOutPath") or data.get("StandardErrorPath")
    output = str(output) if output else None
    args = " ".join(str(value) for value in data.get("ProgramArguments", []))
    environment = data.get("EnvironmentVariables", {})
    notification = bool(isinstance(environment, dict) and environment.get("CRON_CANARY_NOTIFY"))
    notification = notification or bool(_NOTIFY_RE.search(args) and "||" in args)
    return _assessment(
        source_type="launchd",
        source=path,
        name=name,
        notification=notification,
        output=output,
        interval=interval,
        now=now,
    )


def _launchd_interval(data: dict[str, object]) -> int | None:
    value = data.get("StartInterval")
    if isinstance(value, int) and value > 0:
        return value
    calendar = data.get("StartCalendarInterval")
    if isinstance(calendar, dict):
        return 86400
    if isinstance(calendar, list) and calendar:
        return max(60, 86400 // len(calendar))
    return None


def _parse_github_actions(path: Path, now: datetime) -> JobAssessment:
    text = _read_text("github-actions", path)
    if not re.search(r"(?m)^\s*(?:on|schedule|jobs)\s*:", text):
        raise SourceReadError(
            "github-actions",
            str(path),
            f"cannot inspect workflow {path.name}: no recognizable workflow structure",
        )
    name_match = re.search(r"(?m)^name:\s*['\"]?([^'\"#\n]+)", text)
    name = name_match.group(1).strip() if name_match else path.stem
    cron_match = re.search(r"(?m)^\s*-?\s*cron:\s*['\"]([^'\"]+)['\"]", text)
    interval = _cron_interval_seconds(cron_match.group(1)) if cron_match else None

    annotation: dict[str, str] = {}
    for match in re.finditer(r"(?m)^\s*#\s*cron-canary:\s*(.+)$", text):
        for token in shlex.split(match.group(1)):
            if "=" in token:
                key, value = token.split("=", 1)
                annotation[key.lower()] = value
    if annotation.get("interval", "").isdigit():
        interval = int(annotation["interval"])
    output = annotation.get("output")
    annotated_notify = annotation.get("notify", "").lower() in {"1", "true", "yes"}
    failure_condition = bool(re.search(r"failure\s*\(\s*\)", text, re.I))
    notification = annotated_notify or bool(failure_condition and _NOTIFY_RE.search(text))
    return _assessment(
        source_type="github-actions",
        source=path,
        name=name,
        notification=notification,
        output=output,
        interval=interval,
        now=now,
    )


def _declared_output(command: str) -> str | None:
    match = _OUTPUT_RE.search(command)
    return match.group(1).strip('"\'') if match else None


def _assessment(
    *,
    source_type: str,
    source: Path,
    name: str,
    notification: bool,
    output: str | None,
    interval: int | None,
    now: datetime,
) -> JobAssessment:
    output_path = _resolve_output(source, output) if output else None
    last_output: datetime | None = None
    overdue: bool | None = None
    risks: list[str] = []

    if not notification:
        risks.append("no-failure-notification")
    if interval is None:
        risks.append("interval-unknown")
    if output_path is None:
        risks.append("no-output-target")
    elif output_path.exists():
        last_output = datetime.fromtimestamp(output_path.stat().st_mtime, tz=timezone.utc)
        if last_output > now:
            risks.append("output-timestamp-in-future")
        else:
            overdue = None if interval is None else (now - last_output).total_seconds() > interval
            if overdue:
                risks.append("output-overdue")
    else:
        overdue = True if interval is not None else None
        risks.append("output-missing")

    return JobAssessment(
        source_type=source_type,
        source=str(source),
        name=name,
        has_failure_notification=notification,
        output_target=str(output_path) if output_path else None,
        last_output=last_output,
        expected_interval_seconds=interval,
        overdue=overdue,
        risks=tuple(risks),
    )


def _resolve_output(source: Path, output: str) -> Path:
    path = Path(output).expanduser()
    return path if path.is_absolute() else source.parent / path


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)
