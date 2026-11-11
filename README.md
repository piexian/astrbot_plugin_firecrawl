# Firecrawl网络工具 - AstrBot 插件

通过 [Firecrawl](https://www.firecrawl.dev/) API 为 AstrBot 提供网页搜索、单页抓取、站点爬取和批量抓取能力，模型可根据对话内容自动调用。

## 功能

- 网页搜索：搜索网页、新闻和图片，支持域名筛选，可同时抓取结果正文。
- 单页抓取：返回 Markdown、摘要、链接、HTML、页面问答或 JSON 抽取结果。
- 站点任务：获取站点链接、爬取站点、批量抓取，并查询或取消异步任务。
- 多 API Key 轮询：鉴权、额度或限流失败的 Key 会暂停使用。
- 费用控制：支持单次预估上限、UTC 每日额度、并发限制和待决记录管理。

## 安装

需要 AstrBot 4.29 或更高版本。在 AstrBot 控制台的插件管理中通过仓库地址安装：

```
https://github.com/piexian/astrbot_plugin_firecrawl
```

## 配置

### 获取 API Key

1. 前往 [Firecrawl](https://www.firecrawl.dev/) 注册并创建 API Key。
2. 在 AstrBot 控制台 → 插件设置 → Firecrawl网络工具 → 填写 API Key 列表。

### 配置项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_keys` | Firecrawl API Key 列表，多个 Key 自动轮询 | `[]` |
| `base_url` | 自托管或代理地址，不带 `/v2` | `https://api.firecrawl.dev` |
| `timeout_seconds` | 单次请求超时时间（秒） | `60` |
| `key_cooldown_seconds` | 失败 Key 的暂停使用时间（秒） | `600` |
| `search_max_results` | 搜索条数上限（1-100） | `10` |
| `max_content_chars` | 单页返回给模型的正文最大字符数 | `8000` |
| `site_max_pages` | map 链接数、crawl 页数和 batch URL 数上限 | `50` |
| `pdf_max_pages` | PDF 解析页数上限（1-10000），同时用于费用预估 | `20` |
| `max_estimated_credits_per_call` | 单次调用预估 credits 上限，`0` 表示不限制 | `0` |
| `daily_credit_limit` | 每日 credits 上限，按 UTC 自然日计算，`0` 表示不限制 | `0` |
| `max_concurrent_requests` | 插件同时发起的 Firecrawl 请求数上限 | `4` |
| `check_prompt_injection` | 默认是否检测提示注入，完成检测时每页额外 4 credits | `false` |
| `site_admin_only` | crawl 和 batch 是否仅管理员可用 | `true` |
| `blocked_domains` | 带正文搜索、单页抓取和站点抓取的高价域名拦截列表 | `x.com`、`twitter.com`、`mobile.twitter.com`、`linkedin.com` |

同一团队的多个 Key 共享 Firecrawl 团队额度和限流，插件不会自动识别它们是否属于同一团队。

## 使用方法

### LLM Tool 自动调用

插件注册了 3 个 LLM Tool，模型会根据对话内容选择调用：

| 工具 | 用途 |
|------|------|
| `firecrawl_search` | 搜索网页、新闻或图片，支持域名筛选和结果正文抓取 |
| `firecrawl_scrape` | 抓取单个网页，支持 Markdown、摘要、链接、HTML、问答和 JSON |
| `firecrawl_site` | 执行 map、crawl、batch，或用任务句柄执行 status、cancel |

例如，可以直接对机器人说：

- “搜索最近的 AstrBot 新闻，返回标题和链接。”
- “抓取 https://example.com 的正文。”
- “列出 https://example.com 的页面链接。”

### 管理指令

```
/fc help                                      # 显示帮助
/fc credit                                    # 查询团队额度与 Key 冷却状态
/fc pending list                              # 查看待决预算记录
/fc pending resolve <预留ID> <实际credits>      # 按人工核对的实际费用结算
```

除 `/fc help` 外，上述指令仅管理员可用。

这些指令使用 AstrBot 指令组注册，可在“插件 → 管理行为 → 指令”中查看和管理。裸 `/fc` 或 `/fc pending` 会显示对应指令树。

请求结果不确定时，插件会保留待决记录。先在 Firecrawl 账单中核对，再使用 `pending resolve` 结算；只有确认请求未被受理时，才将实际 credits 填为 `0` 释放预留。

LLM Tool 的远端错误会在 AstrBot 日志中记录状态码、错误码、request_id 和脱敏后的错误原文；每条日志最多 1000 字符，超出部分截断。

## 费用与任务说明

- 搜索时开启正文抓取会按每条结果额外产生单页抓取费用。
- 页面问答、JSON 抽取和提示注入检测会产生额外费用，PDF 页数上限也会影响费用预估。
- 提示注入检测通过时不返回独立结论；命中注入时请求失败，扫描不完整时返回 `warning`。
- crawl 和 batch 默认仅管理员可提交，返回任务句柄后可查询进度或取消任务。
- batch 中任一 URL 校验失败或命中 `blocked_domains`，整批请求都会被拒绝，不会自动剔除或发送其余 URL。
- 站点任务的 `next`、`hasNext` 只表示分页信息；任务完成不代表所有发现的页面都抓取成功。
- Firecrawl 计划于 2026-11-16 调整 `research` 分类的论文返回格式；插件遇到新版论文数据时会明确提示接口变化。

## 相关链接

- [Firecrawl 官方文档](https://docs.firecrawl.dev/)
- [AstrBot 官方文档](https://docs.astrbot.app/)

## 许可证

[AGPL-3.0](./LICENSE)
