"""AstrBot Firecrawl 插件入口：配置装配、运行时生命周期、LLM 工具与 fc 管理指令。"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from .firecrawl_api.budget import BudgetPolicy
from .firecrawl_api.client import FirecrawlClient
from .firecrawl_api.errors import (
    ErrorCategory,
    FirecrawlError,
    friendly_message,
    mask_key,
)
from .firecrawl_api.keys import KeyPool
from .persistence import SQLiteStore
from .tools.execution import BudgetedExecutor
from .tools.scrape import FirecrawlScrapeTool
from .tools.search import FirecrawlSearchTool
from .tools.site import FirecrawlSiteTool

_DEFAULT_BASE_URL = "https://api.firecrawl.dev"
_DB_FILENAME = "firecrawl.sqlite3"
_RUNTIME_UNAVAILABLE = "Firecrawl 插件运行时不可用：未初始化或正在关闭。"

_HELP_TEXT = (
    "Firecrawl 插件指令：\n"
    "/fc help — 显示本帮助\n"
    "/fc credit — 查询团队额度与 Key 冷却状态（管理员）\n"
    "/fc pending list — 查看待决预算预留（管理员）\n"
    "/fc pending resolve <预留ID> <实际credits> — 结算或确认未受理，0 表示未受理（管理员）"
)
_CREDIT_USAGE_TEXT = "用法：/fc credit。"
_NO_KEY_TEXT = "尚未配置 Firecrawl API Key，无法查询额度。"
_USAGE_REMOTE_TEXT = "Firecrawl 拒绝了该查询：用法或参数受限。"
_PENDING_USAGE_TEXT = (
    "用法：/fc pending list 或 /fc pending resolve <预留ID> <实际credits>。"
)
_PENDING_READ_FAILED_TEXT = "读取本地预算数据失败，请稍后重试。"
_RESOLVE_USAGE_TEXT = (
    "用法：/fc pending resolve <预留ID> <实际credits>；"
    "实际 credits 为非负有限小数，0 表示确认未受理。"
)
_RESOLVE_INVALID_TEXT = "预留 ID 或金额参数无效。"
_RESOLVE_NOT_ALLOWED_TEXT = "预留不存在或当前状态不允许该操作。"
_RESOLVE_FAILED_TEXT = "处理预留失败，请稍后重试。"
_OUTPUT_LIMIT = 6000
_PENDING_LIST_LIMIT = 20
_CREDIT_ENDPOINT = "/team/credit-usage"
_AMOUNT_PATTERN = re.compile(r"\d{1,12}(?:\.\d{1,6})?")


def _truncate(text: str) -> str:
    """输出统一截断到固定字符上限。"""
    if len(text) <= _OUTPUT_LIMIT:
        return text
    return text[: _OUTPUT_LIMIT - 1] + "…"


def _fmt_credits(value: float) -> str:
    """额度数值用十进制短格式显示。"""
    return f"{value:g}"


def _credit_label(pool: KeyPool, ref: str, index: int) -> str:
    """输出摘要前缀与查询后的白名单冷却状态。"""
    states = dict(zip(pool.key_refs, pool.summary()["keys"], strict=True))
    state = states[ref]
    status = "正常"
    if state["cooling"]:
        status = (
            f"冷却中：{state['cooldown_reason']}，"
            f"剩余 {state['cooldown_seconds_left']} 秒"
        )
    return f"[{index}] sha256:{ref[:8]}（{status}）"


def _is_amount(value: object) -> bool:
    """额度字段必须是非布尔的有限非负数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _parse_credits(text: str) -> float | None:
    """严格解析非负有限小数金额；缺参、NaN/Inf、负数、超长输入一律拒绝。"""
    if not text or len(text) > 16:
        return None
    if _AMOUNT_PATTERN.fullmatch(text) is None:
        return None
    return float(text)


