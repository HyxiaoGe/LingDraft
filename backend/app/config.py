"""运行配置：全部来自环境变量，PoC 不引入配置文件。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool) -> bool:
    """开关型环境变量：不设或留空用默认值；认不出的值直接报错，免得写错了悄悄按反的跑（比如误开付费模型）。"""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"环境变量 {name}={os.environ[name]!r} 不是开关值，请用 1/0、true/false、yes/no、on/off")


BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LINGDRAFT_DATA_DIR", BACKEND_DIR / "data"))
OUTPUT_DIR = Path(os.environ.get("LINGDRAFT_OUTPUT_DIR", BACKEND_DIR / "output"))

ARK_BASE_URL = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
ARK_API_KEY = os.environ.get("ARK_API_KEY")
ARK_MODEL = os.environ.get("ARK_MODEL", "doubao-seed-2-1-pro-260915")
ARK_MODEL_LITE = os.environ.get("ARK_MODEL_LITE", "doubao-seed-2-1-lite-260915")

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

# 调研场景的模型：探针对照后定为 DeepSeek V4 Pro 思考 high（docs/probe/autonomy）；设 RESEARCH_PROVIDER=ark 可切回豆包
RESEARCH_PROVIDER = os.environ.get("RESEARCH_PROVIDER", "deepseek")
RESEARCH_MODEL = os.environ.get("RESEARCH_MODEL", "")  # 空则按提供方默认
RESEARCH_THINKING = _flag("RESEARCH_THINKING", True)
RESEARCH_EFFORT = os.environ.get("RESEARCH_EFFORT", "high")
# 子 agent（主 agent 用 task 工具派出去的）用快一些、便宜一些的模型（2026-11-01 Sean 定）。空则按提供方默认：deepseek-flash / 豆包 lite
SUBAGENT_MODEL = os.environ.get("SUBAGENT_MODEL", "")
SUBAGENT_THINKING = _flag("SUBAGENT_THINKING", True)
SUBAGENT_EFFORT = os.environ.get("SUBAGENT_EFFORT", "high")
# 一个子 agent 最多搜几次、读几页：超了的不执行，每步提醒还剩几次（2026-11-01 Sean 定按工具设上限，11-02 定 8/8）
SUBAGENT_SEARCH_BUDGET = int(os.environ.get("SUBAGENT_SEARCH_BUDGET", "8"))
SUBAGENT_READ_BUDGET = int(os.environ.get("SUBAGENT_READ_BUDGET", "8"))
DEEPSEEK_FLASH_MODEL = os.environ.get("DEEPSEEK_FLASH_MODEL", "deepseek-flash")
# 上下文压缩（见 app/graph/research/compaction.py）：发给主 agent 的对话估算超过 TRIGGER 个 token 时，把之前几轮压到 TARGET 以下。
# 窗口 1M，Sean 记得压到 20%～30% 效果好（2026-11-03 定 20 万触发、压到 10 万左右）
COMPACT_TRIGGER_TOKENS = int(os.environ.get("COMPACT_TRIGGER_TOKENS", "200000"))
COMPACT_TARGET_TOKENS = int(os.environ.get("COMPACT_TARGET_TOKENS", "100000"))

ARK_IMAGE_MODEL = os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-5-0-flash-260915")
# 背景图生成开关（M2 起默认开）；关闭时背景为模板渐变
BACKGROUND_ENABLED = _flag("LINGDRAFT_BACKGROUND", True)
USE_FAKE_PLANNER = _flag("LINGDRAFT_FAKE", False)
RESEARCH_WORKERS = int(os.environ.get("LINGDRAFT_RESEARCH_WORKERS", "4"))  # 同时跑几个调研线程，多出来的排队
# 对话的检查点存 Postgres（postgresql://用户:密码@主机:端口/库）；不配存 DATA_DIR 下的 SQLite
DATABASE_URL = os.environ.get("LINGDRAFT_DATABASE_URL", "")
# 前端构建出来的页面（frontend/dist）：部署时由后端一起出页面（镜像里配好了）；本机开发不配，页面由 Vite 出
STATIC_DIR = os.environ.get("LINGDRAFT_STATIC_DIR", "")
VERSION = os.environ.get("LINGDRAFT_VERSION", "")  # 部署时的提交号（镜像构建时带进来），记进运行记录；本机为空

MAX_CHECK_RETRIES = 2

# dev 上的 agent-tools 网关：搜索 / 网页读取，Bearer 产品密钥
AGENT_TOOLS_URL = os.environ.get("AGENT_TOOLS_URL", "http://agent-tools-gateway:8100")
AGENT_TOOLS_API_KEY = os.environ.get("AGENT_TOOLS_API_KEY", "")
# image-service 自己的地址，要和它的 SERVICE_BASE_URL 一样：改图时把原图的地址交回去，它认出是自己生成的图才直接读本地文件
# （别的地址它当成外部图片去下载，内网地址一律拒绝）。生图本身和取图都走上面的网关
IMAGE_SERVICE_URL = os.environ.get("IMAGE_SERVICE_URL", "http://image-service.dev.seanfield.org")

# 新对话页的“今日灵感”从 Fusion 的公开接口取（不用登录），见 app/api/inspiration.py
FUSION_URL = os.environ.get("FUSION_URL", "https://fusion.seanfield.org")
