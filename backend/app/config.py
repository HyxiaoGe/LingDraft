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

ARK_IMAGE_MODEL = os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-5-0-flash-260915")
# 背景图生成开关（M2 起默认开）；关闭时背景为模板渐变
BACKGROUND_ENABLED = os.environ.get("LINGDRAFT_BACKGROUND", "1") not in ("0", "false", "")
USE_FAKE_PLANNER = os.environ.get("LINGDRAFT_FAKE", "0") in ("1", "true")

MAX_CHECK_RETRIES = 2
