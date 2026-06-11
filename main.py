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

from gpt_image2.client import GPTImage2Client
from gpt_image2.config import PluginConfig
from gpt_image2.image_io import materialize_images, normalize_choice, strip_command_text
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
        self.config = PluginConfig.from_mapping(self.raw_config)
        self.session: aiohttp.ClientSession | None = None
        self.manager: JobManager | None = None

    async def initialize(self) -> None:
        errors = self.config.validate()
        if errors:
            logger.warning("GPT Image 2 plugin config has issues: " + "; ".join(errors))
        self.session = aiohttp.ClientSession()
        client = GPTImage2Client(
            self.session,
            base_url=self.config.api.base_url,
            api_key=self.config.api.api_key,
            model=self.config.api.model,
            timeout_seconds=self.config.api.timeout_seconds,
            user_agent=self.config.api.user_agent,
        )
        self.manager = JobManager(
            config=self.config,
            client=client,
            sender=AstrBotMessageSender(self.context),
            data_dir=self._data_dir(),
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
        try:
            job = await self.manager.enqueue(
                self._make_request("generation", prompt, parsed, reference_paths=[], source="command"),
                self._origin_from_event(event),
            )
        except Exception as exc:
            yield event.plain_result(f"提交 GPT Image 2 任务失败：{exc}")
            return
        yield event.plain_result(self._submit_message(job.job_id, "generation", job.queue_position))

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
        try:
            refs = await self._materialize_event_images(event)
            if not refs:
                yield event.plain_result("未检测到参考图。请在当前消息附图，或引用一条包含图片的消息后使用 /gptedit。")
                return
            job = await self.manager.enqueue(
                self._make_request("edit", prompt, parsed, reference_paths=refs, source="command"),
                self._origin_from_event(event),
            )
        except asyncio.TimeoutError:
            yield event.plain_result(
                "提取参考图超时，未提交改图任务。请稍后重试，或使用更小/更少的图片。"
            )
            return
        except Exception as exc:
            yield event.plain_result(f"提交 GPT Image 2 改图任务失败：{exc}")
            return
        yield event.plain_result(self._submit_message(job.job_id, "edit", job.queue_position))

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
        try:
            job = await self.manager.enqueue(
                self._make_request("generation", prompt, opts, reference_paths=[], source="llm_tool"),
                self._origin_from_event(event),
            )
            return self._tool_submit_message(job.job_id, "generation", job.queue_position)
        except Exception as exc:
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
        try:
            refs = await self._materialize_event_images(event)
            if not refs:
                return "未检测到参考图；没有提交改图任务。请让用户附图或引用包含图片的消息。"
            opts = {
                "size": normalize_choice(size, ALLOWED_SIZES, self.config.llm_tool.default_size),
                "quality": normalize_choice(quality, ALLOWED_QUALITIES, self.config.llm_tool.default_quality),
            }
            job = await self.manager.enqueue(
                self._make_request("edit", prompt, opts, reference_paths=refs, source="llm_tool"),
                self._origin_from_event(event),
            )
            return self._tool_submit_message(job.job_id, "edit", job.queue_position)
        except asyncio.TimeoutError:
            return "提取参考图超时，未提交改图任务。请让用户稍后重试，或使用更小/更少的图片。"
        except Exception as exc:
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

    def _data_dir(self) -> Path:
        if get_astrbot_plugin_data_path:
            return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        return Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME

    def _origin_from_event(self, event: AstrMessageEvent) -> JobOrigin:
        return JobOrigin(
            session=event.unified_msg_origin,
            platform_name=str(event.get_platform_name()),
            sender_id=str(event.get_sender_id()),
            sender_name=str(event.get_sender_name()),
            group_id=str(event.get_group_id()),
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
            prompt=prompt.strip(),
            size=normalize_choice(str(opts.get("size", "")), ALLOWED_SIZES, self.config.defaults.size),
            quality=normalize_choice(str(opts.get("quality", "")), ALLOWED_QUALITIES, self.config.defaults.quality),
            output_format=normalize_choice(str(opts.get("output_format", "")), ALLOWED_FORMATS, self.config.defaults.output_format),
            background=normalize_choice(str(opts.get("background", "")), ALLOWED_BACKGROUNDS, self.config.defaults.background),
            reference_paths=reference_paths,
            source=source,
        )

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
        return (
            "✅ 已提交 GPT Image 2 后台任务\n"
            f"job_id: {job_id}\n"
            f"type: {kind}\n"
            f"status: queued\n"
            f"queue_position: {queue_position}\n"
            "完成后会直接把图片发送到当前会话。"
        )

    def _tool_submit_message(self, job_id: str, kind: str, queue_position: int) -> str:
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


HELP_TEXT = """GPT Image 2 Stable 用法

命令：
/gptimg <prompt>
/gptimg --size 1536x1024 --quality medium <prompt>
/gptedit <prompt>    # 当前消息附图，或引用包含图片的消息
/gptimg_status [job_id]
/gptimg_cancel <job_id>
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
