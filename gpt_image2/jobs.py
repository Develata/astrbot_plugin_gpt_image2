from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Protocol

from .client import GPTImage2Client, extract_image_payload, save_b64_image
from .config import PluginConfig
from .errors import format_job_error
from .models import ImageJob, JobOrigin, JobRequest, JobStatus


class MessageSender(Protocol):
    async def send_text(self, session: str, text: str) -> None: ...
    async def send_image(self, session: str, path_or_url: str, caption: str | None = None) -> None: ...


class JobManager:
    def __init__(
        self,
        *,
        config: PluginConfig,
        client: GPTImage2Client,
        sender: MessageSender,
        data_dir: Path,
    ) -> None:
        self.config = config
        self.client = client
        self.sender = sender
        self.data_dir = data_dir
        self.output_dir = data_dir / config.runtime.output_dir
        self.state_path = data_dir / config.runtime.state_file
        self.jobs: dict[str, ImageJob] = {}
        self.queue: deque[str] = deque()
        self._worker_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_outputs()
        await self.load_state()
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop(), name="gpt-image2-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        self._changed.set()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        await self.save_state()

    async def enqueue(self, request: JobRequest, origin: JobOrigin) -> ImageJob:
        async with self._lock:
            active_count = sum(1 for job in self.jobs.values() if not job.is_terminal)
            if active_count >= self.config.runtime.queue_max_size:
                raise RuntimeError(f"图片任务队列已满：{active_count}/{self.config.runtime.queue_max_size}")
            pending_same_user = sum(
                1
                for job in self.jobs.values()
                if job.origin.sender_id == origin.sender_id
                and job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
            )
            if pending_same_user >= self.config.runtime.per_user_queue_max_size:
                raise RuntimeError(
                    f"当前用户图片任务过多：{pending_same_user}/{self.config.runtime.per_user_queue_max_size}，请等待已有任务完成"
                )
            job_id = self._new_job_id(request.operation)
            job = ImageJob(job_id=job_id, request=request, origin=origin)
            self.jobs[job_id] = job
            self.queue.append(job_id)
            self._refresh_queue_positions()
            await self.save_state()
            self._changed.set()
            return job

    async def cancel(self, job_id: str) -> ImageJob | None:
        async with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
                try:
                    self.queue.remove(job_id)
                except ValueError:
                    pass
                self._refresh_queue_positions()
                await self.save_state()
                self._changed.set()
            return job

    def get(self, job_id: str) -> ImageJob | None:
        return self.jobs.get(job_id)

    def list_recent(self, limit: int = 10) -> list[ImageJob]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]

    async def load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            loaded = [ImageJob.from_json(item) for item in raw.get("jobs", [])]
        except Exception:
            return
        now = time.time()
        ttl = self.config.runtime.job_ttl_hours * 3600
        for job in loaded:
            if now - job.created_at > ttl:
                continue
            if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
                job.status = JobStatus.FAILED
                job.finished_at = now
                job.error = "插件重启时任务仍未完成；为避免重复提交，已标记为失败。"
            self.jobs[job.job_id] = job

    async def save_state(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        data = {"jobs": [job.to_json() for job in self.list_recent(limit=200)]}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _cleanup_old_outputs(self) -> None:
        """Best-effort output cache cleanup, inspired by OmniDraw's cache discipline."""
        ttl = self.config.runtime.job_ttl_hours * 3600
        cutoff = time.time() - ttl
        for path in self.output_dir.glob("*"):
            if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            job = await self._next_job()
            if not job:
                self._changed.clear()
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                continue
            await self._run_job(job)

    async def _next_job(self) -> ImageJob | None:
        async with self._lock:
            running = sum(1 for job in self.jobs.values() if job.status == JobStatus.RUNNING)
            if running >= self.config.runtime.global_max_concurrent:
                return None
            while self.queue:
                job_id = self.queue.popleft()
                job = self.jobs.get(job_id)
                if not job or job.status != JobStatus.QUEUED:
                    continue
                job.status = JobStatus.RUNNING
                job.started_at = time.time()
                self._refresh_queue_positions()
                await self.save_state()
                return job
            return None

    async def _run_job(self, job: ImageJob) -> None:
        if self.config.runtime.send_start_message:
            try:
                await self.sender.send_text(job.origin.session, self._start_message(job))
            except Exception:
                # A transient notice-delivery failure must not kill the worker or
                # prevent the actual image request from running.
                pass
        stage = "api_request"
        try:
            if job.request.operation == "generation":
                response = await self.client.generate(
                    prompt=job.request.prompt,
                    size=job.request.size,
                    quality=job.request.quality,
                    output_format=job.request.output_format,
                    background=job.request.background,
                )
            else:
                response = await self.client.edit(
                    prompt=job.request.prompt,
                    image_paths=job.request.reference_paths,
                    size=job.request.size,
                    quality=job.request.quality,
                    output_format=job.request.output_format,
                    background=job.request.background,
                )
            stage = "parse_response"
            kind, value = extract_image_payload(response)
            if kind == "b64_json":
                suffix = job.request.output_format if job.request.output_format in {"png", "jpeg", "jpg", "webp"} else "png"
                output = self.output_dir / f"{job.job_id}.{suffix}"
                save_b64_image(value, output)
                job.output_path = str(output)
            else:
                job.output_path = value
            job.status = JobStatus.SUCCEEDED
            job.finished_at = time.time()
            await self.save_state()
            stage = "deliver_image"
            caption = self._finish_caption(job) if self.config.runtime.send_finish_message else None
            await self.sender.send_image(job.origin.session, job.output_path, caption=caption)
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            job.error = str(exc)
            await self.save_state()
            try:
                await self.sender.send_text(job.origin.session, format_job_error(job.job_id, stage, exc))
            except Exception:
                pass

    def _refresh_queue_positions(self) -> None:
        for idx, job_id in enumerate(self.queue, start=1):
            job = self.jobs.get(job_id)
            if job:
                job.queue_position = idx

    def _new_job_id(self, operation: str) -> str:
        prefix = "edit" if operation == "edit" else "img"
        return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    def _start_message(self, job: ImageJob) -> str:
        return (
            "🎨 GPT Image 2 任务开始处理\n"
            f"job_id: {job.job_id}\n"
            f"type: {job.request.operation}\n"
            f"size: {job.request.size}\n"
            f"quality: {job.request.quality}"
        )

    def _finish_caption(self, job: ImageJob) -> str:
        elapsed = ""
        if job.started_at and job.finished_at:
            elapsed = f"\nelapsed: {job.finished_at - job.started_at:.1f}s"
        return f"✅ GPT Image 2 任务完成\njob_id: {job.job_id}{elapsed}"


def describe_job(job: ImageJob) -> str:
    lines = [
        f"job_id: {job.job_id}",
        f"status: {job.status}",
        f"type: {job.request.operation}",
        f"size: {job.request.size}",
        f"quality: {job.request.quality}",
    ]
    if job.queue_position:
        lines.append(f"queue_position: {job.queue_position}")
    if job.output_path:
        lines.append(f"output: {job.output_path}")
    if job.error:
        lines.append(f"error: {job.error}")
    return "\n".join(lines)
