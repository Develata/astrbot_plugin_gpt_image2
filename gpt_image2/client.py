from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from aiohttp import ClientTimeout, FormData

from .errors import APIStatusError, compact_error_body


class GPTImage2Client:
    def __init__(self, session: Any, *, base_url: str, api_key: str, model: str, timeout_seconds: int, user_agent: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = ClientTimeout(total=timeout_seconds)
        self.user_agent = user_agent

    async def generate(
        self,
        *,
        prompt: str,
        size: str,
        quality: str,
        output_format: str,
        background: str,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/images/generations"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "background": background,
        }
        headers = self._json_headers()
        async with self.session.post(url, json=payload, headers=headers, timeout=self.timeout) as resp:
            return await self._parse_response(resp)

    async def edit(
        self,
        *,
        prompt: str,
        image_paths: list[str],
        size: str,
        quality: str,
        output_format: str,
        background: str,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/images/edits"
        form = FormData()
        form.add_field("model", self.model)
        form.add_field("prompt", prompt)
        form.add_field("size", size)
        form.add_field("quality", quality)
        form.add_field("output_format", output_format)
        form.add_field("background", background)
        opened = []
        try:
            for index, path in enumerate(image_paths):
                p = Path(path)
                content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                fh = p.open("rb")
                opened.append(fh)
                # OpenAI-compatible image edit APIs commonly accept repeated image fields.
                form.add_field("image", fh, filename=p.name or f"reference_{index}.png", content_type=content_type)
            async with self.session.post(url, data=form, headers=self._auth_headers(), timeout=self.timeout) as resp:
                return await self._parse_response(resp)
        finally:
            for fh in opened:
                try:
                    fh.close()
                except Exception:
                    pass

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

    def _json_headers(self) -> dict[str, str]:
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        return headers

    async def _parse_response(self, resp: Any) -> dict[str, Any]:
        body = await resp.read()
        content_type = resp.headers.get("Content-Type", "")
        if resp.status < 200 or resp.status >= 300:
            raise APIStatusError(resp.status, compact_error_body(body), content_type)
        try:
            return await resp.json(content_type=None)
        except Exception as exc:
            raise APIStatusError(resp.status, f"JSON parse error: {exc}; body={compact_error_body(body)}", content_type) from exc


def extract_image_payload(response: dict[str, Any]) -> tuple[str, str]:
    data = response.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            if isinstance(first.get("b64_json"), str) and first["b64_json"]:
                return "b64_json", first["b64_json"]
            if isinstance(first.get("url"), str) and first["url"]:
                return "url", first["url"]
    found = _walk_for_image(response)
    if found:
        return found
    raise ValueError("API response did not contain data[0].b64_json or data[0].url")


def save_b64_image(value: str, output_path: Path) -> int:
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    raw = base64.b64decode(value)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(raw)
    return len(raw)


def _walk_for_image(obj: Any) -> tuple[str, str] | None:
    if isinstance(obj, dict):
        for key in ("b64_json", "image_url", "url"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return ("b64_json" if key == "b64_json" else "url", value)
        for value in obj.values():
            got = _walk_for_image(value)
            if got:
                return got
    elif isinstance(obj, list):
        for value in obj:
            got = _walk_for_image(value)
            if got:
                return got
    return None
