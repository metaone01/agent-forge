"""Validated service settings; credentials never belong to the read API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .metrics import WindowPolicy
from .observations import STAR_POLICY
from .download_projection import DOWNLOAD_POLICY


@dataclass(frozen=True)
class Settings:
    catalog_root: Path
    runtime_root: Path
    publication_delay_seconds: int = 600
    query_timeout_seconds: float = 2.0
    max_page_size: int = 100
    max_query_length: int = 200
    star_window: WindowPolicy = STAR_POLICY
    download_window: WindowPolicy = DOWNLOAD_POLICY
    snapshot_cursor_ttl_seconds: int = 86400

    def __post_init__(self):
        if type(self.snapshot_cursor_ttl_seconds) is not int or not 60 <= self.snapshot_cursor_ttl_seconds <= 7 * 86400:
            raise ValueError("snapshot_cursor_ttl_seconds must be in [60,604800]")
        if not isinstance(self.download_window, WindowPolicy):
            raise ValueError("download_window must be an explicit immutable WindowPolicy")
        if not isinstance(self.star_window, WindowPolicy):
            raise ValueError("star_window must be an explicit immutable WindowPolicy")
        object.__setattr__(self, "catalog_root", self.catalog_root.resolve())
        object.__setattr__(self, "runtime_root", self.runtime_root.resolve())
        if self.runtime_root == self.catalog_root or self.catalog_root.is_relative_to(self.runtime_root) or self.runtime_root.is_relative_to(self.catalog_root / "sources"):
            raise ValueError("runtime writes must not overlap canonical source paths")
        if not 0 <= self.publication_delay_seconds <= 86400:
            raise ValueError("publication_delay_seconds must be between 0 and 86400")
        if not 0 < self.query_timeout_seconds <= 30:
            raise ValueError("query_timeout_seconds must be in (0, 30]")
        if not 1 <= self.max_page_size <= 100:
            raise ValueError("max_page_size must be between 1 and 100")
        if not 1 <= self.max_query_length <= 200:
            raise ValueError("max_query_length must be between 1 and 200")

    @property
    def snapshots(self) -> Path:
        return self.runtime_root / "snapshots"

    @property
    def state_path(self) -> Path:
        return self.runtime_root / "state.db"
