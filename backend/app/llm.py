"""模型接入层。

- Planner 协议：图节点只依赖这个接口，测试用 FakePlanner，生产用 ArkPlanner（豆包 / 火山方舟）。
- 所有模型调用都是结构化输出；事实字段永远不交给模型生成。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.graph.poster import prompts
from app.graph.poster.state import KIND_LABELS, Element, Fact, StyleOption

# ---- 结构化输出 schema（扁平、全部 required，符合方舟 strict 模式建议） ----


class IntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_name: str | None = Field(description="商品名称，用户未提及则为 null")
    offer: str | None = Field(description="优惠信息，如“5 折”“满 300 减 50”，用户未提及则为 null")
    date_range: str | None = Field(description="活动时间，保留用户原话，用户未提及则为 null")
    cta: str | None = Field(description="行动号召，如“立即抢购”，用户未提及则为 null")
    rules: str | None = Field(description="活动规则或限制条件，用户未提及则为 null")
    style_hint: str | None = Field(description="用户明确描述的视觉风格，未描述则为 null")


class StyleOptionsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: list[StyleOption] = Field(description="视觉方向列表")


class ElementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="槽位 id，必须来自“需要撰写的槽位”")
    content: str = Field(description="该槽位的文案")


class ComposeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updates: list[ElementUpdate]


class PosterOp(BaseModel):
    """终审时用户的话对应的一个操作，字段和按钮回传的一样，由同一套校验检查。"""

    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "edit", "manual_edit", "change_fact", "change_style"]
    target: str | None = Field(description="edit / manual_edit 填元素 id；change_fact 填事实 key；其余为 null")
    instruction: str | None = Field(description="edit 时怎么改；change_style 时用户描述的新风格；其余为 null")
    content: str | None = Field(description="manual_edit 时的新文字；change_fact 时用户说的新值（没说为 null）；其余为 null")


class PosterPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ops: list[PosterOp] = Field(description="用户这句话要做的操作，按用户说的顺序；对不上任何操作时为空")
    reply: str | None = Field(description="ops 为空、或用户的话有对不上的部分时，一两句话告诉用户；操作已覆盖用户的话时为 null")


class CopyIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str = Field(description="有问题的文案的槽位 id")
    problem: str = Field(description="一句话：哪里不合规")


class CopyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issues: list[CopyIssue] = Field(description="有问题的文案；都合规时为空")


# ---- 协议 ------------------------------------------------------------------


class Planner(Protocol):
    def intake(self, request: str) -> IntakeResult: ...

    def style_options(self, request: str, facts: dict[str, Fact], hint: str | None) -> list[StyleOption]: ...

    def compose(
        self,
        facts: dict[str, Fact],
        style: StyleOption,
        elements: list[Element],
        targets: list[str],
        instruction: str | None,
        constraints: list[str],
    ) -> list[ElementUpdate]: ...

    def background(self, style: StyleOption, facts: dict[str, Fact], out_path: Path) -> Path:
        """生成一张无文字的背景图写到 out_path。只画氛围，不画任何文字和数字。"""
        ...

    def interpret_review(self, facts: dict[str, Fact], style: StyleOption, elements: list[dict], text: str) -> PosterPlan:
        """终审时用户直接说的话 → 操作。elements 是终审卡上的元素视图（id、类型、文字、能不能让模型改）。"""
        ...

    def check_copy(self, facts: dict[str, Fact], copy: dict[str, str]) -> list[CopyIssue]:
        """模型写的文案（槽位 id → 文字）是否合规：绝对化用语、数字承诺、和事实冲突。"""
        ...


def _facts_text(facts: dict[str, Fact]) -> str:
    lines = []
    for f in facts.values():
        if f.value:
            lines.append(f"- {f.label}（{f.key}）：{f.value}")
    return "\n".join(lines) or "（无）"


def _review_elements_text(elements: list[dict]) -> str:
    lines = []
    for e in elements:
        if e["kind"] in ("product_image", "background"):
            continue
        how = "事实" if e.get("bound_fact") else ("模型可改" if e.get("editable_by_model") else "用户改过")
        lines.append(f"- {e['id']} | {KIND_LABELS.get(e['kind'], e['kind'])} | {e.get('text') or '（空）'} | {how}")
    return "\n".join(lines) or "（无）"


def _elements_text(elements: list[Element], targets: list[str]) -> tuple[str, str]:
    need, locked = [], []
    for e in elements:
        if e.id in targets:
            need.append(f"- {e.id}（{e.kind}）")
        elif e.content:
            locked.append(f"- {e.id}（{e.kind}）：{e.content}")
    return "\n".join(need) or "（无）", "\n".join(locked) or "（无）"


# ---- 生产实现：豆包 / 火山方舟 ----------------------------------------------


class ArkPlanner:
    """通过方舟 OpenAI 兼容接口调用豆包。结构化调用关闭深度思考。"""

    def __init__(self, model: str | None = None, lite_model: str | None = None):
        from langchain_openai import ChatOpenAI

        if not config.ARK_API_KEY:
            raise RuntimeError("未设置 ARK_API_KEY")
        common = {
            "base_url": config.ARK_BASE_URL,
            "api_key": config.ARK_API_KEY,
            "extra_body": {"thinking": {"type": "disabled"}},
            "timeout": 60,
            "max_retries": 2,
        }
        self._pro = ChatOpenAI(model=model or config.ARK_MODEL, **common)
        self._lite = ChatOpenAI(model=lite_model or config.ARK_MODEL_LITE, **common)

    def _structured(self, llm, schema):
        return llm.with_structured_output(schema, method="json_schema", strict=True)

    def intake(self, request: str) -> IntakeResult:
        chain = self._structured(self._lite, IntakeResult)
        return chain.invoke(
            [("system", prompts.INTAKE_SYSTEM), ("user", f"用户需求：{request}")]
        )

    def style_options(self, request: str, facts: dict[str, Fact], hint: str | None) -> list[StyleOption]:
        chain = self._structured(self._pro, StyleOptionsResult)
        if hint:
            ask = f"用户已指定风格：“{hint}”。请只给出 1 个贴合该描述的视觉方向。"
        else:
            ask = "请给出 3 个差异明显的视觉方向供用户选择。"
        res = chain.invoke(
            [
                ("system", prompts.STYLE_SYSTEM),
                ("user", f"活动需求：{request}\n已确认事实：\n{_facts_text(facts)}\n\n{ask}"),
            ]
        )
        return res.options

    def compose(self, facts, style, elements, targets, instruction, constraints) -> list[ElementUpdate]:
        chain = self._structured(self._pro, ComposeResult)
        need, locked = _elements_text(elements, targets)
        parts = [
            f"已确认事实：\n{_facts_text(facts)}",
            f"视觉方向：{style.name}，氛围：{style.mood}。{style.description}",
            f"需要撰写的槽位：\n{need}",
            f"锁定的元素（不要改动，只作参考）：\n{locked}",
        ]
        if instruction:
            parts.append(f"用户修改意见：{instruction}")
        if constraints:
            parts.append("上一轮校验未通过，必须满足：\n" + "\n".join(f"- {c}" for c in constraints))
        res = chain.invoke([("system", prompts.COMPOSE_SYSTEM), ("user", "\n\n".join(parts))])
        return [u for u in res.updates if u.id in targets]

    def interpret_review(self, facts, style, elements, text) -> PosterPlan:
        chain = self._structured(self._pro, PosterPlan)
        user = (f"已确认事实：\n{_facts_text(facts)}\n\n当前风格：{style.name}（{style.description}）\n\n"
                f"海报上的元素（id | 类型 | 当前文字 | 能怎么改）：\n{_review_elements_text(elements)}\n\n用户说：{text}")
        return chain.invoke([("system", prompts.INTERPRET_SYSTEM), ("user", user)])

    def check_copy(self, facts, copy) -> list[CopyIssue]:
        if not copy:
            return []
        chain = self._structured(self._pro, CopyCheck)
        lines = "\n".join(f"- {slot}：{text}" for slot, text in copy.items())
        res = chain.invoke([("system", prompts.COMPLIANCE_SYSTEM),
                            ("user", f"已确认事实：\n{_facts_text(facts)}\n\n要检查的文案（槽位 id：文字）：\n{lines}")])
        return [i for i in res.issues if i.slot in copy]

    def background(self, style: StyleOption, facts: dict[str, Fact], out_path: Path) -> Path:
        import httpx
        from openai import OpenAI

        client = OpenAI(base_url=config.ARK_BASE_URL, api_key=config.ARK_API_KEY, timeout=120)
        product = facts["product_name"].value or "商品"
        prompt = prompts.BACKGROUND_PROMPT.format(
            mood=style.mood, description=style.description, palette="、".join(style.palette), product=product
        )
        res = client.images.generate(
            model=config.ARK_IMAGE_MODEL,
            prompt=prompt,
            size="1080x1440",
            response_format="url",
            extra_body={"watermark": False},
        )
        url = res.data[0].url
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(httpx.get(url, timeout=120).content)
        return out_path


# ---- 测试 / 演示实现：确定性输出，不联网 ------------------------------------


class FakePlanner:
    """按关键词给出固定结果，用于 pytest 与 --fake 演示。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.review_plans: list[PosterPlan] = []  # 终审时用户的话依次被理解成什么（测试按需设）；没有预设就是没对上
        self.copy_issues: list[list[CopyIssue]] = []  # 每次合规检查报告的问题（测试按需设）；没有预设就是都合规

    def intake(self, request: str) -> IntakeResult:
        self.calls.append(("intake", request))
        return IntakeResult(
            product_name="蓝牙耳机" if "耳机" in request else None,
            offer="5 折" if "5 折" in request or "5折" in request else None,
            date_range="11.20 至 11.27" if "11.20" in request else None,
            cta=None,
            rules=None,
            style_hint="极简白底" if "极简白底" in request else None,
        )

    def style_options(self, request, facts, hint) -> list[StyleOption]:
        self.calls.append(("style_options", hint))
        if hint:
            return [StyleOption(id="s_hint", name=hint, palette=["#ffffff", "#f2f2f2", "#111111", "#111111"], mood="极简", description="白底黑字")]
        return [
            StyleOption(id="s1", name="黑金炸裂", palette=["#0b0b0b", "#1f1a0e", "#f5c542", "#ffffff"], mood="热烈", description="黑底金字，强促销感"),
            StyleOption(id="s2", name="深蓝科技", palette=["#0a1a33", "#102a52", "#4fd1ff", "#ffffff"], mood="科技", description="深蓝渐变，电子产品常用"),
            StyleOption(id="s3", name="活力橙", palette=["#fff4e6", "#ffd7a8", "#ff6a00", "#1a1a1a"], mood="活泼", description="暖色明亮，年轻人群"),
        ]

    def compose(self, facts, style, elements, targets, instruction, constraints) -> list[ElementUpdate]:
        self.calls.append(("compose", tuple(targets), instruction, tuple(constraints)))
        product = facts["product_name"].value or "好物"
        defaults = {
            "headline": f"黑五预热 {product}狂欢",
            "subhead": "预热开抢，好礼限时放送",
            "cta": "立即抢购",
        }
        if instruction and "紧迫" in instruction:
            defaults["headline"] = "黑五开抢 不容错过"
        if instruction and "短" in instruction:
            defaults["headline"] = "黑五来了"
        if any("短" in c or "溢出" in c for c in constraints):
            defaults["headline"] = "黑五预热"
            defaults["subhead"] = "限时放送"
        return [ElementUpdate(id=t, content=defaults[t]) for t in targets if t in defaults]

    def interpret_review(self, facts, style, elements, text) -> PosterPlan:
        self.calls.append(("interpret", text))
        if self.review_plans:
            return self.review_plans.pop(0)
        return PosterPlan(ops=[], reply="（假模型）没有预设怎么理解这句话")

    def check_copy(self, facts, copy) -> list[CopyIssue]:
        self.calls.append(("check_copy", dict(copy)))
        return [i for i in (self.copy_issues.pop(0) if self.copy_issues else []) if i.slot in copy]

    def background(self, style: StyleOption, facts: dict[str, Fact], out_path: Path) -> Path:
        """用 Pillow 画一张纵向渐变 + 圆形光斑，模拟生成结果。"""
        from PIL import Image, ImageDraw

        self.calls.append(("background", style.id))

        def hex2rgb(h):
            h = h.lstrip("#")
            return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))

        top, bottom, accent = hex2rgb(style.palette[0]), hex2rgb(style.palette[1]), hex2rgb(style.palette[2])
        w, h = 1080, 1440
        img = Image.new("RGB", (w, h))
        px = img.load()
        for y in range(h):
            t = y / (h - 1)
            c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
            for x in range(w):
                px[x, y] = c
        d = ImageDraw.Draw(img, "RGBA")
        d.ellipse((560, 560, 1200, 1200), fill=accent + (40,))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)
        return out_path
