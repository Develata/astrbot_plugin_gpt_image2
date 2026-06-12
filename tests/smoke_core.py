from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from gpt_image2.access import AccessController
from gpt_image2.cache import OutputCache
from gpt_image2.client import extract_image_payload, save_b64_image
from gpt_image2.config import PluginConfig
from gpt_image2.errors import APIStatusError, redact_secret
from gpt_image2.fallback import FallbackImageClient
from gpt_image2.jobs import JobManager
from gpt_image2.models import JobOrigin, JobRequest, JobStatus


class FakeClient:
    async def generate(self, **kwargs):
        return {"data": [{"b64_json": base64.b64encode(b"png-bytes").decode()}]}

    async def edit(self, **kwargs):
        return {"data": [{"url": "https://example.test/out.png"}]}


class FailingClient:
    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        raise self.exc

    async def edit(self, **kwargs):
        self.calls += 1
        raise self.exc


class RecordingClient(FakeClient):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return await super().generate(**kwargs)


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


async def test_cache_cleanup_and_access_and_fallback() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out = root / "outputs"
        out.mkdir()
        old = out / "old.png"
        new = out / "new.png"
        old.write_bytes(b"old")
        new.write_bytes(b"newer")
        old_time = time.time() - 3 * 3600
        old.touch()
        new.touch()
        import os

        os.utime(old, (old_time, old_time))
        cache = OutputCache(out)
        res = cache.cleanup(ttl_hours=1, max_cache_mb=0)
        assert res.deleted_count == 1
        assert not old.exists() and new.exists()

        access_cfg = PluginConfig.from_mapping(
            {
                "access": {
                    "enabled": True,
                    "user_whitelist": "u-admin",
                    "group_whitelist": "g1",
                    "non_whitelist_daily_limit": 1,
                }
            }
        ).access
        ac = AccessController(config=access_cfg, state_path=root / "access.json")
        assert ac.check(JobOrigin(session="s", sender_id="u-admin", group_id="g1", is_group_chat=True)).allowed
        origin = JobOrigin(session="s", sender_id="u2", group_id="g1", is_group_chat=True)
        decision = ac.check_and_reserve(origin)
        assert decision.allowed and decision.reserved
        assert not ac.check(origin).allowed
        ac.release_reservation(origin, decision)
        assert ac.check(origin).allowed
        assert ac.check_and_reserve(origin).allowed
        assert not ac.check(origin).allowed
        assert ac.check(JobOrigin(session="s", sender_id="u2", group_id="g2", is_group_chat=True)).silent
        assert ac.check(JobOrigin(session="private", sender_id="u3", group_id="", is_group_chat=False)).allowed

        secondary = RecordingClient()
        client = FallbackImageClient(
            [
                ("bad", FailingClient(APIStatusError(429, "busy"))),
                ("good", secondary),
            ]
        )
        await client.generate(prompt="x", size="1024x1024", quality="low", output_format="png", background="auto")
        assert secondary.calls == 1
        timeout_secondary = RecordingClient()
        client = FallbackImageClient(
            [
                ("timeout", FailingClient(TimeoutError("maybe still running"))),
                ("should_not_run", timeout_secondary),
            ]
        )
        try:
            await client.generate(prompt="x", size="1024x1024", quality="low", output_format="png", background="auto")
        except RuntimeError as exc:
            assert "maybe still running" in str(exc)
        else:
            raise AssertionError("expected conservative no-fallback failure")
        assert timeout_secondary.calls == 0

        cfg = PluginConfig.from_mapping(
            {
                "api": {
                    "base_url": "https://primary.example/v1",
                    "api_key": "sk-primary",
                    "model": "gpt-image-2",
                    "fallback_endpoints": [
                        {
                            "__template_key": "fallback_endpoint",
                            "base_url": "https://backup1.example/v1",
                            "api_key": "sk-backup1",
                            "model": "backup-image-model",
                        },
                        {"base_url": "", "api_key": "sk-empty"},
                    ],
                }
            }
        )
        assert len(cfg.api.fallback_endpoints) == 1
        assert cfg.api.fallback_endpoints[0].base_url == "https://backup1.example/v1"
        assert cfg.api.fallback_endpoints[0].api_key == "sk-backup1"
        assert cfg.api.fallback_endpoints[0].model == "backup-image-model"

        per_endpoint_model_cfg = PluginConfig.from_mapping(
            {
                "api": {
                    "model": "gpt-image-2",
                    "fallback_endpoints": [
                        {
                            "base_url": "https://backup2.example/v1",
                            "api_key": "sk-backup2",
                            "model": "other-image-model",
                        }
                    ],
                }
            }
        )
        assert per_endpoint_model_cfg.api.fallback_endpoints[0].model == "other-image-model"

        legacy_string_cfg = PluginConfig.from_mapping(
            {
                "api": {
                    "fallback_endpoints": '[{"base_url":"https://legacy.example/v1","api_key":"sk-legacy"}]'
                }
            }
        )
        assert legacy_string_cfg.api.fallback_endpoints == []


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
    await test_cache_cleanup_and_access_and_fallback()
    print("core smoke tests passed")


if __name__ == "__main__":
    asyncio.run(main())
