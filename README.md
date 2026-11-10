# astrbot_plugin_firecrawl

基于 [Firecrawl](https://www.firecrawl.dev/) API 的 AstrBot 插件。

当前是阶段 0 骨架：插件可被 AstrBot 加载与卸载，尚未实现任何 Firecrawl 调用能力。

## 当前能力

- 可加载的插件骨架，插件身份见 `metadata.yaml`
- 配置入口已定义（`_conf_schema.json`）：API Key 列表、base_url、超时、预算与安全护栏等

## 配置

在 AstrBot 插件配置中填写 `api_keys`（Firecrawl API Key 列表）；自托管或代理场景调整 `base_url`（不带 `/v2`）。

## 后续阶段（当前未实现）

- LLM 工具：`firecrawl_search`、`firecrawl_scrape`、`firecrawl_site`
- `/fc` 指令：help、credit、pending
- 变更监控推送与整站写入知识库

## 相关链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)

## 许可证

[AGPL-3.0](./LICENSE)
