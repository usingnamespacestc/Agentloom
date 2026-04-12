"""Provider configuration schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from agentloom.schemas.common import generate_node_id, utcnow


class ProviderKind(str, Enum):
    OPENAI_COMPAT = "openai_compat"
    ANTHROPIC_NATIVE = "anthropic_native"


class ModelInfo(BaseModel):
    """Metadata about one model exposed by a provider."""

    id: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = True
    supports_streaming: bool = True
    pinned: bool = False


ApiKeySource = Literal["inline", "env_var", "none"]


class ProviderConfig(BaseModel):
    """User-configured provider connection.

    One provider row = one API endpoint + key. Users can register multiple
    OpenAI-compat instances with different friendly_names (e.g., "DeepSeek
    primary" and "DeepSeek backup").
    """

    id: str = Field(default_factory=generate_node_id)
    friendly_name: str
    provider_kind: ProviderKind
    base_url: str
    api_key_source: ApiKeySource = "env_var"
    api_key_ciphertext: bytes | None = None  # set when api_key_source == "inline"
    api_key_env_var: str | None = None  # set when api_key_source == "env_var"
    available_models: list[ModelInfo] = Field(default_factory=list)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    rate_limit_bucket: str | None = None  # name of the HTB bucket to use
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _validate_key_fields(self) -> "ProviderConfig":
        if self.api_key_source == "inline":
            if self.api_key_env_var is not None:
                raise ValueError("inline key source cannot also set api_key_env_var")
        elif self.api_key_source == "env_var":
            if self.api_key_ciphertext is not None:
                raise ValueError("env_var key source cannot also set api_key_ciphertext")
        elif self.api_key_source == "none":
            # Keyless mode (local servers like Ollama, LM Studio).
            if self.api_key_env_var is not None or self.api_key_ciphertext is not None:
                raise ValueError("none key source cannot set env_var or ciphertext")
        return self

    def pinned_models(self) -> list[ModelInfo]:
        return [m for m in self.available_models if m.pinned]
