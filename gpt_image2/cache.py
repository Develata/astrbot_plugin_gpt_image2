from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(slots=True)
class CacheStats:
    file_count: int
    total_bytes: int
    oldest_mtime: float | None = None
    newest_mtime: float | None = None

    @property
    def total_mb(self) -> float:
        return self.total_bytes / 1024 / 1024


@dataclass(slots=True)
class CacheCleanupResult:
    deleted_count: int = 0
    deleted_bytes: int = 0
    before: CacheStats | None = None
    after: CacheStats | None = None


class OutputCache:
    """Manage plugin-owned output image files only.

    This deliberately never traverses outside the configured output directory and
    never touches AstrBot adapter temp files used for input/reference images.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def stats(self) -> CacheStats:
        files = list(self._image_files())
        total = 0
        mtimes: list[float] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            total += stat.st_size
            mtimes.append(stat.st_mtime)
        return CacheStats(
            file_count=len(mtimes),
            total_bytes=total,
            oldest_mtime=min(mtimes) if mtimes else None,
            newest_mtime=max(mtimes) if mtimes else None,
        )

    def cleanup(
        self,
        *,
        ttl_hours: int,
        max_cache_mb: int,
        exclude_paths: set[str] | None = None,
    ) -> CacheCleanupResult:
        exclude = {str(Path(path).resolve()) for path in (exclude_paths or set())}
        result = CacheCleanupResult(before=self.stats())
        cutoff = time.time() - max(ttl_hours, 0) * 3600

        # 1. TTL cleanup first, if enabled by ttl_hours > 0.
        if ttl_hours > 0:
            for path in list(self._image_files()):
                if str(path.resolve()) in exclude:
                    continue
                try:
                    stat = path.stat()
                    if stat.st_mtime < cutoff:
                        self._delete(path, stat.st_size, result)
                except OSError:
                    continue

        # 2. Size-cap cleanup: oldest files first until under cap.
        if max_cache_mb > 0:
            max_bytes = max_cache_mb * 1024 * 1024
            files_with_stat: list[tuple[float, int, Path]] = []
            total = 0
            for path in self._image_files():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                total += stat.st_size
                if str(path.resolve()) not in exclude:
                    files_with_stat.append((stat.st_mtime, stat.st_size, path))
            if total > max_bytes:
                for _, size, path in sorted(files_with_stat, key=lambda item: item[0]):
                    if total <= max_bytes:
                        break
                    try:
                        self._delete(path, size, result)
                        total -= size
                    except OSError:
                        continue
        result.after = self.stats()
        return result

    def _image_files(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for path in self.output_dir.iterdir():
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                yield path

    @staticmethod
    def _delete(path: Path, size: int, result: CacheCleanupResult) -> None:
        path.unlink(missing_ok=True)
        result.deleted_count += 1
        result.deleted_bytes += size
