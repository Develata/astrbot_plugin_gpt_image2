from __future__ import annotations

from typing import Any, Protocol

from .errors import compact_error_body


class ImageEndpointClient(Protocol):
    async def generate(self, **kwargs: Any) -> dict[str, Any]: ...
    async def edit(self, **kwargs: Any) -> dict[str, Any]: ...


class FallbackImageClient:
    """Try primary then fallback clients for compatible gpt-image-2 endpoints.

    It does not retry the same endpoint. Fallback is only attempted after a
    concrete exception from the current endpoint and is reported as one combined
    error if all endpoints fail.
    """

    def __init__(self, clients: list[tuple[str, ImageEndpointClient]]) -> None:
        if not clients:
            raise ValueError("at least one image client is required")
        self.clients = clients

    async def generate(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call("generate", kwargs)

    async def edit(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call("edit", kwargs)

    async def _call(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        for index, (name, client) in enumerate(self.clients, start=1):
            try:
                response = await getattr(client, method)(**kwargs)
                response["__gpt_image2_endpoint"] = name
                response["__gpt_image2_endpoint_index"] = index
                model = getattr(client, "model", "")
                if model:
                    response["__gpt_image2_model"] = str(model)
                return response
            except Exception as exc:
                errors.append(
                    f"endpoint#{index}({name}): {compact_error_body(str(exc), limit=800)}"
                )
        raise RuntimeError("所有 gpt-image-2 endpoint 均失败：\n" + "\n".join(errors))
