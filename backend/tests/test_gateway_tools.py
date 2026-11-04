"""网关客户端（生图在 test_images）和灵稿自己的工具；搜索、读页、天气是 MCP 工具，见 test_web_tools、test_weather。"""

from __future__ import annotations

import httpx
import pytest

from app.graph.research.state import Source, tidy_title
from app.tools.gateway import GatewayClient, GatewayError
from app.tools.research_tools import make_research_tools


def test_own_tools():
    client = GatewayClient("http://gw", "k1", scene="research", transport=httpx.MockTransport(lambda req: httpx.Response(404)))
    assert [t.name for t in make_research_tools(client)] == ["generate_image", "get_current_time"]


def test_missing_key():
    with pytest.raises(GatewayError):
        GatewayClient("http://gw", "")


@pytest.mark.parametrize(("raw", "want"), [
    ("DeepSeek 隐私政策 - DeepSeek 隐私政策", "DeepSeek 隐私政策"),
    ("Wi-Fi 7 有多快 | Wi-Fi 7 有多快 - 某站", "Wi-Fi 7 有多快 - 某站"),  # 站名留着
    ("  大模型“独角兽”亟须差异化应对价格冲击 - 大模型“独角兽”亟须差异化应对价格冲击 ", "大模型“独角兽”亟须差异化应对价格冲击"),
    ("Wi-Fi 7 - 评测", "Wi-Fi 7 - 评测"),  # 不是重复不动
    ("FAQ - FAQ", "FAQ"),
    ("首页_首页", "首页"),
    ("1 - 1", "1 - 1"),  # 一个字的不算
    ("10-10-2024 发布会", "10-10-2024 发布会"),  # 横线两边没空格的不是分隔符
    ("A 与 B - A 与 BC", "A 与 B - A 与 BC"),  # 只认一字不差的重复
])
def test_tidy_title(raw, want):
    assert tidy_title(raw) == want


def test_old_source_titles_are_tidied():
    """以前存下的来源打开时（从检查点还原会重新校验）标题理掉重复；新的搜索结果搜索服务已经理过了。"""
    assert Source(id="s1", url="https://a", title="A 文章 - A 文章").title == "A 文章"
