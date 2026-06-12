from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

if __package__:
    from .gpt_image2.access import AccessController, AccessDecision
    from .gpt_image2.client import GPTImage2Client
    from .gpt_image2.fallback import FallbackImageClient
    from .gpt_image2.config import PluginConfig
    from .gpt_image2.image_io import collect_image_components, materialize_images, normalize_choice, strip_command_text
    from .gpt_image2.jobs import JobManager, describe_job
    from .gpt_image2.message_io import AstrBotMessageSender
    from .gpt_image2.models import JobOrigin, JobRequest
else:  # pragma: no cover - local smoke tests may import main.py as a top-level module.
    from gpt_image2.access import AccessController, AccessDecision
    from gpt_image2.client import GPTImage2Client
    from gpt_image2.fallback import FallbackImageClient
    from gpt_image2.config import PluginConfig
    from gpt_image2.image_io import collect_image_components, materialize_images, normalize_choice, strip_command_text
    from gpt_image2.jobs import JobManager, describe_job
    from gpt_image2.message_io import AstrBotMessageSender
    from gpt_image2.models import JobOrigin, JobRequest

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except Exception:  # pragma: no cover - only used outside AstrBot runtime.
    get_astrbot_plugin_data_path = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_gpt_image2"
ALLOWED_SIZES = {"1024x1024", "1536x1024", "1024x1536"}
ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
ALLOWED_FORMATS = {"png", "jpeg", "webp"}
ALLOWED_BACKGROUNDS = {"auto", "transparent", "opaque"}


