from __future__ import annotations

import json
import os
import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cron_canary.cli import main
from cron_canary.models import SourceReadError
from cron_canary.parsers import inspect_source

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def set_mtime(path: Path, value: datetime) -> None:
    timestamp = value.timestamp()
    os.utime(path, (timestamp, timestamp))


@pytest.fixture
def healthy_job(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "healthy.log"
    log.write_text("completed\n", encoding="utf-8")
    set_mtime(log, NOW - timedelta(minutes=2))
    source = tmp_path / "crontab"
    source.write_text(f"MAILTO=alerts@example.invalid\n*/5 * * * * run-task >> {log} 2>&1\n", encoding="utf-8")
    return source, log


@pytest.fixture
def no_alerting_job(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "quiet.log"
    log.write_text("completed\n", encoding="utf-8")
    set_mtime(log, NOW - timedelta(minutes=3))
    source = tmp_path / "crontab"
    source.write_text(f"*/10 * * * * run-task >> {log} 2>&1\n", encoding="utf-8")
    return source, log


@pytest.fixture
def overdue_job(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "late.log"
    log.write_text("old output\n", encoding="utf-8")
    set_mtime(log, NOW - timedelta(minutes=11))
    source = tmp_path / "crontab"
    source.write_text(f"MAILTO=alerts@example.invalid\n*/5 * * * * run-task >> {log} 2>&1\n", encoding="utf-8")
    return source, log


@pytest.fixture
def unreadable_source(tmp_path: Path) -> Path:
    source = tmp_path / "blocked.cron"
    source.write_text("*/5 * * * * run-task\n", encoding="utf-8")
    source.chmod(0)
    yield source
    source.chmod(0o600)


def test_healthy_job_uses_real_schedule_and_mtime(healthy_job: tuple[Path, Path]) -> None:
    source, log = healthy_job
    job = inspect_source("crontab", source, now=NOW)[0]
    assert job.has_failure_notification is True
    assert job.output_target == str(log)
    assert job.last_output == NOW - timedelta(minutes=2)
    assert job.expected_interval_seconds == 300
    assert job.overdue is False
    assert job.risks == ()


def test_job_without_alerting_names_the_risk(no_alerting_job: tuple[Path, Path]) -> None:
    source, log = no_alerting_job
    job = inspect_source("crontab", source, now=NOW)[0]
    assert job.output_target == str(log)
    assert job.last_output == NOW - timedelta(minutes=3)
    assert job.expected_interval_seconds == 600
    assert job.has_failure_notification is False
    assert job.risks == ("no-failure-notification",)


def test_job_past_interval_uses_output_age(overdue_job: tuple[Path, Path]) -> None:
    source, log = overdue_job
    job = inspect_source("crontab", source, now=NOW)[0]
    assert job.output_target == str(log)
    assert job.last_output == NOW - timedelta(minutes=11)
    assert job.expected_interval_seconds == 300
    assert job.overdue is True
    assert job.risks == ("output-overdue",)


def test_crontab_future_output_timestamp_is_a_risk(tmp_path: Path) -> None:
    log = tmp_path / "future-crontab.log"
    log.write_text("future evidence\n", encoding="utf-8")
    set_mtime(log, NOW + timedelta(minutes=5))
    source = tmp_path / "crontab"
    source.write_text(
        f"MAILTO=alerts@example.invalid\n*/5 * * * * run-task >> {log} 2>&1\n",
        encoding="utf-8",
    )

    job = inspect_source("crontab", source, now=NOW)[0]
    assert job.name == "crontab:2"
    assert job.output_target == str(log)
    assert job.last_output == NOW + timedelta(minutes=5)
    assert job.expected_interval_seconds == 300
    assert job.overdue is None
    assert job.risks == ("output-timestamp-in-future",)


def test_unreadable_source_is_explicit_and_never_healthy(unreadable_source: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SourceReadError) as captured:
        inspect_source("crontab", unreadable_source, now=NOW)
    assert captured.value.source == str(unreadable_source)
    assert captured.value.message == "cannot read source: permission denied"

    exit_code = main(["--source", f"crontab:{unreadable_source}", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["jobs"] == []
    assert payload["errors"] == [
        {
            "message": "cannot read source: permission denied",
            "source": str(unreadable_source),
            "source_type": "crontab",
        }
    ]


def test_launchd_values_are_parsed_from_plist(tmp_path: Path) -> None:
    log = tmp_path / "launchd.log"
    log.write_text("done\n", encoding="utf-8")
    set_mtime(log, NOW - timedelta(seconds=30))
    source = tmp_path / "example.plist"
    with source.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "scheduled-job-example",
                "ProgramArguments": ["run-task"],
                "StartInterval": 60,
                "StandardOutPath": str(log),
                "EnvironmentVariables": {"CRON_CANARY_NOTIFY": "local-alert"},
            },
            handle,
        )
    job = inspect_source("launchd", source, now=NOW)[0]
    assert job.name == "scheduled-job-example"
    assert job.expected_interval_seconds == 60
    assert job.last_output == NOW - timedelta(seconds=30)
    assert job.has_failure_notification is True
    assert job.risks == ()


def test_launchd_future_output_timestamp_is_a_risk(tmp_path: Path) -> None:
    log = tmp_path / "future-launchd.log"
    log.write_text("future evidence\n", encoding="utf-8")
    set_mtime(log, NOW + timedelta(minutes=5))
    source = tmp_path / "future.plist"
    with source.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "future-launchd-job",
                "ProgramArguments": ["run-task"],
                "StartInterval": 300,
                "StandardOutPath": str(log),
                "EnvironmentVariables": {"CRON_CANARY_NOTIFY": "local-alert"},
            },
            handle,
        )

    job = inspect_source("launchd", source, now=NOW)[0]
    assert job.name == "future-launchd-job"
    assert job.output_target == str(log)
    assert job.last_output == NOW + timedelta(minutes=5)
    assert job.expected_interval_seconds == 300
    assert job.overdue is None
    assert job.risks == ("output-timestamp-in-future",)


def test_github_actions_values_are_parsed_from_workflow(tmp_path: Path) -> None:
    log = tmp_path / "workflow.log"
    log.write_text("done\n", encoding="utf-8")
    set_mtime(log, NOW - timedelta(minutes=5))
    source = tmp_path / "scheduled.yml"
    source.write_text(
        """name: Scheduled check
on:
  schedule:
    - cron: '*/15 * * * *'
# cron-canary: output=workflow.log notify=true
jobs:
  check:
    runs-on: local
    steps:
      - run: run-task
""",
        encoding="utf-8",
    )
    job = inspect_source("github-actions", source, now=NOW)[0]
    assert job.name == "Scheduled check"
    assert job.output_target == str(log)
    assert job.expected_interval_seconds == 900
    assert job.last_output == NOW - timedelta(minutes=5)
    assert job.has_failure_notification is True
    assert job.risks == ()


def test_github_actions_annotation_without_workflow_fails_loud(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "annotation-only.yml"
    source.write_text(
        "# cron-canary: output=workflow.log interval=300 notify=true\n",
        encoding="utf-8",
    )
    message = f"cannot inspect workflow {source.name}: no recognizable workflow structure"

    with pytest.raises(SourceReadError) as captured:
        inspect_source("github-actions", source, now=NOW)
    assert captured.value.source_type == "github-actions"
    assert captured.value.source == str(source)
    assert captured.value.message == message

    exit_code = main(["--source", f"github-actions:{source}", "--json", "--now", NOW.isoformat()])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["jobs"] == []
    assert payload["errors"] == [
        {"message": message, "source": str(source), "source_type": "github-actions"}
    ]


def test_github_actions_future_output_timestamp_is_a_risk(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log = tmp_path / "future.log"
    log.write_text("future evidence\n", encoding="utf-8")
    set_mtime(log, NOW + timedelta(minutes=5))
    source = tmp_path / "future.yml"
    source.write_text(
        """name: Future evidence
on:
  schedule:
    - cron: '*/5 * * * *'
# cron-canary: output=future.log notify=true
jobs:
  check:
    runs-on: local
    steps:
      - run: run-task
""",
        encoding="utf-8",
    )

    job = inspect_source("github-actions", source, now=NOW)[0]
    assert job.name == "Future evidence"
    assert job.output_target == str(log)
    assert job.last_output == NOW + timedelta(minutes=5)
    assert job.expected_interval_seconds == 300
    assert job.overdue is None
    assert job.risks == ("output-timestamp-in-future",)

    exit_code = main(["--source", f"github-actions:{source}", "--json", "--now", NOW.isoformat()])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["errors"] == []
    assert payload["jobs"][0]["last_output"] == "2026-01-15T12:05:00+00:00"
    assert payload["jobs"][0]["overdue"] is None
    assert payload["jobs"][0]["risks"] == ["output-timestamp-in-future"]
    assert payload["jobs"][0]["healthy"] is False


def test_github_actions_unstructured_yml_without_annotation_fails_loud(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "not-a-workflow.yml"
    source.write_text("name: Just a document\nvalue: true\n", encoding="utf-8")
    message = f"cannot inspect workflow {source.name}: no recognizable workflow structure"

    with pytest.raises(SourceReadError) as captured:
        inspect_source("github-actions", source, now=NOW)
    assert captured.value.source_type == "github-actions"
    assert captured.value.source == str(source)
    assert captured.value.message == message

    exit_code = main(["--source", f"github-actions:{source}", "--json", "--now", NOW.isoformat()])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["jobs"] == []
    assert payload["errors"] == [
        {"message": message, "source": str(source), "source_type": "github-actions"}
    ]
