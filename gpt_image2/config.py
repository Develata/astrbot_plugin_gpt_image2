from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class APIEndpointConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-image-2"


@dataclass(slots=True)
class APIConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-image-2"
    timeout_seconds: int = 900
    user_agent: str = "AstrBot-GPT-Image2/0.2.2"
    fallback_enabled: bool = False
    fallback_endpoints: list[APIEndpointConfig] = field(default_factory=list)


@dataclass(slots=True)
class DefaultConfig:
    size: str = "1536x1024"
    quality: str = "medium"
    output_format: str = "png"
    background: str = "auto"


@dataclass(slots=True)
class RuntimeConfig:
    global_max_concurrent: int = 1
    per_user_queue_max_size: int = 5
    queue_max_size: int = 5
    job_ttl_hours: int = 24
    cleanup_interval_minutes: int = 360
    max_cache_mb: int = 1024
    quiet_mode: bool = False
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
    materialize_timeout_seconds: int = 10


@dataclass(slots=True)
class PromptConfig:
    prefix: str = ""


@dataclass(slots=True)
class AccessConfig:
    enabled: bool = False
    user_whitelist: str = ""
    group_whitelist: str = ""
    non_whitelist_daily_limit: int = 0


@dataclass(slots=True)
class PluginConfig:
    api: APIConfig = field(default_factory=APIConfig)
    defaults: DefaultConfig = field(default_factory=DefaultConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    llm_tool: LLMToolConfig = field(default_factory=LLMToolConfig)
    edit: EditConfig = field(default_factory=EditConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    access: AccessConfig = field(default_factory=AccessConfig)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "PluginConfig":
        raw = _unwrap_config_values(raw or {})
        cfg = cls(
            api=_merge_api_config(raw.get("api")),
            defaults=_merge_dataclass(DefaultConfig, raw.get("defaults")),
            runtime=_merge_dataclass(RuntimeConfig, raw.get("runtime")),
            llm_tool=_merge_dataclass(LLMToolConfig, raw.get("llm_tool")),
            edit=_merge_dataclass(EditConfig, raw.get("edit")),
            prompt=_merge_dataclass(PromptConfig, raw.get("prompt")),
            access=_merge_dataclass(AccessConfig, raw.get("access")),
        )
        # v0.1 deliberately runs a single worker request at a time. This is a
        # product invariant for gpt-image-2 account-concurrency safety, not a
        # performance tuning knob.
        cfg.runtime.global_max_concurrent = 1
        return cfg

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
        if self.runtime.per_user_queue_max_size < 1:
            errors.append("runtime.per_user_queue_max_size 必须 >= 1")
        if self.runtime.queue_max_size < 1:
            errors.append("runtime.queue_max_size 必须 >= 1")
        if self.runtime.cleanup_interval_minutes < 0 or self.runtime.cleanup_interval_minutes > 10080:
            errors.append("runtime.cleanup_interval_minutes 必须在 0..10080 之间")
        if self.runtime.max_cache_mb < 0 or self.runtime.max_cache_mb > 1048576:
            errors.append("runtime.max_cache_mb 必须在 0..1048576 之间")
        if self.access.non_whitelist_daily_limit < 0 or self.access.non_whitelist_daily_limit > 10000:
            errors.append("access.non_whitelist_daily_limit 必须在 0..10000 之间")
        if self.edit.max_reference_images < 1:
            errors.append("edit.max_reference_images 必须 >= 1")
        if self.edit.materialize_timeout_seconds < 1 or self.edit.materialize_timeout_seconds > 60:
            errors.append("edit.materialize_timeout_seconds 必须在 1..60 之间")
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


def _merge_api_config(value: Any) -> APIConfig:
    cfg = _merge_dataclass(APIConfig, value)
    endpoints: list[APIEndpointConfig] = []
    if isinstance(value, dict):
        raw_endpoints = value.get("fallback_endpoints") or []
        if isinstance(raw_endpoints, list):
            for item in raw_endpoints:
                if not isinstance(item, dict):
                    continue
                data = dict(item)
                data.pop("__template_key", None)
                endpoint = _merge_dataclass(APIEndpointConfig, data)
                if endpoint.base_url and endpoint.api_key:
                    endpoints.append(endpoint)
    cfg.fallback_endpoints = endpoints
    return cfg


def _merge_dataclass(cls: type, value: Any):
    defaults = cls()
    if not isinstance(value, dict):
        return defaults
    allowed = set(defaults.__dataclass_fields__.keys())  # type: ignore[attr-defined]
    data = {k: v for k, v in value.items() if k in allowed}
    return cls(**{**{k: getattr(defaults, k) for k in allowed}, **data})
