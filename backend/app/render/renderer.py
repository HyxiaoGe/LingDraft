"""渲染层：元素 + 事实 → Jinja2 模板 → HTML → Playwright 截图 PNG。

模型不参与这一层。局部修改只改元素 JSON，再整体重渲染。
"""
from __future__ import annotations

import atexit
import threading
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.graph.poster.state import Element, Fact, StyleOption

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATES = ["bold_sale"]
WIDTH, HEIGHT = 1080, 1440

_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]))
# Playwright 同步 API 绑定创建它的线程，所以每个线程各自持有一个浏览器
_local = threading.local()


def resolve_text(e: Element, facts: dict[str, Fact]) -> str:
    """事实绑定元素的文本永远来自事实，不来自 element.content。"""
    if e.bound_fact:
        f = facts.get(e.bound_fact)
        return (f.value or "") if f else ""
    return e.content or ""


def render_html(
    elements: list[Element],
    facts: dict[str, Fact],
    style: StyleOption,
    template_id: str,
    title: str = "",
    background_image: str | None = None,
    product_image: str | None = None,
) -> str:
    """background_image / product_image 为本地文件路径，写进 HTML 时转成相对 html 所在目录的文件名或 file URI。"""
    tpl = _env.get_template(f"{template_id}.html")
    texts = {e.id: resolve_text(e, facts) for e in elements}
    return tpl.render(
        elements=elements,
        texts=texts,
        palette=style.palette,
        title=title,
        background_image=background_image,
    )


def _get_browser():
    if getattr(_local, "browser", None) is None:
        from playwright.sync_api import sync_playwright

        _local.playwright = sync_playwright().start()
        _local.browser = _local.playwright.chromium.launch()
        atexit.register(_shutdown, _local.playwright, _local.browser)
    return _local.browser


def _shutdown(pw, browser):
    import contextlib

    with contextlib.suppress(Exception):  # 进程退出时的清理，失败无所谓
        browser.close()
        pw.stop()


def _relative_to(path: str | None, out_dir: Path) -> str | None:
    if not path:
        return None
    p = Path(path)
    try:
        return str(p.resolve().relative_to(out_dir.resolve()))
    except ValueError:
        return p.resolve().as_uri()


@dataclass
class RenderResult:
    html_path: str
    png_path: str
    layout: dict = field(default_factory=dict)  # slot -> {id, kind, bbox, overflow, text}


def render(
    elements: list[Element],
    facts: dict[str, Fact],
    style: StyleOption,
    template_id: str,
    out_dir: Path,
    version: int,
    background_image: str | None = None,
) -> RenderResult:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"v{version}.html"
    png_path = out_dir / f"v{version}.png"
    # 图片素材都放在 out_dir 下，HTML 里用相对路径引用，导出后整个目录可以搬走
    bg_rel = _relative_to(background_image, out_dir)
    elements = [e.model_copy(update={"content": _relative_to(e.content, out_dir)}) if e.kind == "product_image" else e for e in elements]
    html_path.write_text(
        render_html(elements, facts, style, template_id, title=f"v{version}", background_image=bg_rel), encoding="utf-8"
    )

    page = _get_browser().new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
    try:
        page.goto(html_path.resolve().as_uri())
        page.wait_for_load_state("load")
        page.evaluate("window.__fitAll()")
        layout = page.evaluate("window.__layout()")
        page.screenshot(path=str(png_path), full_page=False)
    finally:
        page.close()
    return RenderResult(html_path=str(html_path), png_path=str(png_path), layout=layout)