def _short_ref(key_ref: str | None) -> str:
    """key_ref 只显示 sha256 前 8 位短摘要。"""
    if not key_ref:
        return "未知"
    return "sha256:" + key_ref[:8]


def _short_job(job_id: str | None) -> str:
    """job_id 只显示前 8 位短值。"""
    if not job_id:
        return "-"
    return job_id[:8] + "…"


def _safe_error_text(err: FirecrawlError) -> str:
    """远端错误只显示分类固定提示，不透传服务端 message/details。"""
    if err.category is ErrorCategory.USAGE:
        return _USAGE_REMOTE_TEXT
    return friendly_message(err)


def _credit_fields(body: object) -> tuple[float, float, str] | None:
    """白名单读取 data 内的额度字段；缺失或类型不符返回 None。"""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    remaining = data.get("remainingCredits")
    plan = data.get("planCredits")
    if not _is_amount(remaining) or not _is_amount(plan):
        return None
    start = data.get("billingPeriodStart")
    end = data.get("billingPeriodEnd")
    valid_start = (
        isinstance(start, str)
        and 0 < len(start) <= 64
        and "\n" not in start
        and "\r" not in start
    )
    valid_end = (
        isinstance(end, str)
        and 0 < len(end) <= 64
        and "\n" not in end
        and "\r" not in end
    )
    period = (
        f"{mask_key(start)} ~ {mask_key(end)}" if valid_start and valid_end else "未知"
    )
    return float(remaining), float(plan), period


