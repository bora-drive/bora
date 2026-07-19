"""One-way export of native Google Workspace files to Office/Markdown.

A native Google Doc has no file extension in Drive; when export is enabled we
give it one on disk (``Report`` -> ``Report.docx``). To keep the sync index,
the ``PathIDCache`` key, and the on-disk name in agreement, the *projected*
leaf name is used as the canonical ``remote_path`` everywhere; the raw Drive
name survives only in ``PathIDCache.name`` (used for id-keyed API calls).
:func:`project_leaf_name` is the single projection applied at the three
``compose_path`` chokepoints (changes feed, folder listing, cache rebuild).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from .native_docs import export_display_name, is_exportable

if TYPE_CHECKING:
    from .config.user import UserConfig


@dataclass(frozen=True)
class ExportOptions:
    enabled: bool = False
    markdown: bool = False

    @classmethod
    def disabled(cls) -> "ExportOptions":
        return cls(enabled=False, markdown=False)

    @classmethod
    def from_config(cls, conf: "UserConfig") -> "ExportOptions":
        return cls(
            enabled=bool(conf.get("sync", "export_google_docs")),
            markdown=bool(conf.get("sync", "export_google_docs_markdown")),
        )


def project_leaf_name(raw_name: str, mime: str | None, opts: ExportOptions) -> str:
    """Return the on-disk / canonical leaf name for *raw_name*.

    Unchanged unless export is enabled and *mime* is exportable, in which case
    the native extension is appended (``.md`` for a Doc when markdown is on).
    """
    if opts.enabled and is_exportable(mime):
        return export_display_name(raw_name, cast(str, mime), opts.markdown)
    return raw_name
