from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class APIConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-image-2"
    timeout_seconds: int = 900
    user_agent: str = "AstrBot-GPT-Image2/0.1.0"


@dataclass(slots=True)
class DefaultConfig:
    size: str = "1536x1024"
    quality: str = "medium"
    output_format: str = "png"
    background: str = "auto"


@dataclass(slots=True)
class RuntimeConfig:
    global_max_concurrent: int = 1
    per_user_max_concurrent: int = 1
    queue_max_size: int = 5
    job_ttl_hours: int = 24
    send_start_message: bool = True
    send_finish_message: bool = True
    state_file: str = "jobs.json"
    output_dir: str = "outputs"


@dataclass(slots=True)
class LLMToolConfig:
    enabled: bool = True
    default_size: str = "1536x1024"
    default_quality: str = "medium"
    require_explicit_image_intent: bool = True


@dataclass(slots=True)
class EditConfig:
    enabled: bool = True
    max_reference_images: int = 4
    max_reference_image_mb: int = 20


@dataclass(slots=True)
class PluginConfig:
    api: APIConfig = field(default_factory=APIConfig)
    defaults: DefaultConfig = field(default_factory=DefaultConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    llm_tool: LLMToolConfig = field(default_factory=LLMToolConfig)
    edit: EditConfig = field(default_factory=EditConfig)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "PluginConfig":
        raw = _unwrap_config_values(raw or {})
        return cls(
            api=_merge_dataclass(APIConfig, raw.get("api")),
            defaults=_merge_dataclass(DefaultConfig, raw.get("defaults")),
            runtime=_merge_dataclass(RuntimeConfig, raw.get("runtime")),
            llm_tool=_merge_dataclass(LLMToolConfig, raw.get("llm_tool")),
            edit=_merge_dataclass(EditConfig, raw.get("edit")),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.api.base_url:
            errors.append("api.base_url 不能为空")
        if not self.api.api_key:
            errors.append("api.api_key 不能为空")
        if self.api.model != "gpt-image-2":
            errors.append("api.model 首版只支持 gpt-image-2")
        if self.api.timeout_seconds < 60:
            errors.append("api.timeout_seconds 建议至少 60 秒")
        if self.runtime.global_max_concurrent < 1:
            errors.append("runtime.global_max_concurrent 必须 >= 1")
        if self.runtime.per_user_max_concurrent < 1:
            errors.append("runtime.per_user_max_concurrent 必须 >= 1")
        if self.runtime.queue_max_size < 1:
            errors.append("runtime.queue_max_size 必须 >= 1")
        if self.edit.max_reference_images < 1:
            errors.append("edit.max_reference_images 必须 >= 1")
        return errors


def _unwrap_config_values(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept both AstrBot parsed config and raw data/config entity shapes.

    AstrBot usually passes parsed values to plugin __init__, but this helper also
    handles `{key: {"value": ...}}` entities for manual tests or older loaders.
    """
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and set(value.keys()) >= {"value"} and "config_type" in value:
            out[key] = value.get("value")
        elif isinstance(value, dict):
            out[key] = _unwrap_config_values(value)
        else:
            out[key] = value
    return out


def _merge_dataclass(cls: type, value: Any):
    defaults = cls()
    if not isinstance(value, dict):
        return defaults
    allowed = set(defaults.__dataclass_fields__.keys())  # type: ignore[attr-defined]
    data = {k: v for k, v in value.items() if k in allowed}
    return cls(**{**{k: getattr(defaults, k) for k in allowed}, **data})
