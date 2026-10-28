"""运行配置：全部来自环境变量，PoC 不引入配置文件。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

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
RESEARCH_THINKING = os.environ.get("RESEARCH_THINKING", "1") not in ("0", "false", "")
RESEARCH_EFFORT = os.environ.get("RESEARCH_EFFORT", "high")

ARK_IMAGE_MODEL = os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-5-0-flash-260915")
# 背景图生成开关（M2 起默认开）；关闭时背景为模板渐变
BACKGROUND_ENABLED = os.environ.get("LINGDRAFT_BACKGROUND", "1") not in ("0", "false", "")
USE_FAKE_PLANNER = os.environ.get("LINGDRAFT_FAKE", "0") in ("1", "true")
RESEARCH_WORKERS = int(os.environ.get("LINGDRAFT_RESEARCH_WORKERS", "4"))  # 同时跑几个调研线程，多出来的排队

MAX_CHECK_RETRIES = 2

# dev 上的 agent-tools 网关：搜索 / 网页读取，Bearer 产品密钥
AGENT_TOOLS_URL = os.environ.get("AGENT_TOOLS_URL", "http://agent-tools-gateway:8100")
AGENT_TOOLS_API_KEY = os.environ.get("AGENT_TOOLS_API_KEY", "")
