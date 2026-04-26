"""Unit tests for ``TauBenchBackendClient`` — backend HTTP wrapper.

Uses httpx ``MockTransport`` so we don't need a running backend; just
verifies the client constructs the right requests + parses responses
into typed dataclasses.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agentloom_bench.tau_bench.client import TauBenchBackendClient


@pytest.mark.asyncio
async def test_create_session_posts_correct_body_and_parses():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "session_id": "abc",
                "chatflow_id": "abc",
                "domain": "retail",
                "task_index": 7,
                "instruction": "do the thing",
                "num_tools": 16,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        client = TauBenchBackendClient(http)
        info = await client.create_session(domain="retail", task_index=7)

    assert captured["url"].endswith("/api/tau-bench/sessions")
    assert captured["body"] == {"domain": "retail", "task_index": 7}
    assert info.session_id == "abc"
    assert info.num_tools == 16
    assert info.instruction == "do the thing"


@pytest.mark.asyncio
async def test_create_session_includes_optional_fields():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "session_id": "x",
                "chatflow_id": "x",
                "domain": "airline",
                "task_index": 0,
                "instruction": "",
                "num_tools": 14,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://t"
    ) as http:
        client = TauBenchBackendClient(http)
        await client.create_session(
            domain="airline",
            task_index=0,
            agent_model={"provider_id": "p", "model_id": "m"},
            title="custom",
        )
    assert captured["body"]["agent_model"] == {"provider_id": "p", "model_id": "m"}
    assert captured["body"]["title"] == "custom"


@pytest.mark.asyncio
async def test_submit_turn_posts_to_chatflow_endpoint():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"node_id": "n1", "status": "succeeded", "agent_response": "hello"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://t"
    ) as http:
        client = TauBenchBackendClient(http)
        result = await client.submit_turn("cf-1", "hi there")

    assert captured["url"].endswith("/api/chatflows/cf-1/turns")
    assert captured["body"] == {"text": "hi there"}
    assert result.agent_response == "hello"
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_submit_turn_with_parent_id():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"node_id": "n2", "status": "succeeded", "agent_response": ""},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://t"
    ) as http:
        client = TauBenchBackendClient(http)
        await client.submit_turn("cf-1", "msg", parent_id="parent-x")
    assert captured["body"]["parent_id"] == "parent-x"


@pytest.mark.asyncio
async def test_teardown_returns_response_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "session_id": "s", "unregistered_tools": 16}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://t"
    ) as http:
        client = TauBenchBackendClient(http)
        result = await client.teardown_session("s")
    assert result["unregistered_tools"] == 16


@pytest.mark.asyncio
async def test_create_session_propagates_http_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "task index out of range"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://t"
    ) as http:
        client = TauBenchBackendClient(http)
        with pytest.raises(httpx.HTTPStatusError):
            await client.create_session(domain="retail", task_index=99999)
