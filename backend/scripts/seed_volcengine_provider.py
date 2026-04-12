"""One-shot seed: register the existing Volcengine env-var setup as a DB provider.

Run once after migrating to the Provider-configuration workflow:

    cd ~/Agentloom/backend
    conda run -n agentloom python -m scripts.seed_volcengine_provider

Idempotent — re-running skips if a provider with the same friendly_name exists.
The API key stays in the env var; we only store a reference to VOLCENGINE_API_KEY.
"""

from __future__ import annotations

import asyncio
import os
import sys

from agentloom.db.base import get_session_maker
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.provider import ProviderRepository
from agentloom.schemas.provider import ModelInfo, ProviderConfig, ProviderKind

FRIENDLY_NAME = "volcengine"
BASE_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"
ENV_VAR = "VOLCENGINE_API_KEY"


async def main() -> int:
    if not os.environ.get(ENV_VAR):
        print(f"error: {ENV_VAR} not set in environment", file=sys.stderr)
        return 1

    session_maker = get_session_maker()
    async with session_maker() as session:
        repo = ProviderRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)
        existing = await repo.list_all()
        if any(p["friendly_name"] == FRIENDLY_NAME for p in existing):
            print(f"provider '{FRIENDLY_NAME}' already registered — skipping")
            return 0

        config = ProviderConfig(
            friendly_name=FRIENDLY_NAME,
            provider_kind=ProviderKind.OPENAI_COMPAT,
            base_url=BASE_URL,
            api_key_source="env_var",
            api_key_env_var=ENV_VAR,
            available_models=[
                ModelInfo(id="ark-code-latest", pinned=True),
            ],
        )
        await repo.create(config)
        await session.commit()
        print(f"registered provider '{FRIENDLY_NAME}' (id={config.id})")
        print(f"  base_url: {BASE_URL}")
        print(f"  api key: from env var {ENV_VAR}")
        print(f"  pinned model: {config.available_models[0].id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
