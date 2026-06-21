"""
Agent 基类和接口定义 -- 决赛版本

选手通过继承 BaseAgent 类来实现自己的 Agent。
此文件定义了 Agent 的输入输出数据结构和基类接口。

与初赛相比的简化：
- 移除签名验证、FORBIDDEN_KWARGS 过滤、_is_production_mode() 等防篡改逻辑
- API Key 仅通过 VLM_API_KEY 环境变量读取
- 动作空间扩展为 11 种
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from PIL import Image
import io
import os
import base64
import logging

logger = logging.getLogger(__name__)


# ==========================================
#               Token 限制异常
# ==========================================

class TokenLimitExceeded(Exception):
    """Token 使用量超过限制异常"""
    def __init__(self, current_tokens: int, limit: int):
        self.current_tokens = current_tokens
        self.limit = limit
        super().__init__(
            f"Token limit exceeded: {current_tokens} > {limit}"
        )


# ==========================================
#               固定 API 配置
# ==========================================

DEFAULT_API_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL_ID = "ep-20260415212058-bx48w"


# ==========================================
#               标准动作常量
# ==========================================
# Agent 返回的动作必须是以下常量之一

# 初赛保留 (5种)
ACTION_CLICK = "CLICK"
ACTION_SCROLL = "SCROLL"
ACTION_TYPE = "TYPE"
ACTION_OPEN = "OPEN"
ACTION_COMPLETE = "COMPLETE"

# 决赛扩展 (6种)
ACTION_LONG_PRESS = "LONG_PRESS"
ACTION_DOUBLE_CLICK = "DOUBLE_CLICK"
ACTION_DRAG = "DRAG"
ACTION_BACK = "BACK"
ACTION_HOME = "HOME"
ACTION_WAIT = "WAIT"

# 所有有效动作的集合 (11种)
VALID_ACTIONS = {
    ACTION_CLICK,
    ACTION_SCROLL,
    ACTION_TYPE,
    ACTION_OPEN,
    ACTION_COMPLETE,
    ACTION_LONG_PRESS,
    ACTION_DOUBLE_CLICK,
    ACTION_DRAG,
    ACTION_BACK,
    ACTION_HOME,
    ACTION_WAIT,
}


# ==========================================
#               Token 使用信息
# ==========================================

@dataclass
class UsageInfo:
    """Token 使用信息"""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


# ==========================================
#               标准参数格式
# ==========================================
# 坐标必须使用归一化坐标：0-1000
#
# 1. CLICK:
#    {"point": [x, y]}
#
# 2. SCROLL:
#    {"start_point": [x, y], "end_point": [x, y]}
#
# 3. TYPE:
#    {"text": "内容"}
#
# 4. OPEN:
#    {"app_name": "应用名"}
#
# 5. COMPLETE:
#    {}
#
# 6. LONG_PRESS:
#    {"point": [x, y], "duration": 1000}
#
# 7. DOUBLE_CLICK:
#    {"point": [x, y]}
#
# 8. DRAG:
#    {"start_point": [x, y], "end_point": [x, y]}
#
# 9. BACK:
#    {}
#
# 10. HOME:
#    {}
#
# 11. WAIT:
#    {"seconds": 2}


@dataclass
class AgentInput:
    """Agent 输入数据结构"""
    instruction: str
    current_image: Image.Image
    step_count: int
    history_messages: List[Dict[str, Any]] = field(default_factory=list)
    history_actions: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentOutput:
    """Agent 输出数据结构"""
    action: str
    parameters: Dict[str, Any]
    raw_output: str = ""
    usage: Optional[UsageInfo] = None


class BaseAgent:
    """Agent 基类 - 选手继承此类实现自己的 Agent

    决赛简化：API URL 和 Model ID 使用固定常量，API Key 从 VLM_API_KEY 环境变量读取。
    """

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}

        self._api_url = DEFAULT_API_URL
        self._model_id = DEFAULT_MODEL_ID
        self._api_key = os.environ.get("VLM_API_KEY", "")

        self._initialize()

    @property
    def api_url(self) -> str:
        return self._api_url

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def api_key(self) -> str:
        return self._api_key

    def _initialize(self):
        """初始化方法，子类可重写"""
        pass

    def generate_messages(
        self,
        input_data: AgentInput
    ) -> List[Dict[str, Any]]:
        """生成发给大模型的 messages"""
        system_prompt = self._build_system_prompt(input_data.instruction)

        messages = [
            {"role": "user", "content": system_prompt},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": self._encode_image(input_data.current_image)}}]
            }
        ]

        return messages

    def _build_system_prompt(self, instruction: str) -> str:
        """构建系统提示词，子类可重写"""
        return f"""You are a GUI agent. You need to complete the following task.

