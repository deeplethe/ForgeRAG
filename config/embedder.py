"""
Embedder configuration.

Two backends:

    litellm               -- unified interface to dozens of hosted
                             embedding APIs (OpenAI, Azure, Cohere,
                             Voyage, Bedrock, Vertex, Ollama, ...).
                             One install, one call signature.

    sentence_transformers -- local model execution via the
                             sentence-transformers library. Use for
                             GPU/CPU inference without a network call.

`dimension` is declared here and cross-checked against
persistence.vector.*.dimension at AppConfig load time so the
embedder and vector index can never drift apart silently.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class LiteLLMEmbedderConfig(BaseModel):
    # Any model string litellm understands. Examples:
    #   openai/text-embedding-3-small
    #   azure/my-deployment
    #   cohere/embed-multilingual-v3.0
    #   voyage/voyage-large-2
    #   ollama/bge-m3
    #   huggingface/BAAI/bge-large-en-v1.5
    model: str = "openai/text-embedding-3-small"
    # Authentication: use ONE of api_key (plaintext in yaml) or
    # api_key_env (name of an env var that holds the key).
    # api_key takes precedence if both are set.
    api_key: str | None = None  # direct key value
    api_key_env: str | None = None  # e.g. "OPENAI_API_KEY"
    api_base: str | None = None  # for self-hosted / ollama
    # OpenAI supports reducing output dim; pass through when set
    requested_dimensions: int | None = None
    timeout: float = 30.0


class SentenceTransformersConfig(BaseModel):
    model_name: str = "BAAI/bge-m3"
    device: Literal["cpu", "cuda", "mps"] = "cuda"
    trust_remote_code: bool = False
    normalize: bool = True  # unit-norm output vectors
    cache_folder: str | None = None  # local model cache dir


class EmbedderConfig(BaseModel):
    backend: Literal["litellm", "sentence_transformers"] = "litellm"
    litellm: LiteLLMEmbedderConfig | None = Field(default_factory=LiteLLMEmbedderConfig)
    sentence_transformers: SentenceTransformersConfig | None = None

    # Declared dimension -- must match the vector store's dimension.
    dimension: int = 1536

    # Batch size used by embed_texts() / embed_chunks()
    batch_size: int = 32

    # Retry policy for API backends
    max_retries: int = 3
    retry_base_delay: float = 1.0

    @model_validator(mode="after")
    def _check_section(self) -> EmbedderConfig:
        if self.backend == "litellm" and self.litellm is None:
            self.litellm = LiteLLMEmbedderConfig()
        if self.backend == "sentence_transformers" and self.sentence_transformers is None:
            raise ValueError(
                "embedder.backend=sentence_transformers but embedder.sentence_transformers section missing"
            )
        return self
