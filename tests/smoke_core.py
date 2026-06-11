from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from gpt_image2.client import extract_image_payload, save_b64_image
from gpt_image2.config import PluginConfig
from gpt_image2.errors import redact_secret
from gpt_image2.jobs import JobManager
from gpt_image2.models import JobOrigin, JobRequest, JobStatus


class FakeClient:
    async def generate(self, **kwargs):
        return {"data": [{"b64_json": base64.b64encode(b"png-bytes").decode()}]}

    async def edit(self, **kwargs):
        return {"data": [{"url": "https://example.test/out.png"}]}


class FakeSender:
    def __init__(self, fail_image: bool = False) -> None:
        self.texts = []
        self.images = []
        self.fail_image = fail_image

    async def send_text(self, session, text):
        self.texts.append((session, text))

    async def send_image(self, session, path_or_url, caption=None):
        self.images.append((session, path_or_url, caption))
        if self.fail_image:
            raise RuntimeError("delivery failed")


async def wait_terminal(manager: JobManager, job_id: str, timeout: float = 3):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = manager.get(job_id)
        if job and job.is_terminal:
            return job
        await asyncio.sleep(0.05)
    raise TimeoutError(job_id)


async def test_success_send_image_even_without_finish_caption() -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg = PluginConfig.from_mapping(
            {
                "api": {"base_url": "https://example.test/v1", "api_key": "sk-tes...9012"},
                "runtime": {"send_finish_message": False, "send_start_message": False},
            }
        )
        sender = FakeSender()
        manager = JobManager(config=cfg, client=FakeClient(), sender=sender, data_dir=Path(td))
        await manager.start()
        try:
            job = await manager.enqueue(
                JobRequest(
                    operation="generation",
                    prompt="black cat",
                    size="1536x1024",
                    quality="medium",
                    source="test",
                ),
                JobOrigin(session="telegram:chat", sender_id="u1", platform_name="telegram"),
            )
            done = await wait_terminal(manager, job.job_id)
        finally:
            await manager.stop()
        assert done.status == JobStatus.SUCCEEDED
        assert len(sender.images) == 1
        assert sender.images[0][2] is None
        assert Path(done.output_path).read_bytes() == b"png-bytes"


async def test_delivery_failure_stage_and_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg = PluginConfig.from_mapping(
            {
                "api": {"base_url": "https://example.test/v1", "api_key": "sk-tes...9012"},
                "runtime": {"send_start_message": False},
            }
        )
        sender = FakeSender(fail_image=True)
        manager = JobManager(config=cfg, client=FakeClient(), sender=sender, data_dir=Path(td))
        await manager.start()
        try:
            job = await manager.enqueue(
                JobRequest(
                    operation="generation",
                    prompt="black cat",
                    size="1536x1024",
                    quality="medium",
                    source="test",
                ),
                JobOrigin(session="telegram:chat", sender_id="u1", platform_name="telegram"),
            )
            done = await wait_terminal(manager, job.job_id)
        finally:
            await manager.stop()
        assert done.status == JobStatus.FAILED
        assert "delivery failed" in (done.error or "")
        assert any("stage: deliver_image" in text for _, text in sender.texts)


async def test_per_user_queue_limit_and_global_clamp() -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg = PluginConfig.from_mapping(
            {
                "api": {"base_url": "https://example.test/v1", "api_key": "sk-tes...9012"},
                "runtime": {"global_max_concurrent": 99, "queue_max_size": 3, "per_user_queue_max_size": 1},
            }
        )
        assert cfg.runtime.global_max_concurrent == 1
        manager = JobManager(config=cfg, client=FakeClient(), sender=FakeSender(), data_dir=Path(td))
        await manager.start()
        try:
            await manager.enqueue(
                JobRequest(operation="generation", prompt="1", size="1536x1024", quality="medium", source="test"),
                JobOrigin(session="telegram:chat", sender_id="u1", platform_name="telegram"),
            )
            try:
                await manager.enqueue(
                    JobRequest(operation="generation", prompt="2", size="1536x1024", quality="medium", source="test"),
                    JobOrigin(session="telegram:chat", sender_id="u1", platform_name="telegram"),
                )
            except RuntimeError as exc:
                assert "当前用户图片任务过多" in str(exc)
            else:
                raise AssertionError("expected per-user queue limit")
        finally:
            await manager.stop()


def test_payload_helpers() -> None:
    kind, value = extract_image_payload(
        {"data": [{"b64_json": "data:image/png;base64," + base64.b64encode(b"abc").decode()}]}
    )
    assert kind == "b64_json"
    p = Path(tempfile.gettempdir()) / "gpt_image2_payload_test.bin"
    try:
        assert save_b64_image(value, p) == 3
        assert p.read_bytes() == b"abc"
    finally:
        p.unlink(missing_ok=True)
    text = redact_secret("https://x.test/v1?api_key=SECRET&ok=1 Authorization: Bearer abcdefghijklmnop")
    assert "SECRET" not in text and "abcdefghijklmnop" not in text


async def main() -> None:
    test_payload_helpers()
    await test_success_send_image_even_without_finish_caption()
    await test_delivery_failure_stage_and_state()
    await test_per_user_queue_limit_and_global_clamp()
    print("core smoke tests passed")


if __name__ == "__main__":
    asyncio.run(main())