def _drop_legacy_fallback_endpoints_config(raw_config: dict) -> None:
    """Reset unreleased legacy JSON fallback config to the new template-list shape.

    AstrBot preserves existing scalar config values when a schema key changes
    type. Without this, an old string value for api.fallback_endpoints can be
    passed back into the WebUI field that now expects a list.
    """
    api_config = raw_config.get("api") if isinstance(raw_config, dict) else None
    if not isinstance(api_config, dict):
        return
    if isinstance(api_config.get("fallback_endpoints"), str):
        api_config["fallback_endpoints"] = []
        save_config = getattr(raw_config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
            except Exception as exc:  # pragma: no cover - defensive runtime migration.
                logger.warning("Failed to save migrated GPT Image 2 fallback config: %s", exc)


@register(
    PLUGIN_NAME,
    "Develata",
    "面向 gpt-image-2 的稳定生图/改图插件，支持命令与 LLM Tool，内置后台任务队列与并发控制。",
    "0.1.0",
    "https://github.com/Develata/astrbot_plugin_gpt_image2",
)
class GPTImage2Plugin(Star):
    """GPT Image 2 Stable.

    命令：
    - `/gptimg <prompt>`：提交文生图后台任务。
    - `/gptedit <prompt>`：提交改图后台任务，支持当前消息图片与引用消息图片。
    - `/gptimg_status [job_id]`：查询任务状态；不传 job_id 时列出最近任务。
    - `/gptimg_cancel <job_id>`：取消尚未开始的 queued 任务。
    - `/gptimg_help`：查看帮助。

    LLM Tool：
    - `gpt_image2_generate`：让大模型提交文生图后台任务。
    - `gpt_image2_edit`：让大模型基于上下文图片提交改图后台任务。

    注意：LLM Tool 只返回 job_id，不等待最终图片；最终图片或错误由后台 worker 主动发送到原会话。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.raw_config = config or {}
        _drop_legacy_fallback_endpoints_config(self.raw_config)
        self.config = PluginConfig.from_mapping(self.raw_config)
        self.session: aiohttp.ClientSession | None = None
        self.manager: JobManager | None = None
        self.access: AccessController | None = None

    async def initialize(self) -> None:
        errors = self.config.validate()
        if errors:
            logger.warning("GPT Image 2 plugin config has issues: " + "; ".join(errors))
        self.session = aiohttp.ClientSession()
        client = self._build_image_client(self.session)
        data_dir = self._data_dir()
        self.access = AccessController(config=self.config.access, state_path=data_dir / "access_state.json")
        self.manager = JobManager(
            config=self.config,
            client=client,
            sender=AstrBotMessageSender(self.context),
            data_dir=data_dir,
        )
        await self.manager.start()
        logger.info("GPT Image 2 plugin initialized")

    async def terminate(self) -> None:
        if self.manager:
            await self.manager.stop()
        if self.session:
            await self.session.close()
        logger.info("GPT Image 2 plugin terminated")

    @filter.command("gptimg")
    async def gptimg(self, event: AstrMessageEvent):
        """使用 gpt-image-2 提交文生图后台任务。用法：/gptimg [--size 1536x1024] [--quality medium] <prompt>"""
        if not self.manager:
            yield event.plain_result("GPT Image 2 插件尚未初始化完成。")
            return
        prompt_text = strip_command_text(event.get_message_str(), "gptimg")
        parsed = self._parse_prompt_and_options(prompt_text)
        prompt = parsed.pop("prompt")
        if not prompt:
            yield event.plain_result("请提供 prompt。用法：/gptimg [--size 1536x1024] [--quality medium] <prompt>")
            return
        origin = None
        decision = None
        try:
            origin = self._origin_from_event(event)
            decision = self._reserve_access(origin)
            if not decision.allowed:
                if decision.silent:
                    _stop_event_silently(event)
                    return
                yield event.plain_result(self._access_denied_message(decision.reason))
                return
            refs = await self._try_materialize_event_images(event)
            operation = "edit" if refs else "generation"
            job = await self.manager.enqueue(
                self._make_request(operation, prompt, parsed, reference_paths=refs, source="command"),
                origin,
            )
        except asyncio.TimeoutError:
            if origin is not None and decision is not None:
                self._release_access(origin, decision)
            yield event.plain_result("提取参考图超时，未提交任务。请稍后重试，或使用更小/更少的图片。")
            return
        except Exception as exc:
            if origin is not None and decision is not None:
                self._release_access(origin, decision)
            yield event.plain_result(f"提交 GPT Image 2 任务失败：{exc}")
            return
        message = self._submit_message(job.job_id, operation, job.queue_position)
        if message:
            yield event.plain_result(message)
        else:
            _stop_event_silently(event)

    @filter.command("gptedit")
    async def gptedit(self, event: AstrMessageEvent):
        """使用 gpt-image-2 提交改图后台任务。支持当前消息图片与引用消息图片。"""
        if not self.manager:
            yield event.plain_result("GPT Image 2 插件尚未初始化完成。")
            return
        if not self.config.edit.enabled:
            yield event.plain_result("gpt-image-2 改图功能当前未启用。")
            return
        prompt_text = strip_command_text(event.get_message_str(), "gptedit")
        parsed = self._parse_prompt_and_options(prompt_text)
        prompt = parsed.pop("prompt")
        if not prompt:
            yield event.plain_result("请提供改图 prompt，并附带或引用至少一张图片。")
            return
        origin = None
        decision = None
        try:
            origin = self._origin_from_event(event)
            decision = self._reserve_access(origin)
            if not decision.allowed:
                if decision.silent:
                    _stop_event_silently(event)
                    return
                yield event.plain_result(self._access_denied_message(decision.reason))
                return
            refs = await self._materialize_event_images(event)
            if not refs:
                self._release_access(origin, decision)
                yield event.plain_result("未检测到参考图。请在当前消息附图，或引用一条包含图片的消息后使用 /gptedit。")
                return
            job = await self.manager.enqueue(
                self._make_request("edit", prompt, parsed, reference_paths=refs, source="command"),
                origin,
            )
        except asyncio.TimeoutError:
            if origin is not None and decision is not None:
                self._release_access(origin, decision)
            yield event.plain_result(
                "提取参考图超时，未提交改图任务。请稍后重试，或使用更小/更少的图片。"
            )
            return
        except Exception as exc:
            if origin is not None and decision is not None:
                self._release_access(origin, decision)
            yield event.plain_result(f"提交 GPT Image 2 改图任务失败：{exc}")
            return
        message = self._submit_message(job.job_id, "edit", job.queue_position)
        if message:
            yield event.plain_result(message)
        else:
            _stop_event_silently(event)

    @filter.command("gptimg_status")
    async def gptimg_status(self, event: AstrMessageEvent):
        """查询 GPT Image 2 任务状态。用法：/gptimg_status [job_id]"""
        if not self.manager:
            yield event.plain_result("GPT Image 2 插件尚未初始化完成。")
            return
        arg = strip_command_text(event.get_message_str(), "gptimg_status").strip()
        if arg:
            job = self.manager.get(arg)
            yield event.plain_result(describe_job(job) if job else f"未找到任务：{arg}")
            return
        recent = self.manager.list_recent(limit=8)
        if not recent:
            yield event.plain_result("暂无 GPT Image 2 任务。")
            return
        yield event.plain_result("最近 GPT Image 2 任务：\n\n" + "\n\n".join(describe_job(job) for job in recent))

    @filter.command("gptimg_cancel")
    async def gptimg_cancel(self, event: AstrMessageEvent):
        """取消尚未开始的 GPT Image 2 queued 任务。用法：/gptimg_cancel <job_id>"""
        if not self.manager:
            yield event.plain_result("GPT Image 2 插件尚未初始化完成。")
            return
        job_id = strip_command_text(event.get_message_str(), "gptimg_cancel").strip()
        if not job_id:
            yield event.plain_result("用法：/gptimg_cancel <job_id>")
            return
        job = await self.manager.cancel(job_id)
        if not job:
            yield event.plain_result(f"未找到任务：{job_id}")
            return
        yield event.plain_result(describe_job(job))

    @filter.command("gptimg_cache")
    async def gptimg_cache(self, event: AstrMessageEvent):
        """查看 GPT Image 2 输出图片缓存。"""
        if not self.manager:
            yield event.plain_result("GPT Image 2 插件尚未初始化完成。")
            return
        stats = self.manager.cache_stats()
        yield event.plain_result(
            "GPT Image 2 图片缓存\n"
            f"files: {stats.file_count}\n"
            f"size_mb: {stats.total_mb:.2f}\n"
            f"max_cache_mb: {self.config.runtime.max_cache_mb}\n"
            f"cleanup_interval_minutes: {self.config.runtime.cleanup_interval_minutes}\n"
            f"job_ttl_hours: {self.config.runtime.job_ttl_hours}"
        )

    @filter.command("gptimg_cache_clear")
    async def gptimg_cache_clear(self, event: AstrMessageEvent):
        """手动执行 GPT Image 2 输出图片缓存清理。"""
        if not self.manager:
            yield event.plain_result("GPT Image 2 插件尚未初始化完成。")
            return
        result = self.manager.cleanup_cache()
        yield event.plain_result(
            "GPT Image 2 图片缓存清理完成\n"
            f"deleted_files: {result.deleted_count}\n"
            f"deleted_mb: {result.deleted_bytes / 1024 / 1024:.2f}\n"
            f"before_mb: {(result.before.total_mb if result.before else 0):.2f}\n"
            f"after_mb: {(result.after.total_mb if result.after else 0):.2f}"
        )

    @filter.command("gptimg_help")
    async def gptimg_help(self, event: AstrMessageEvent):
        """查看 GPT Image 2 插件帮助。"""
        yield event.plain_result(HELP_TEXT)

    @filter.llm_tool(name="gpt_image2_generate")
    async def gpt_image2_generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        size: str = "",
        quality: str = "",
    ) -> str:
        """Submit a gpt-image-2 image generation job in the background. Use this tool only when the user explicitly asks to draw, create, render, generate, or make an image. Do not call repeatedly for the same user request. The final image will be sent separately to the chat; this tool returns only a job id.

        Args:
            prompt(string): The explicit, detailed image prompt to send to gpt-image-2.
            size(string): Optional image size. One of 1024x1024, 1536x1024, 1024x1536. Leave empty to use default.
            quality(string): Optional quality. One of low, medium, high, auto. Leave empty to use default.
        """
        if not self.config.llm_tool.enabled:
            return "GPT Image 2 LLM Tool 当前未启用。"
        if not self.manager:
            return "GPT Image 2 插件尚未初始化完成。"
        opts = {
            "size": normalize_choice(size, ALLOWED_SIZES, self.config.llm_tool.default_size),
            "quality": normalize_choice(quality, ALLOWED_QUALITIES, self.config.llm_tool.default_quality),
        }
        origin = None
        decision = None
        try:
            origin = self._origin_from_event(event)
            decision = self._reserve_access(origin)
            if not decision.allowed:
                return "" if decision.silent else self._access_denied_message(decision.reason)
            job = await self.manager.enqueue(
                self._make_request("generation", prompt, opts, reference_paths=[], source="llm_tool"),
                origin,
            )
            return self._tool_submit_message(job.job_id, "generation", job.queue_position)
        except Exception as exc:
            if origin is not None and decision is not None:
                self._release_access(origin, decision)
            return f"提交 GPT Image 2 任务失败：{exc}"

    @filter.llm_tool(name="gpt_image2_edit")
    async def gpt_image2_edit(
        self,
        event: AstrMessageEvent,
        prompt: str,
        size: str = "",
        quality: str = "",
    ) -> str:
        """Submit a gpt-image-2 image edit job using images attached to or quoted by the current message. Use this tool only when the user explicitly asks to edit, transform, modify, restyle, or redraw an image. Do not call repeatedly for the same user request. The final image will be sent separately to the chat; this tool returns only a job id.

        Args:
            prompt(string): The explicit, detailed edit instruction for gpt-image-2.
            size(string): Optional output image size. One of 1024x1024, 1536x1024, 1024x1536. Leave empty to use default.
            quality(string): Optional output quality. One of low, medium, high, auto. Leave empty to use default.
        """
        if not self.config.llm_tool.enabled:
            return "GPT Image 2 LLM Tool 当前未启用。"
        if not self.config.edit.enabled:
            return "GPT Image 2 改图功能当前未启用。"
        if not self.manager:
            return "GPT Image 2 插件尚未初始化完成。"
        origin = None
        decision = None
        try:
            origin = self._origin_from_event(event)
            decision = self._reserve_access(origin)
            if not decision.allowed:
                return "" if decision.silent else self._access_denied_message(decision.reason)
            refs = await self._materialize_event_images(event)
            if not refs:
                self._release_access(origin, decision)
                return "未检测到参考图；没有提交改图任务。请让用户附图或引用包含图片的消息。"
            opts = {
                "size": normalize_choice(size, ALLOWED_SIZES, self.config.llm_tool.default_size),
                "quality": normalize_choice(quality, ALLOWED_QUALITIES, self.config.llm_tool.default_quality),
            }
            job = await self.manager.enqueue(
                self._make_request("edit", prompt, opts, reference_paths=refs, source="llm_tool"),
                origin,
            )
            return self._tool_submit_message(job.job_id, "edit", job.queue_position)
        except asyncio.TimeoutError:
            if origin is not None and decision is not None:
                self._release_access(origin, decision)
            return "提取参考图超时，未提交改图任务。请让用户稍后重试，或使用更小/更少的图片。"
        except Exception as exc:
            if origin is not None and decision is not None:
                self._release_access(origin, decision)
            return f"提交 GPT Image 2 改图任务失败：{exc}"

    async def _materialize_event_images(self, event: AstrMessageEvent) -> list[str]:
        """Resolve current/quoted image components to local paths within a short timeout.

        This runs before enqueue because AstrBot message components/events are not a
        stable serializable job payload. The timeout keeps LLM Tool calls fast and
        prevents slow platform downloads from hitting AstrBot's tool timeout.
        """
        return await asyncio.wait_for(
            materialize_images(
                event,
                max_images=self.config.edit.max_reference_images,
                max_bytes=self.config.edit.max_reference_image_mb * 1024 * 1024,
            ),
            timeout=max(1, self.config.edit.materialize_timeout_seconds),
        )

    async def _try_materialize_event_images(self, event: AstrMessageEvent) -> list[str]:
        if not self.config.edit.enabled:
            return []
        components = collect_image_components(
            event,
            max_images=self.config.edit.max_reference_images,
            max_bytes=self.config.edit.max_reference_image_mb * 1024 * 1024,
        )
        if not components:
            return []
        refs = await self._materialize_event_images(event)
        if not refs:
            raise ValueError("检测到图片消息，但未能解析为本地参考图；为避免误走文生图，已取消提交。")
        return refs

    def _reserve_access(self, origin: JobOrigin) -> AccessDecision:
        if not self.access:
            return AccessDecision(True)
        return self.access.check_and_reserve(origin)

    def _release_access(self, origin: JobOrigin, decision: AccessDecision) -> None:
        if self.access:
            self.access.release_reservation(origin, decision)

    def _access_denied_message(self, reason: str) -> str:
        if reason.startswith("daily_limit_exceeded"):
            return "今日非白名单生图额度已用完。"
        if reason == "user_not_whitelisted":
            return "你不在 GPT Image 2 用户白名单中，当前配置不允许非白名单用户生成图片。"
        return "当前会话无权使用 GPT Image 2 生图功能。"

    def _data_dir(self) -> Path:
        if get_astrbot_plugin_data_path:
            return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        return Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME

    def _build_image_client(self, session: aiohttp.ClientSession):
        clients = [
            (
                "primary",
                GPTImage2Client(
                    session,
                    base_url=self.config.api.base_url,
                    api_key=self.config.api.api_key,
                    model=self.config.api.model,
                    timeout_seconds=self.config.api.timeout_seconds,
                    user_agent=self.config.api.user_agent,
                ),
            )
        ]
        if self.config.api.fallback_enabled:
            for idx, endpoint in enumerate(self.config.api.fallback_endpoints, start=1):
                clients.append(
                    (
                        f"fallback_{idx}",
                        GPTImage2Client(
                            session,
                            base_url=endpoint.base_url,
                            api_key=endpoint.api_key,
                            model=endpoint.model or "gpt-image-2",
                            timeout_seconds=self.config.api.timeout_seconds,
                            user_agent=self.config.api.user_agent,
                        ),
                    )
                )
        return FallbackImageClient(clients) if len(clients) > 1 else clients[0][1]

    def _origin_from_event(self, event: AstrMessageEvent) -> JobOrigin:
        raw_group_id = event.get_group_id()
        group_id = _safe_id(raw_group_id)
        return JobOrigin(
            session=event.unified_msg_origin,
            platform_name=str(event.get_platform_name()),
            sender_id=_safe_id(event.get_sender_id()),
            sender_name=str(event.get_sender_name()),
            group_id=group_id,
            is_group_chat=_is_group_event(event, group_id),
        )

    def _make_request(
        self,
        operation: str,
        prompt: str,
        opts: dict[str, Any],
        reference_paths: list[str],
        source: str,
    ) -> JobRequest:
        return JobRequest(
            operation="edit" if operation == "edit" else "generation",
            prompt=self._apply_prompt_prefix(prompt.strip()),
            size=normalize_choice(str(opts.get("size", "")), ALLOWED_SIZES, self.config.defaults.size),
            quality=normalize_choice(str(opts.get("quality", "")), ALLOWED_QUALITIES, self.config.defaults.quality),
            output_format=normalize_choice(str(opts.get("output_format", "")), ALLOWED_FORMATS, self.config.defaults.output_format),
            background=normalize_choice(str(opts.get("background", "")), ALLOWED_BACKGROUNDS, self.config.defaults.background),
            reference_paths=reference_paths,
            source=source,
        )

    def _apply_prompt_prefix(self, prompt: str) -> str:
        prefix = self.config.prompt.prefix.strip()
        if not prefix:
            return prompt
        if not prompt:
            return prefix
        return f"{prefix}\n{prompt}"

    def _parse_prompt_and_options(self, text: str) -> dict[str, str]:
        opts: dict[str, str] = {}
        prompt_parts: list[str] = []
        for is_option, chunk in _split_prompt_option_chunks(text):
            if not is_option:
                if chunk.strip():
                    prompt_parts.append(chunk.strip())
                continue
            key, value, remainder = _split_option_chunk(chunk)
            if key in {"size", "s"}:
                opts["size"] = value
            elif key in {"quality", "q"}:
                opts["quality"] = value
            elif key in {"format", "output_format"}:
                opts["output_format"] = value
            elif key == "background":
                opts["background"] = value
            else:
                # Unknown options are kept in the prompt instead of being silently
                # discarded. This mirrors OmniDraw's parser idea: only whitespace
                # followed by --key starts an option, so ordinary hyphenated text
                # such as "mid-journey" remains intact.
                prompt_parts.append(chunk.strip())
            if remainder:
                prompt_parts.append(remainder)
        opts["prompt"] = " ".join(part for part in prompt_parts if part).strip()
        return opts

    def _submit_message(self, job_id: str, kind: str, queue_position: int) -> str:
        if self.config.runtime.quiet_mode:
            return ""
        return (
            "✅ 已提交 GPT Image 2 后台任务\n"
            f"job_id: {job_id}\n"
            f"type: {kind}\n"
            f"status: queued\n"
            f"queue_position: {queue_position}\n"
            "完成后会直接把图片发送到当前会话。"
        )

    def _tool_submit_message(self, job_id: str, kind: str, queue_position: int) -> str:
        if self.config.runtime.quiet_mode:
            return f"queued:{job_id}. Final image will be sent directly by the plugin. Do not call this tool again for the same request."
        return (
            "已提交 GPT Image 2 后台图片任务。\n"
            f"job_id: {job_id}\n"
            f"type: {kind}\n"
            f"status: queued\n"
            f"queue_position: {queue_position}\n"
            "最终图片会由插件后台直接发送到当前会话。不要为了同一用户请求重复调用本工具。"
        )


def _split_prompt_option_chunks(text: str) -> list[tuple[bool, str]]:
    """Split prompt text at whitespace-prefixed --key boundaries.

    Borrowed in spirit from OmniDraw's CommandParser: normal hyphenated words
    inside the prompt are not option separators, while suffix options such as
    `--size 1024x1024` can appear after free-form prompt text.
    """
    parts = re.split(r"(?=\s--[A-Za-z0-9_-]+)", " " + (text or ""))
    chunks: list[tuple[bool, str]] = []
    for index, part in enumerate(parts):
        stripped = part.strip()
        if not stripped:
            continue
        chunks.append((index > 0 and stripped.startswith("--"), stripped))
    return chunks


def _split_option_chunk(chunk: str) -> tuple[str, str, str]:
    raw = chunk[2:].strip() if chunk.startswith("--") else chunk.strip()
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    if not tokens:
        return "", "", ""
    key = tokens[0].strip().lower().replace("-", "_")
    value = str(tokens[1]).strip() if len(tokens) > 1 else "true"
    remainder = " ".join(str(token) for token in tokens[2:]).strip() if len(tokens) > 2 else ""
    return key, value, remainder


def _stop_event_silently(event: Any) -> None:
    for name in ("stop_event", "stop_propagation", "prevent_default"):
        marker = getattr(event, name, None)
        if callable(marker):
            try:
                marker()
            except Exception:
                pass
            return


def _is_group_event(event: Any, group_id: str) -> bool:
    if group_id:
        return True
    for attr in ("is_group", "is_group_chat"):
        marker = getattr(event, attr, None)
        try:
            value = marker() if callable(marker) else marker
        except Exception:
            continue
        if value is not None:
            return bool(value)
    for attr in ("message_type", "type", "conversation_type"):
        value = getattr(event, attr, None)
        if value is not None and any(token in str(value).lower() for token in ("group", "guild", "channel")):
            return True
    return any(token in str(getattr(event, "unified_msg_origin", "")).lower() for token in ("group", "guild", "channel", "room"))


def _safe_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "none" else text


HELP_TEXT = """GPT Image 2 Stable 用法

命令：
/gptimg <prompt>       # 无图=文生图；当前/引用消息有图=改图
/gptimg --size 1536x1024 --quality medium <prompt>
/gptedit <prompt>      # 兼容保留：强制改图，当前消息附图，或引用包含图片的消息
/gptimg_status [job_id]
/gptimg_cancel <job_id>
/gptimg_cache
/gptimg_cache_clear
/gptimg_help

支持参数：
size: 1024x1024 / 1536x1024 / 1024x1536
quality: low / medium / high / auto
format: png / jpeg / webp
background: auto / transparent / opaque

设计原则：
- 所有生图/改图都进入后台队列。
- LLM Tool 只提交任务，不等待最终图片。
- 完成后插件主动发送图片。
- 失败时直接发送脱敏截断后的原始错误摘要，不再调用 LLM 润色。
"""
