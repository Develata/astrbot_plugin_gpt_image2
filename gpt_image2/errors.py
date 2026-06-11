from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class GPTImage2Error(RuntimeError):
    """Base error for user-visible GPT Image 2 failures."""


@dataclass(slots=True)
class APIStatusError(GPTImage2Error):
    status: int
    body: str
    content_type: str = ""

    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.body}"


def redact_secret(text: str) -> str:
    patterns = [
        (r"sk-[A-Za-z0-9_\-]{12,}", "<redacted>"),
        (r"Bearer\s+[A-Za-z0-9_\-.]{12,}", "Bearer <redacted>"),
        (r"Authorization:\s*[^\n\r]+", "Authorization: <redacted>"),
        (r"([?&](?:api[_-]?key|key|token|access[_-]?token)=)[^&\s]+", r"\1<redacted>"),
    ]
    out = text
    for pattern, replacement in patterns:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    return out


def compact_error_body(body: bytes | str, limit: int = 2000) -> str:
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = body
    text = redact_secret(text.strip())
    try:
        obj: Any = json.loads(text)
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        pass
    if len(text) > limit:
        return text[:limit] + "\n...<truncated>"
    return text


def format_job_error(job_id: str, stage: str, error: BaseException) -> str:
    raw = compact_error_body(str(error), limit=2200)
    return (
        "💥 GPT Image 2 任务失败\n\n"
        f"job_id: {job_id}\n"
        f"stage: {stage}\n"
        "error:\n"
        f"{raw}"
    )
