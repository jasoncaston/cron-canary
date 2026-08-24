# cron-canary

`cron-canary` is a local, read-only CLI that inspects scheduled-job definitions for silent-failure risk. It supports crontab files, macOS launchd property lists, and GitHub Actions workflow files. It makes no network calls, reads no secrets, and uses only the Python 3.11 standard library at runtime.

## The problem

A collection of twenty or more scheduled jobs can look calm while one of them has stopped working. Many schedulers do not notify anyone on the first failure, so the missing output goes unnoticed for days. `cron-canary` checks whether each job has a failure-notification path and whether its declared output is older than the job's expected interval.

## Design principle: silence is not a pass

If `cron-canary` cannot read or parse a requested source, it says so explicitly and exits non-zero. It never labels an unreadable source healthy, never turns an inspection error into an empty successful result, and never treats missing evidence as proof. This fail-loud rule is the core of the tool.

Risky jobs exit `1`; unreadable or invalid sources exit `2`; a fully inspected, risk-free set exits `0`.

## Install

Python 3.11 or newer is required.

```console
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Tests use pytest, which is not a runtime dependency:

```console
python -m pip install -e '.[test]'
pytest
```

## Usage

Pass one or more sources as `TYPE:PATH`:

```console
cron-canary --source crontab:./examples/crontab
cron-canary --source launchd:./launch-agents --source github-actions:./workflows
cron-canary --source github-actions:./workflows --json
```

Supported types are `crontab`, `launchd`, and `github-actions`. A path may be a single source file or a directory. For GitHub Actions, an optional source comment can declare local evidence without changing the workflow:

```yaml
# cron-canary: output=./logs/nightly.log interval=900 notify=true
```

Crontab output is detected from shell redirection. launchd output is read from `StandardOutPath` or `StandardErrorPath`. GitHub Actions has no local output target by default, so a `cron-canary` annotation is required when local mtime evidence exists.

## Example output

```text
JOB                 SOURCE          ALERT  LAST OUTPUT                INTERVAL  OVERDUE  RISK
------------------  --------------  -----  -------------------------  --------  -------  -----------------------
crontab:2            crontab         yes    2026-01-15T11:58:00+00:00  300       no       none
Nightly local check  github-actions  no     2026-01-13T00:00:00+00:00  86400     yes      no-failure-notification,output-overdue
```

The JSON form contains `jobs` and `errors` arrays. Each job includes notification presence, declared output target, last-output timestamp, expected interval, overdue status, and categorical risks.

## This tool shipped with the bug it detects.

I shipped v1 with a hole in the exact rule I said mattered most. It reported an unparseable workflow as healthy when an annotation comment looked like evidence. I found out when an independent verification pass caught it. I changed the parser to reject files without workflow structure and to treat future output timestamps as risk, not proof.

## License

Released under the MIT License. See `LICENSE`.
