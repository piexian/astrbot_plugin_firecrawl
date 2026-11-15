"""firecrawl_agent 执行链路：真实 AgentStore + BudgetedExecutor + MockTransport。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from astrbot_plugin_firecrawl.agent.service import AgentService
from astrbot_plugin_firecrawl.agent.store import AgentStore
from astrbot_plugin_firecrawl.firecrawl_api.budget import BudgetPolicy, ReservationState
from astrbot_plugin_firecrawl.firecrawl_api.client import FirecrawlClient
from astrbot_plugin_firecrawl.firecrawl_api.keys import KeyPool, key_reference
from astrbot_plugin_firecrawl.firecrawl_api.tasks import make_task_handle
from astrbot_plugin_firecrawl.persistence import SQLiteStore
from astrbot_plugin_firecrawl.tools.execution import BudgetedExecutor

_STAMP = "2026-01-01T12:00:00+00:00"
_JOB_ID = "job-1"
_K1 = "fc-aaaaaaaa1111"
_UMO = "qq:group:1"
_PROMPT = "调研 X"
_MAX_CREDITS = 30


def fixed_clock() -> datetime:
    """固定 UTC 时刻。"""
    return datetime.fromisoformat(_STAMP)


class StubPlugin:
    """提供 executor、request_scope 与 config 的最小插件替身。"""

    def __init__(self, executor, config: dict | None = None) -> None:
        self.executor = executor
        self.config = config or {"max_content_chars": 8000}
        self.scope_entries = 0
        self.agent_service = None

    @asynccontextmanager
    async def request_scope(self):
        self.scope_entries += 1
        yield

    async def send_message(self, umo: str, chain) -> bool | None:
        return True

    @property
    def context(self):
        return SimpleNamespace(send_message=self.send_message)


class StubEvent:
    """管理员事件替身：提供 is_admin 与会话来源。"""

    def __init__(self, is_admin: bool = True, umo: str = _UMO) -> None:
        self._is_admin = is_admin
        self.unified_msg_origin = umo

    def is_admin(self) -> bool:
        return self._is_admin


def admin_context() -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(event=StubEvent()))


async def _noop_track(_task_handle: str) -> None:
    """暂停后台 track，避免额外 GET。"""
    return None


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def make_transport(responses: list, on_request=None):
    """按顺序回放响应，并记录请求。"""
    requests: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if on_request is not None:
            on_request(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), requests


async def ready_real(tmp_path, responses: list, on_request=None, *, config=None):
    """真实 AgentStore + BudgetedExecutor + MockTransport。"""
    transport, requests = make_transport(responses, on_request)
    client = FirecrawlClient(KeyPool([_K1]), transport=transport)
    db = tmp_path / "firecrawl.db"
    budget = SQLiteStore(db, policy=BudgetPolicy(), clock=fixed_clock)
    await budget.initialize()
    store = AgentStore(db, clock=fixed_clock)
    await store.initialize()
    executor = BudgetedExecutor(client, budget)
    plugin = StubPlugin(executor, config=config)
    service = AgentService(plugin, store)
    plugin.agent_service = service
    return plugin, store, service, executor, requests


@pytest.mark.asyncio
async def test_start_then_status_reserves_max_credits(sdk_tool_loader, tmp_path):
    """start→status：按 maxCredits 预留、单次提交、processing 不结算、结果无原始 Key。"""
    handle = make_task_handle("agent", _JOB_ID, key_reference(_K1))
    plugin, store, service, executor, requests = await ready_real(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": _JOB_ID}),
            httpx.Response(200, json={"success": True, "status": "processing"}),
        ],
        config={"blocked_domains": [], "max_content_chars": 8000},
    )
    service.track = _noop_track
    try:
        with sdk_tool_loader("agent", "FirecrawlAgentTool") as tool_class:
            tool = tool_class(plugin=plugin)
            start = json.loads(
                await tool.call(
                    admin_context(),
                    action="start",
                    prompt=_PROMPT,
                    max_credits=_MAX_CREDITS,
                )
            )
            assert start["ok"] is True
            assert start["data"]["task_handle"] == handle
            assert start["data"]["status"] == "processing"
            assert "pending" not in start["data"]
            budget = await executor._store.usage()
            assert budget.used == 0.0
            assert budget.active_reserved == float(_MAX_CREDITS)
            reservation = await executor._store.get(start["data"]["reservation_id"])
            assert reservation.state is ReservationState.ACCEPTED
            assert reservation.estimate == _MAX_CREDITS
            assert reservation.accounted_credits == 0.0

            status = json.loads(
                await tool.call(
                    admin_context(),
                    action="status",
                    task_handle=handle,
                )
            )
            assert status["ok"] is True
            assert status["data"]["status"] == "processing"
            assert status["data"]["task_handle"] == handle
            budget = await executor._store.usage()
            assert budget.used == 0.0
            assert budget.active_reserved == float(_MAX_CREDITS)
            assert [item.method for item in requests] == ["POST", "GET"]
            assert json.loads(requests[0].content)["maxCredits"] == _MAX_CREDITS
            assert requests[0].headers["authorization"] == f"Bearer {_K1}"
            assert requests[1].headers["authorization"] == f"Bearer {_K1}"
            assert _K1 not in _dump(start)
            assert _K1 not in _dump(status)
            recorded = await store.get(start["data"]["reservation_id"])
            assert recorded.status == "processing"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_start_then_cancel_then_status_settles_actual(sdk_tool_loader, tmp_path):
    """start→cancel→status：DELETE 不结算，终态 GET 按实际费用入账，结果无原始 Key。"""
    handle = make_task_handle("agent", _JOB_ID, key_reference(_K1))
    seen_states: list[tuple[str, str, float]] = []
    db_holder: dict = {}

    def on_request(request: httpx.Request) -> None:
        conn = sqlite3.connect(db_holder["path"])
        try:
            row = conn.execute(
                "SELECT state, accounted_credits FROM reservations ORDER BY rowid"
            ).fetchone()
        finally:
            conn.close()
        seen_states.append((request.method, row[0], float(row[1])))

    plugin, store, service, executor, requests = await ready_real(
        tmp_path,
        [
            httpx.Response(200, json={"success": True, "id": _JOB_ID}),
            httpx.Response(200, json={"success": True}),
            httpx.Response(
                200,
                json={"success": True, "status": "cancelled", "creditsUsed": 4},
            ),
            httpx.Response(
                200,
                json={"success": True, "status": "cancelled", "creditsUsed": 4},
            ),
        ],
        on_request=on_request,
        config={"blocked_domains": [], "max_content_chars": 8000},
    )
    db_holder["path"] = executor._store._db_path
    service.track = _noop_track
    try:
        with sdk_tool_loader("agent", "FirecrawlAgentTool") as tool_class:
            tool = tool_class(plugin=plugin)
            start = json.loads(
                await tool.call(
                    admin_context(),
                    action="start",
                    prompt=_PROMPT,
                    max_credits=_MAX_CREDITS,
                )
            )
            assert start["ok"] is True
            rid = start["data"]["reservation_id"]
            before_cancel = await executor._store.usage()
            assert before_cancel.used == 0.0
            assert before_cancel.active_reserved == float(_MAX_CREDITS)

            cancel = json.loads(
                await tool.call(
                    admin_context(),
                    action="cancel",
                    task_handle=handle,
                )
            )
            assert cancel["ok"] is True
            assert cancel["data"]["status"] == "cancelled"
            assert cancel["data"]["cancel_requested"] is True
            assert cancel["data"]["creditsUsed"] == 4
            assert cancel["data"]["task_handle"] == handle
            after_cancel = await executor._store.usage()
            assert after_cancel.used == 4.0
            assert after_cancel.active_reserved == 0.0
            reservation = await executor._store.get(rid)
            assert reservation.state is ReservationState.SETTLED
            assert reservation.actual_credits == 4.0

            status = json.loads(
                await tool.call(
                    admin_context(),
                    action="status",
                    task_handle=handle,
                )
            )
            assert status["ok"] is True
            assert status["data"]["status"] == "cancelled"
            assert status["data"]["creditsUsed"] == 4
            assert status["data"]["task_handle"] == handle
            after_status = await executor._store.usage()
            assert after_status.used == 4.0
            assert after_status.active_reserved == 0.0
            assert [item.method for item in requests] == [
                "POST",
                "DELETE",
                "GET",
                "GET",
            ]
            assert requests[1].method == "DELETE"
            assert seen_states[1] == ("DELETE", "accepted", 0.0)
            assert seen_states[2] == ("GET", "accepted", 0.0)
            assert requests[1].headers["authorization"] == f"Bearer {_K1}"
            assert requests[2].headers["authorization"] == f"Bearer {_K1}"
            assert sum(1 for item in requests if item.method == "POST") == 1
            for payload in (start, cancel, status):
                dumped = _dump(payload)
                assert _K1 not in dumped
                assert "Bearer" not in dumped
            recorded = await store.get(rid)
            assert recorded.status == "cancelled"
            assert recorded.credits_used == 4.0
    finally:
        await service.close()
