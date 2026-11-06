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

# 2026-11-05 新接的几家，都走各家的 OpenAI 兼容接口直连（Sean 定不经 LiteLLM，见 research_llm.COMPAT）。
# 地址是国内站的默认值，.env 里只填 key；key 是国际站或专属地址的，加一行同名的 *_BASE_URL 覆盖
MOONSHOT_BASE_URL = os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")  # Kimi
MOONSHOT_API_KEY = os.environ.get("MOONSHOT_API_KEY")
XAI_BASE_URL = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")  # Grok
XAI_API_KEY = os.environ.get("XAI_API_KEY")
DASHSCOPE_BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")  # 千问（阿里云百炼），要 compatible-mode 的地址
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")  # 小米 MiMo
MIMO_API_KEY = os.environ.get("MIMO_API_KEY")
ZHIPUAI_BASE_URL = os.environ.get("ZHIPUAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")  # 智谱 GLM
ZHIPUAI_API_KEY = os.environ.get("ZHIPUAI_API_KEY")
HUNYUAN_BASE_URL = os.environ.get("HUNYUAN_BASE_URL", "https://tokenhub.tencentmaas.com/v1")  # 腾讯混元，走腾讯云 TokenHub（key 也是 TokenHub 的）
HUNYUAN_API_KEY = os.environ.get("HUNYUAN_API_KEY")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1")  # MiniMax
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY")

# 对话用哪些模型（用户能选的选项、子 agent 用哪个、起标题这些内部用途用哪个）不在这里，见 app/models.toml；这里只有各家的 key 和地址。
# 一个子 agent 最多搜几次、读几页：超了的不执行，每步提醒还剩几次（2026-11-01 Sean 定按工具设上限，11-02 定 8/8）
SUBAGENT_SEARCH_BUDGET = int(os.environ.get("SUBAGENT_SEARCH_BUDGET", "8"))
SUBAGENT_READ_BUDGET = int(os.environ.get("SUBAGENT_READ_BUDGET", "8"))
# 上下文压缩（见 app/graph/research/compaction.py）：发给主 agent 的对话估算超过 TRIGGER 个 token 时，把之前几轮压到 TARGET 以下。
# 窗口 1M，Sean 记得压到 20%～30% 效果好（2026-11-03 定 20 万触发、压到 10 万左右）
COMPACT_TRIGGER_TOKENS = int(os.environ.get("COMPACT_TRIGGER_TOKENS", "200000"))
COMPACT_TARGET_TOKENS = int(os.environ.get("COMPACT_TARGET_TOKENS", "100000"))

# 长期记忆（见 app/graph/research/memory.py）：每天北京时间 DREAM_HOUR 点在后台整理一次（做梦），把有新的几轮的对话整理成关于用户的记忆。
# 2026-11-03 Sean 定后台整理、每天一次；3 点是 DeepSeek 的闲时价。用哪个模型见 app/models.toml 的 roles.dream
DREAM_ENABLED = _flag("LINGDRAFT_DREAM", True)  # 关掉就不按时整理（手动的命令照样能用）
DREAM_HOUR = int(os.environ.get("LINGDRAFT_DREAM_HOUR", "3"))

ARK_IMAGE_MODEL = os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-5-0-flash-260915")
# 背景图生成开关（M2 起默认开）；关闭时背景为模板渐变
BACKGROUND_ENABLED = _flag("LINGDRAFT_BACKGROUND", True)
USE_FAKE_PLANNER = _flag("LINGDRAFT_FAKE", False)
RESEARCH_WORKERS = int(os.environ.get("LINGDRAFT_RESEARCH_WORKERS", "4"))  # 同时跑几个调研线程，多出来的排队
# 对话（检查点、长期记忆、运行记录）存 Postgres（postgresql://用户:密码@主机:端口/库）；没配服务起不来
DATABASE_URL = os.environ.get("LINGDRAFT_DATABASE_URL", "")
# 前端构建出来的页面（frontend/dist）：部署时由后端一起出页面（镜像里配好了）；本机开发不配，页面由 Vite 出
STATIC_DIR = os.environ.get("LINGDRAFT_STATIC_DIR", "")
# 登录（见 app/identity.py）：Sean 自己的 auth 服务（auth-service，Authorization Code + PKCE，RS256 JWT）。
# AUTH_URL 是它的公开地址，也是 JWT 的签发者（issuer），页面跳过去登录；AUTH_CLIENT_ID 是灵稿在它那里登记的应用；
# 没配 AUTH_CLIENT_ID 就没有登录（页面不显示登录入口，所有人都是游客）。
# AUTH_INTERNAL_URL：后端取验签公钥（JWKS）走的地址，容器内直连 http://auth-service:8100，不配就用公开地址。
# AUTH_REDIS_URL：auth 服务登出时在它的 Redis 里记吊销标记，这里查一下，别处退出了这里马上拒绝；不配就等 token 自己过期（24 小时）。
AUTH_URL = os.environ.get("AUTH_URL", "https://auth.seanfield.org").rstrip("/")
AUTH_CLIENT_ID = os.environ.get("AUTH_CLIENT_ID", "")
AUTH_INTERNAL_URL = os.environ.get("AUTH_INTERNAL_URL", "").rstrip("/")
AUTH_REDIS_URL = os.environ.get("AUTH_REDIS_URL", "")
# 游客：没登录也能聊，身份是灵稿自己签的令牌（HS256），对话归这个游客，登录后并进账号。密钥不配就每次启动随机一个（重启后游客要重新领身份，对话就找不回了），
# 部署一定要配。每个来源地址一天最多领这么多个新游客身份（防爬虫、脚本刷）
GUEST_SECRET = os.environ.get("LINGDRAFT_GUEST_SECRET", "")
GUEST_PER_IP_DAILY = int(os.environ.get("LINGDRAFT_GUEST_PER_IP_DAILY", "30"))
VERSION = os.environ.get("LINGDRAFT_VERSION", "")  # 部署时的提交号（镜像构建时带进来），记进运行记录；本机为空

MAX_CHECK_RETRIES = 2

# dev 上的 agent-tools 网关：搜索、读网页、天气、生图（MCP），取图、删图，Bearer 产品密钥
AGENT_TOOLS_URL = os.environ.get("AGENT_TOOLS_URL", "http://agent-tools-gateway:8100")
AGENT_TOOLS_API_KEY = os.environ.get("AGENT_TOOLS_API_KEY", "")

# 新对话页的“今日灵感”从 Fusion 的公开接口取（不用登录），见 app/api/inspiration.py
FUSION_URL = os.environ.get("FUSION_URL", "https://fusion.seanfield.org")
