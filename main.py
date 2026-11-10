from astrbot.api.star import Context, Star


class FirecrawlPlugin(Star):
    """Firecrawl 网页抓取插件骨架：当前仅保证可加载，功能在后续阶段实现。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
