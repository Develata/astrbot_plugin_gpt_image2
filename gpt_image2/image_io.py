from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any


def collect_image_components(event: Any, *, max_images: int, max_bytes: int) -> list[Any]:
    images: list[Any] = []
    for component in getattr(getattr(event, "message_obj", None), "message", []) or []:
        _collect_from_component(component, images, max_images)
        if len(images) >= max_images:
            break
    return images[:max_images]


async def materialize_images(event: Any, *, max_images: int, max_bytes: int) -> list[str]:
    paths: list[str] = []
    for component in collect_image_components(event, max_images=max_images, max_bytes=max_bytes):
        converter = getattr(component, "convert_to_file_path", None)
        if not callable(converter):
            continue
        maybe_path = converter()
        path = await maybe_path if inspect.isawaitable(maybe_path) else maybe_path
        p = Path(str(path))
        if not p.exists():
            continue
        size = p.stat().st_size
        if size > max_bytes:
            raise ValueError(f"参考图过大: {p.name} = {size} bytes, limit={max_bytes}")
        paths.append(str(p))
    return paths


def _collect_from_component(component: Any, images: list[Any], max_images: int) -> None:
    if len(images) >= max_images:
        return
    type_name = str(getattr(component, "type", "")).lower()
    cls_name = component.__class__.__name__.lower()
    if "image" in type_name or cls_name == "image":
        images.append(component)
        return
    # Reply components may carry the quoted message chain in `chain`.
    chain = getattr(component, "chain", None)
    if chain:
        for child in chain:
            _collect_from_component(child, images, max_images)
            if len(images) >= max_images:
                break


def strip_command_text(message_str: str, command: str) -> str:
    text = (message_str or "").strip()
    prefixes = [f"/{command}", command]
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def normalize_choice(value: str | None, allowed: set[str], default: str) -> str:
    value = (value or "").strip().lower()
    return value if value in allowed else default
