"""
Parser backend abstract base class.

Every concrete backend (PyMuPDF, MinerU) implements ``ParserBackend``.
The pipeline picks exactly one backend (per ``parser.backend`` config)
and calls it. The legacy multi-tier router + fallback chain is gone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..blob_store import BlobStore
from ..schema import DocProfile, ParsedDocument


class BackendUnavailable(RuntimeError):
    """Raised when a backend's optional dependency / model / hardware
    is missing. Surfaced to the user as an actionable error rather
    than silently falling back."""


class ParserBackend(ABC):
    #: short name used in logs / ``ParseTrace.backend`` (e.g. "pymupdf")
    name: str

    def __init__(self, blob_store: BlobStore):
        self.blob_store = blob_store

    @abstractmethod
    def parse(
        self,
        path: str,
        doc_id: str,
        parse_version: int,
        profile: DocProfile,
    ) -> ParsedDocument:
        """
        Parse *path* into a ``ParsedDocument``. Must populate doc_id,
        parse_version, profile, blocks, and pages. ``parse_trace`` is
        attached by the pipeline, not the backend.

        Raise ``BackendUnavailable`` if preconditions fail at call time
        (model download error, GPU OOM, missing optional dep, etc.) so
        the user gets a clear "fix your config / install deps" message.
        """
