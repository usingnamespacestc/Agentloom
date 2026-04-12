"""Provider repository — CRUD for ProviderConfig via JSONB payload."""

from __future__ import annotations

import os

from sqlalchemy import select

from agentloom.db.models.provider import ProviderRow
from agentloom.db.repositories.base import WorkspaceScopedRepository
from agentloom.schemas.provider import ProviderConfig


class ProviderNotFoundError(KeyError):
    pass


class ProviderRepository(WorkspaceScopedRepository):
    async def create(self, config: ProviderConfig, owner_id: str | None = None) -> ProviderRow:
        row = ProviderRow(
            id=config.id,
            workspace_id=self.workspace_id,
            owner_id=owner_id,
            friendly_name=config.friendly_name,
            provider_kind=config.provider_kind.value,
            base_url=config.base_url,
            payload=config.model_dump(mode="json"),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, provider_id: str) -> ProviderConfig:
        stmt = (
            select(ProviderRow)
            .where(ProviderRow.workspace_id == self.workspace_id)
            .where(ProviderRow.id == provider_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ProviderNotFoundError(provider_id)
        return ProviderConfig.model_validate(row.payload)

    async def list_all(self) -> list[dict]:
        """Return lightweight summaries for all providers."""
        stmt = (
            select(
                ProviderRow.id,
                ProviderRow.friendly_name,
                ProviderRow.provider_kind,
                ProviderRow.base_url,
                ProviderRow.payload,
                ProviderRow.created_at,
                ProviderRow.updated_at,
            )
            .where(ProviderRow.workspace_id == self.workspace_id)
            .order_by(ProviderRow.created_at)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "id": r.id,
                "friendly_name": r.friendly_name,
                "provider_kind": r.provider_kind,
                "base_url": r.base_url,
                "available_models": (r.payload or {}).get("available_models", []),
                "api_key_source": (r.payload or {}).get("api_key_source", "env_var"),
                "api_key_env_var": (r.payload or {}).get("api_key_env_var"),
                "rate_limit_bucket": (r.payload or {}).get("rate_limit_bucket"),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    async def save(self, config: ProviderConfig) -> None:
        """Overwrite an existing provider config."""
        stmt = (
            select(ProviderRow)
            .where(ProviderRow.workspace_id == self.workspace_id)
            .where(ProviderRow.id == config.id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ProviderNotFoundError(config.id)
        row.friendly_name = config.friendly_name
        row.provider_kind = config.provider_kind.value
        row.base_url = config.base_url
        row.payload = config.model_dump(mode="json")
        await self.session.flush()

    async def delete(self, provider_id: str) -> None:
        stmt = (
            select(ProviderRow)
            .where(ProviderRow.workspace_id == self.workspace_id)
            .where(ProviderRow.id == provider_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ProviderNotFoundError(provider_id)
        await self.session.delete(row)
        await self.session.flush()

    def resolve_api_key(self, config: ProviderConfig) -> str | None:
        """Resolve the actual API key string from config.

        For env_var source, reads the env var. For inline, returns
        the ciphertext as-is (encryption is a v1.1+ concern). For
        none, always returns None (keyless local servers).
        """
        if config.api_key_source == "none":
            return None
        if config.api_key_source == "env_var":
            var_name = config.api_key_env_var
            return os.environ.get(var_name) if var_name else None
        # inline — MVP stores plaintext in the JSONB (encryption is v1.1+)
        ct = config.api_key_ciphertext
        return ct.decode() if isinstance(ct, bytes) else ct
