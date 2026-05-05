"""Answer-generation primitives.

Post-cutover only the ``Generator`` LLM wrapper + ``Answer``
dataclass survive. The ``AnsweringPipeline`` orchestration that
combined retrieval + prompt-building + generation was deleted —
``api/agent/loop.py`` now drives that flow with explicit tool
calls instead of a fixed pipeline.
"""

from .generator import Generator, LiteLLMGenerator, make_generator
from .types import Answer

__all__ = [
    "Answer",
    "Generator",
    "LiteLLMGenerator",
    "make_generator",
]
