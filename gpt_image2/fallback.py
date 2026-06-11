from __future__ import annotations

from typing import Any, Protocol

from .errors import APIStatusError, compact_error_body


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
                return await getattr(client, method)(**kwargs)
            except Exception as exc:
                errors.append(f"endpoint#{index}({name}): {compact_error_body(str(exc), limit=800)}")
                if index >= len(self.clients) or not _fallback_is_safe(exc):
                    break
        raise RuntimeError("所有 gpt-image-2 endpoint 均失败：\n" + "\n".join(errors))


def _fallback_is_safe(exc: BaseException) -> bool:
    # Do not fallback after timeout/504/5xx: the upstream may still finish the
    # non-idempotent image POST. Fallback is reserved for failures that indicate
    # the current endpoint did not accept the request or cannot serve it.
    return isinstance(exc, APIStatusError) and exc.status in {401, 403, 404, 429}
