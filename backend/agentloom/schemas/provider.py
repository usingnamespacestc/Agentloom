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


class ProviderSubKind(str, Enum):
    """Fine-grained classification within a ``ProviderKind``.

    Drives which generation parameters are legal on that provider's
    models and which request-body shape the adapter uses. ``None`` on
    a ``ProviderConfig`` means "admin has not classified this provider
    yet" — the UI forces a pick before params can be edited and the
    adapter falls back to a permissive superset at call time.

    Families:
      - ``openai_chat``: OpenAI Chat Completions API (temperature, top_p,
        max_tokens, presence_penalty, frequency_penalty). Strict — 400s
        on unknown keys. ``openai_compat`` parent.
      - ``ollama``: local Ollama over OpenAI-compat endpoint. Adds
        top_k, repetition_penalty, num_ctx. ``openai_compat`` parent.
      - ``volcengine``: Ark / Doubao. Same keys as openai_chat.
        ``openai_compat`` parent.
      - ``anthropic``: Anthropic Messages API. temperature, top_p,
        top_k, max_tokens, thinking_budget_tokens. ``anthropic_native``
        parent.
    """

    OPENAI_CHAT = "openai_chat"
    OLLAMA = "ollama"
    VOLCENGINE = "volcengine"
    ANTHROPIC = "anthropic"


#: Allowed ``ModelInfo`` generation-param keys per ``ProviderSubKind``.
#: Used by ``ProviderConfig._validate_sub_kind_params`` and by the
#: frontend to render per-sub_kind param editors.
SUB_KIND_PARAM_WHITELIST: dict[ProviderSubKind, frozenset[str]] = {
    ProviderSubKind.OPENAI_CHAT: frozenset(
        {"temperature", "top_p", "max_output_tokens", "presence_penalty", "frequency_penalty"}
    ),
    ProviderSubKind.OLLAMA: frozenset(
        {"temperature", "top_p", "top_k", "max_output_tokens", "repetition_penalty", "num_ctx"}
    ),
    ProviderSubKind.VOLCENGINE: frozenset(
        {"temperature", "top_p", "max_output_tokens", "presence_penalty", "frequency_penalty"}
    ),
    ProviderSubKind.ANTHROPIC: frozenset(
        {"temperature", "top_p", "top_k", "max_output_tokens", "thinking_budget_tokens"}
    ),
}


class JsonMode(str, Enum):
    """How a provider/model accepts structured-JSON output requests.

    - ``schema``: full JSON Schema via ``response_format={"type":"json_schema", ...}``
      (Ollama, newer Ark Doubao, OpenAI gpt-4o+).
    - ``object``: free-form JSON only via ``response_format={"type":"json_object"}``
      (DeepSeek, Moonshot, GLM).
    - ``none``: neither accepted — fall back to prompt-only JSON discipline.
    """

    SCHEMA = "schema"
    OBJECT = "object"
    NONE = "none"


class ModelInfo(BaseModel):
    """Metadata about one model exposed by a provider."""

    id: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = True
    supports_streaming: bool = True
    pinned: bool = False
    #: Per-model override of the provider-level ``json_mode``. ``None``
    #: means "inherit from provider" so users only need to set this when
    #: a single model in a mixed-capability fleet diverges from the
    #: provider default (e.g. an older DeepSeek model that only supports
    #: ``object`` while newer ones would support ``schema``).
    json_mode: JsonMode | None = None
    #: Per-model sampling parameters. ``None`` means "don't send — let the
    #: model use its own default". Adapters pass these through to the wire
    #: format, gated by the parent provider's ``provider_sub_kind`` (see
    #: :data:`SUB_KIND_PARAM_WHITELIST`). ``frequency_penalty`` is the
    #: OpenAI-family counterpart to ``repetition_penalty`` (Ollama); they
    #: are never both sent. ``num_ctx`` is Ollama-only; ``thinking_budget_tokens``
    #: is Anthropic-only.
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    repetition_penalty: float | None = None
    num_ctx: int | None = None
    thinking_budget_tokens: int | None = None


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
    #: Fine-grained family within ``provider_kind``. ``None`` means the
    #: admin has not classified this provider yet — UI forces a pick
    #: before model params can be edited. See :class:`ProviderSubKind`.
    provider_sub_kind: ProviderSubKind | None = None
    base_url: str
    api_key_source: ApiKeySource = "env_var"
    api_key_ciphertext: bytes | None = None  # set when api_key_source == "inline"
    api_key_env_var: str | None = None  # set when api_key_source == "env_var"
    available_models: list[ModelInfo] = Field(default_factory=list)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    rate_limit_bucket: str | None = None  # name of the HTB bucket to use
    #: Provider-level default for structured-JSON output. Individual
    #: models may override via :attr:`ModelInfo.json_mode`; resolution is
    #: done at call-time (``model.json_mode or provider.json_mode``).
    json_mode: JsonMode = JsonMode.NONE
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

    @model_validator(mode="after")
    def _validate_sub_kind_params(self) -> "ProviderConfig":
        if self.provider_sub_kind is None:
            return self
        allowed = SUB_KIND_PARAM_WHITELIST[self.provider_sub_kind]
        param_fields = (
            "temperature",
            "top_p",
            "top_k",
            "max_output_tokens",
            "presence_penalty",
            "frequency_penalty",
            "repetition_penalty",
            "num_ctx",
            "thinking_budget_tokens",
        )
        for m in self.available_models:
            for field in param_fields:
                if getattr(m, field, None) is not None and field not in allowed:
                    raise ValueError(
                        f"model {m.id}: param {field!r} not allowed for "
                        f"provider_sub_kind={self.provider_sub_kind.value}"
                    )
        return self

    def pinned_models(self) -> list[ModelInfo]:
        return [m for m in self.available_models if m.pinned]
