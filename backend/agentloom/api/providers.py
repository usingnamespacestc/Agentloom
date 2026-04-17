"""Provider REST endpoints.

Surface:
- ``GET    /api/providers``               list all providers
- ``POST   /api/providers``               create provider
- ``GET    /api/providers/{id}``          get provider config
- ``PATCH  /api/providers/{id}``          update provider
- ``DELETE /api/providers/{id}``          delete provider
- ``POST   /api/providers/{id}/test``     test connection
- ``POST   /api/providers/{id}/models``   discover models from remote
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agentloom.db.base import get_session
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.provider import ProviderNotFoundError, ProviderRepository
from agentloom.providers.registry import build_adapter
from agentloom.schemas.provider import (
    JsonMode,
    ModelInfo,
    ProviderConfig,
    ProviderKind,
    ProviderSubKind,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])


def _repo(session: AsyncSession) -> ProviderRepository:
    return ProviderRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)


# ---------------------------------------------------------------- schemas


class CreateProviderRequest(BaseModel):
    friendly_name: str
    provider_kind: ProviderKind
    provider_sub_kind: ProviderSubKind | None = None
    base_url: str
    api_key_source: str = "env_var"
    api_key_env_var: str | None = None
    api_key_inline: str | None = None  # plaintext in MVP
    available_models: list[ModelInfo] = []
    rate_limit_bucket: str | None = None
    json_mode: JsonMode = JsonMode.NONE


class PatchProviderRequest(BaseModel):
    friendly_name: str | None = None
    provider_sub_kind: ProviderSubKind | None = None
    base_url: str | None = None
    api_key_source: str | None = None
    api_key_env_var: str | None = None
    api_key_inline: str | None = None
    available_models: list[ModelInfo] | None = None
    rate_limit_bucket: str | None = None
    json_mode: JsonMode | None = None


class TestConnectionRequest(BaseModel):
    """Optional override for testing before saving."""
    api_key: str | None = None


# ---------------------------------------------------------------- routes


@router.get("")
async def list_providers(
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    return await _repo(session).list_all()


@router.post("")
async def create_provider(
    body: CreateProviderRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    config = ProviderConfig(
        friendly_name=body.friendly_name,
        provider_kind=body.provider_kind,
        provider_sub_kind=body.provider_sub_kind,
        base_url=body.base_url,
        api_key_source=body.api_key_source,  # type: ignore[arg-type]
        api_key_env_var=body.api_key_env_var if body.api_key_source == "env_var" else None,
        api_key_ciphertext=(
            body.api_key_inline.encode()
            if body.api_key_inline and body.api_key_source == "inline"
            else None
        ),
        available_models=body.available_models,
        rate_limit_bucket=body.rate_limit_bucket,
        json_mode=body.json_mode,
    )
    repo = _repo(session)
    row = await repo.create(config)
    await session.commit()
    return {"id": row.id, "friendly_name": row.friendly_name}


@router.get("/{provider_id}")
async def get_provider(
    provider_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    try:
        config = await repo.get(provider_id)
    except ProviderNotFoundError as exc:
        raise HTTPException(404, f"provider {provider_id} not found") from exc
    # Never send api_key_ciphertext to the frontend.
    data = config.model_dump(mode="json")
    data.pop("api_key_ciphertext", None)
    return data


@router.patch("/{provider_id}")
async def patch_provider(
    provider_id: str,
    body: PatchProviderRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    try:
        config = await repo.get(provider_id)
    except ProviderNotFoundError as exc:
        raise HTTPException(404, f"provider {provider_id} not found") from exc

    provided = body.model_fields_set
    if "friendly_name" in provided and body.friendly_name is not None:
        config.friendly_name = body.friendly_name
    if "provider_sub_kind" in provided:
        config.provider_sub_kind = body.provider_sub_kind
    if "base_url" in provided and body.base_url is not None:
        config.base_url = body.base_url
    if "api_key_source" in provided and body.api_key_source is not None:
        config.api_key_source = body.api_key_source  # type: ignore[assignment]
    if "api_key_env_var" in provided:
        config.api_key_env_var = body.api_key_env_var
    if "api_key_inline" in provided and body.api_key_inline is not None:
        config.api_key_ciphertext = body.api_key_inline.encode()
    # Enforce cross-field invariants after any source change.
    if config.api_key_source == "none":
        config.api_key_env_var = None
        config.api_key_ciphertext = None
    elif config.api_key_source == "env_var":
        config.api_key_ciphertext = None
    elif config.api_key_source == "inline":
        config.api_key_env_var = None
    if "available_models" in provided and body.available_models is not None:
        config.available_models = body.available_models
    if "rate_limit_bucket" in provided:
        config.rate_limit_bucket = body.rate_limit_bucket
    if "json_mode" in provided and body.json_mode is not None:
        config.json_mode = body.json_mode

    # Re-run model validators (sub_kind/param whitelist, key-source
    # invariants) — field assignment alone doesn't trigger them.
    try:
        config = ProviderConfig.model_validate(config.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    await repo.save(config)
    await session.commit()
    return {"ok": True}


@router.delete("/{provider_id}")
async def delete_provider(
    provider_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    try:
        await repo.delete(provider_id)
    except ProviderNotFoundError as exc:
        raise HTTPException(404, f"provider {provider_id} not found") from exc
    await session.commit()
    return {"ok": True}


@router.post("/{provider_id}/test")
async def test_connection(
    provider_id: str,
    body: TestConnectionRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Test that the provider's API key and base_url are reachable."""
    repo = _repo(session)
    try:
        config = await repo.get(provider_id)
    except ProviderNotFoundError as exc:
        raise HTTPException(404, f"provider {provider_id} not found") from exc

    api_key = (body.api_key if body and body.api_key else None) or repo.resolve_api_key(config)

    try:
        adapter = build_adapter(
            kind=config.provider_kind.value,
            friendly_name=config.friendly_name,
            base_url=config.base_url,
            api_key=api_key,
            sub_kind=config.provider_sub_kind.value if config.provider_sub_kind else None,
        )
        models = await adapter.list_models()
        await adapter.close()
        return {"ok": True, "models": models}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/{provider_id}/models")
async def discover_models(
    provider_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fetch available models from the provider and store them."""
    repo = _repo(session)
    try:
        config = await repo.get(provider_id)
    except ProviderNotFoundError as exc:
        raise HTTPException(404, f"provider {provider_id} not found") from exc

    api_key = repo.resolve_api_key(config)

    try:
        adapter = build_adapter(
            kind=config.provider_kind.value,
            friendly_name=config.friendly_name,
            base_url=config.base_url,
            api_key=api_key,
            sub_kind=config.provider_sub_kind.value if config.provider_sub_kind else None,
        )
        model_ids = await adapter.list_models()
        await adapter.close()
    except Exception as exc:
        raise HTTPException(502, f"Failed to fetch models: {exc}") from exc

    # Merge: keep existing ModelInfo for known IDs, add new ones.
    existing = {m.id: m for m in config.available_models}
    for mid in model_ids:
        if mid not in existing:
            existing[mid] = ModelInfo(id=mid)
    config.available_models = list(existing.values())
    await repo.save(config)
    await session.commit()
    return {"models": [m.model_dump() for m in config.available_models]}