## Task
{instruction}

## Output Format
```
Thought: ...
Action: ...
```

## Action Space
click(point='<point>x y</point>')
long_press(point='<point>x y</point>', duration='ms')
double_click(point='<point>x y</point>')
type(content='')
scroll(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
open(app_name='')
back()
home()
wait(seconds='')
complete(content='xxx')

## Note
- Use Chinese in `Thought` part.
- Write a small plan and finally summarize your next action (with its target element) in one sentence in `Thought` part.
- All coordinates are normalized to [0, 1000].
- For long_press, specify duration in milliseconds (default 1000).
- For wait, specify seconds to pause (default 2).
"""

    def _encode_image(self, image: Image.Image, image_format: str = "JPEG") -> str:
        """将图片编码为 base64 URL

        自动压缩大图（最长边不超过 1280px）并使用 JPEG 格式，
        减少 API 调用请求体大小，避免超时。
        """
        max_side = 1280
        w, h = image.size
        if max(w, h) > max_side:
            ratio = max_side / max(w, h)
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            image = image.resize((new_w, new_h), Image.LANCZOS)
            logger.debug(f"图片已缩放: {w}x{h} -> {new_w}x{new_h}")

        buffered = io.BytesIO()
        if image_format.upper() == "JPEG":
            image.save(buffered, format="JPEG", quality=85)
        else:
            image.save(buffered, format=image_format)
        base64_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        size_kb = len(buffered.getvalue()) / 1024
        logger.debug(f"图片编码完成: {size_kb:.1f}KB, format={image_format}")
        return f"data:image/{image_format.lower()};base64,{base64_str}"

    def act(self, input_data: AgentInput) -> AgentOutput:
        """Agent 核心方法：根据输入生成动作，子类必须实现"""
        raise NotImplementedError("Subclass must implement act method")

    def reset(self):
        """重置 Agent 状态，在每个测试用例开始前调用"""
        pass

    def _call_api(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """调用大模型 API

        Args:
            messages: 符合 OpenAI 格式的消息列表
            **kwargs: 额外的 API 调用参数 (如 temperature, top_p)

        Returns:
            API 响应对象
        """
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("请安装 openai 包: pip install openai")

        import httpx

        # 从环境变量读取代理配置 (公司网络环境可能需要)
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(
            timeout=httpx.Timeout(300.0, connect=60.0),
            proxy=proxy_url if proxy_url else None
        )

        client = OpenAI(
            base_url=self._api_url,
            api_key=self._api_key,
            http_client=http_client
        )

        logger.info(f"[API调用] model={self._model_id}, url={self._api_url}")

        completion = client.chat.completions.create(
            model=self._model_id,
            messages=messages,
            extra_body={
                "thinking": {
                    "type": "disabled"
                }
            },
            timeout=300,
            **kwargs
        )

        return completion

    def extract_usage_info(self, response: Any) -> UsageInfo:
        """从 API 响应中提取 Token 使用信息"""
        usage = UsageInfo()

        if hasattr(response, 'usage') and response.usage:
            usage.input_tokens = (
                getattr(response.usage, 'prompt_tokens', 0) or
                getattr(response.usage, 'input_tokens', 0)
            )
            usage.output_tokens = (
                getattr(response.usage, 'completion_tokens', 0) or
                getattr(response.usage, 'output_tokens', 0)
            )
            usage.total_tokens = getattr(response.usage, 'total_tokens', 0)

            details = (
                getattr(response.usage, 'prompt_tokens_details', None) or
                getattr(response.usage, 'input_tokens_details', None)
            )
            if details and hasattr(details, 'cached_tokens'):
                usage.cached_tokens = details.cached_tokens or 0

            details = (
                getattr(response.usage, 'completion_tokens_details', None) or
                getattr(response.usage, 'output_tokens_details', None)
            )
            if details and hasattr(details, 'reasoning_tokens'):
                usage.reasoning_tokens = details.reasoning_tokens or 0

        return usage
