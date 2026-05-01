"""One-shot seed: register GLM (智谱 AI / Zhipu BigModel) as a DB provider.

Run once after setting ``GLM_API_KEY`` in your env::

    cd ~/Agentloom/backend
    conda run -n agentloom python -m scripts.seed_glm_provider

Idempotent — re-running skips if a provider with the same friendly_name
already exists. The API key stays in the env var; we only persist a
reference to ``GLM_API_KEY``.

GLM details:
- OpenAI-compatible chat completions wire format (``openai_compat`` adapter
  already supports it — see ``providers/openai_compat.py`` docstring)
- ``JsonMode.OBJECT`` (no full json_schema enforcement; the wire format
  supports ``response_format={"type":"json_object"}`` only)
- ``ProviderSubKind.OPENAI_CHAT`` — same param whitelist as OpenAI
  (temperature, top_p, max_tokens, presence/frequency_penalty)
"""

from __future__ import annotations

import asyncio
import os
import sys

from agentloom.db.base import get_session_maker
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.provider import ProviderRepository
from agentloom.schemas.provider import (
    JsonMode,
    ProviderConfig,
    ProviderKind,
    ProviderSubKind,
)

FRIENDLY_NAME = "glm"
BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ENV_VAR = "GLM_API_KEY"


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

        # Provider row only — no models attached. The user manages
        # the model lineup through the settings UI afterwards (discover
        # / pin / set per-model params there). available_models stays
        # empty so the seeder doesn't second-guess the catalog.
        config = ProviderConfig(
            friendly_name=FRIENDLY_NAME,
            provider_kind=ProviderKind.OPENAI_COMPAT,
            provider_sub_kind=ProviderSubKind.OPENAI_CHAT,
            base_url=BASE_URL,
            api_key_source="env_var",
            api_key_env_var=ENV_VAR,
            json_mode=JsonMode.OBJECT,
        )
        await repo.create(config)
        await session.commit()
        print(f"registered provider '{FRIENDLY_NAME}' (id={config.id})")
        print(f"  base_url:    {BASE_URL}")
        print(f"  api key:     from env var {ENV_VAR}")
        print(f"  sub_kind:    {config.provider_sub_kind.value}")
        print(f"  json_mode:   {config.json_mode.value}")
        print( "  models:      (none — add via settings UI)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
