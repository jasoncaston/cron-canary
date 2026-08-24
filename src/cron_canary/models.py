from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class JobAssessment:
    source_type: str
    source: str
    name: str
    has_failure_notification: bool
    output_target: str | None
    last_output: datetime | None
    expected_interval_seconds: int | None
    overdue: bool | None
    risks: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.risks

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["last_output"] = self.last_output.isoformat() if self.last_output else None
        value["risks"] = list(self.risks)
        value["healthy"] = self.healthy
        return value


@dataclass(frozen=True)
class InspectionError:
    source_type: str
    source: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class SourceReadError(RuntimeError):
    def __init__(self, source_type: str, source: str, message: str) -> None:
        super().__init__(message)
        self.source_type = source_type
        self.source = source
        self.message = message

    def as_error(self) -> InspectionError:
        return InspectionError(self.source_type, self.source, self.message)
