from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal

Operation = Literal["generation", "edit"]


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class JobRequest:
    operation: Operation
    prompt: str
    size: str
    quality: str
    output_format: str = "png"
    background: str = "auto"
    reference_paths: list[str] = field(default_factory=list)
    source: str = "command"


@dataclass(slots=True)
class JobOrigin:
    session: str
    platform_name: str = ""
    sender_id: str = ""
    sender_name: str = ""
    group_id: str = ""
    is_group_chat: bool = False


@dataclass(slots=True)
class ImageJob:
    job_id: str
    request: JobRequest
    origin: JobOrigin
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    output_path: str | None = None
    error: str | None = None
    queue_position: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ImageJob":
        request = JobRequest(**data["request"])
        origin = JobOrigin(**data["origin"])
        return cls(
            job_id=data["job_id"],
            request=request,
            origin=origin,
            status=JobStatus(data.get("status", JobStatus.QUEUED)),
            created_at=float(data.get("created_at", time.time())),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            output_path=data.get("output_path"),
            error=data.get("error"),
            queue_position=int(data.get("queue_position", 0)),
        )