def _normalize_api_keys(raw: object) -> list[str]:
    """api_keys 规范化：字符串视为单项，空值得到空池，无效类型按空池告警。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list | None = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = None
    if items is None:
        logger.warning("api_keys 配置不是字符串或列表，已按空 Key 池处理。")
        return []
    keys = []
    for item in items:
        key = str(item or "").strip()
        if key:
            keys.append(key)
    return keys


def _int_option(config: dict, name: str, default: int) -> int:
    """整数配置读取：缺失用默认值，不可转换抛固定文案且不回显原值。"""
    value = config.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"配置项 {name} 必须是整数。")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"配置项 {name} 必须是整数。") from None


class FirecrawlPlugin(Star):
    """Firecrawl 网页抓取插件：注册 LLM 工具并管理客户端、预算与并发。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.key_pool: KeyPool | None = None
        self.client: FirecrawlClient | None = None
        self.store: SQLiteStore | None = None
        self.executor: BudgetedExecutor | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._active_scopes: set[object] = set()
        self._drained = asyncio.Event()
        self._drained.set()
        self._closing = False
        self._terminated = False

    async def initialize(self) -> None:
        """装配运行时：预算存储、恢复、Key 池、客户端与工具注册，失败即回滚。"""
        if self.executor is not None:
            return
        self._closing = False
        self._terminated = False
        keys = _normalize_api_keys(self.config.get("api_keys", []))
        policy = BudgetPolicy(
            max_estimated_credits_per_call=(
                _int_option(self.config, "max_estimated_credits_per_call", 0)
            ),
            daily_credit_limit=_int_option(self.config, "daily_credit_limit", 0),
        )
        client: FirecrawlClient | None = None
        try:
            data_dir = StarTools.get_data_dir()
            store = SQLiteStore(data_dir / _DB_FILENAME, policy=policy)
            await store.initialize()
            pending = await store.recover()
            logger.info(f"Firecrawl 预算恢复完成：待决预留 {len(pending)} 笔。")
            self.store = store
            self.key_pool = KeyPool(
                keys,
                cooldown_seconds=_int_option(self.config, "key_cooldown_seconds", 600),
                logger=logger,
            )
            client = FirecrawlClient(
                self.key_pool,
                base_url=self.config.get("base_url", _DEFAULT_BASE_URL),
                timeout_seconds=_int_option(self.config, "timeout_seconds", 60),
                logger=logger,
            )
            self.client = client
            self.executor = BudgetedExecutor(client, store)
            concurrency = _int_option(self.config, "max_concurrent_requests", 4)
            if concurrency < 1:
                raise ValueError("max_concurrent_requests 必须至少为 1。")
            self._semaphore = asyncio.Semaphore(concurrency)
            self.context.add_llm_tools(
                FirecrawlSearchTool(plugin=self),
                FirecrawlScrapeTool(plugin=self),
                FirecrawlSiteTool(plugin=self),
            )
        except BaseException:
            if client is not None:
                await client.aclose()
            self._release_runtime()
            logger.error("Firecrawl 插件初始化失败：已回滚运行时资源。")
            raise

    async def terminate(self) -> None:
        """关闭运行时：拒绝新请求、等待在途请求排空后关闭客户端；可重复调用。"""
        self._closing = True
        if self._terminated:
            return
        self._terminated = True
        await self._drained.wait()
        client = self.client
        if client is not None:
            await client.aclose()
        self._release_runtime()

    @asynccontextmanager
    async def request_scope(self) -> AsyncIterator[None]:
        """限制并发的请求作用域：未初始化或正在关闭时固定拒绝。"""
        semaphore = self._semaphore
        if semaphore is None or self._closing:
            raise RuntimeError(_RUNTIME_UNAVAILABLE)
        await semaphore.acquire()
        token = object()
        try:
            if self._closing:
                raise RuntimeError(_RUNTIME_UNAVAILABLE)
            self._active_scopes.add(token)
            self._drained.clear()
            yield
        finally:
            self._active_scopes.discard(token)
            if not self._active_scopes:
                self._drained.set()

            semaphore.release()

    def _release_runtime(self) -> None:
        """清空全部运行时引用，已关闭资源不再可达。"""
        self.key_pool = None
        self.client = None
        self.store = None
        self.executor = None
        self._semaphore = None
        self._active_scopes = set()
        self._drained = asyncio.Event()
        self._drained.set()

    @filter.command_group("fc")
    def fc(self):
        """Firecrawl 管理指令组。"""
        pass

    @fc.command("help")
    async def fc_help(self, event: AstrMessageEvent):
        """显示 Firecrawl 管理指令帮助。"""
        return event.plain_result(_HELP_TEXT)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @fc.command("credit")
    async def fc_credit(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        """查询各 API Key 所属团队的额度。"""
        if extra is not GreedyStr:
            return event.plain_result(_CREDIT_USAGE_TEXT)
        return event.plain_result(_truncate(await self._credit_text()))

    @fc.group("pending")
    @filter.permission_type(filter.PermissionType.ADMIN)
    def fc_pending(self):
        """管理待决预算预留（管理员）。"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @fc_pending.command("list")
    async def fc_pending_list(
        self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr
    ):
        """查看待决预算预留（管理员）。"""
        store = self.store
        if store is None or self._closing:
            return event.plain_result(_RUNTIME_UNAVAILABLE)
        if extra is not GreedyStr:
            return event.plain_result(_PENDING_USAGE_TEXT)
        return event.plain_result(_truncate(await self._pending_list_text(store)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @fc_pending.command("resolve")
    async def fc_pending_resolve(
        self,
        event: AstrMessageEvent,
        reservation_id: str = "",
        actual_credits: str = "",
        extra: GreedyStr = GreedyStr,
    ):
        """按实际 credits 结算待决预留，0 表示确认未受理（管理员）。"""
        store = self.store
        if store is None or self._closing:
            return event.plain_result(_RUNTIME_UNAVAILABLE)
        if not reservation_id or not actual_credits:
            return event.plain_result(_RESOLVE_USAGE_TEXT)
        args = [reservation_id, actual_credits]
        if extra is not GreedyStr:
            args.extend(extra.split())
        return event.plain_result(_truncate(await self._resolve_text(store, args)))

    async def _credit_text(self) -> str:
        """逐个 Key 查询团队额度；该查询不写本地账目。"""
        client = self.client
        pool = self.key_pool
        if client is None or pool is None or self._closing:
            return _RUNTIME_UNAVAILABLE
        refs = pool.key_refs
        if not refs:
            return _NO_KEY_TEXT
        lines = []
        for index, ref in enumerate(refs, start=1):
            lines.append(await self._credit_line(client, pool, ref, index))
        return "\n".join(lines)

    async def _credit_line(
        self, client: FirecrawlClient, pool: KeyPool, ref: str, index: int
    ) -> str:
        """查询单个 Key 的团队额度，并显示最新冷却状态。"""
        try:
            async with self.request_scope():
                response = await client.request("GET", _CREDIT_ENDPOINT, key_ref=ref)
            fields = _credit_fields(response.json())
        except FirecrawlError as err:
            return f"{_credit_label(pool, ref, index)}：{_safe_error_text(err)}"
        except RuntimeError:
            return _RUNTIME_UNAVAILABLE
        except ValueError:
            return f"{_credit_label(pool, ref, index)}：查询失败：响应格式无效。"
        except Exception as exc:
            logger.warning("fc credit 查询异常：%s", type(exc).__name__)
            return f"{_credit_label(pool, ref, index)}：查询失败，请稍后重试。"
        if fields is None:
            return f"{_credit_label(pool, ref, index)}：响应缺少额度字段。"
        remaining, plan, period = fields
        return (
            f"{_credit_label(pool, ref, index)}：剩余 "
            f"{_fmt_credits(remaining)} / {_fmt_credits(plan)}"
            f" credits（账期 {period}）。"
        )

    async def _pending_list_text(self, store: SQLiteStore) -> str:
        """输出本地待决预留短摘要与 UTC 用量，绝不输出指纹与完整 Key。"""
        try:
            pending = await store.list_pending()
            snapshot = await store.usage()
        except Exception as exc:
            logger.warning("fc pending list 读取失败：%s", type(exc).__name__)
            return _PENDING_READ_FAILED_TEXT
        header = (
            f"UTC {snapshot.utc_day}：已用 {_fmt_credits(snapshot.used)}，"
            f"活动预留 {_fmt_credits(snapshot.active_reserved)} credits。"
        )
        if not pending:
            return f"{header}\n当前没有待决预留。"
        lines = [header, f"待决预留 {len(pending)} 笔："]
        for record in pending[:_PENDING_LIST_LIMIT]:
            lines.append(
                f"- {record.reservation_id} | {record.kind} | {record.state.value}"
                f" | 预估 {record.estimate}"
                f" | 已入账 {_fmt_credits(record.accounted_credits)}"
                f" | key {_short_ref(record.key_ref)} | job {_short_job(record.job_id)}"
            )
        hidden = len(pending) - _PENDING_LIST_LIMIT
        if hidden > 0:
            lines.append(f"… 其余 {hidden} 笔已省略。")
        return "\n".join(lines)

    async def _resolve_text(self, store: SQLiteStore, args: list[str]) -> str:
        """结算（>0）或确认未受理释放（0）；非法参数零写入。"""
        if len(args) != 2:
            return _RESOLVE_USAGE_TEXT
        reservation_id, amount_text = args
        value = _parse_credits(amount_text)
        if not reservation_id or len(reservation_id) > 64 or value is None:
            return _RESOLVE_INVALID_TEXT
        try:
            if value == 0:
                await store.release(reservation_id, confirmed_not_accepted=True)
                return f"{reservation_id}：已确认未受理，预留已释放，不计费。"
            await store.settle(reservation_id, value)
            return f"{reservation_id}：已按实际费用结算 {_fmt_credits(value)} credits。"
        except (KeyError, FirecrawlError):
            return _RESOLVE_NOT_ALLOWED_TEXT
        except Exception as exc:
            logger.warning("fc pending resolve 处理异常：%s", type(exc).__name__)
            return _RESOLVE_FAILED_TEXT
