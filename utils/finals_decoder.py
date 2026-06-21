from __future__ import annotations

"""决赛通用动作解析器。

把多模态大模型可能输出的多种文本格式，稳健地解析为决赛框架要求的
标准动作常量（11 种）和标准参数字典。解析顺序为「从严到松」，并在
完全无法识别时兜底返回 COMPLETE，避免抛异常导致用例中断。

支持的模型动作书写形式（来自 system prompt 的 Action Space）：
    click(point='<point>x y</point>')
    long_press(point='<point>x y</point>', duration='ms')
    double_click(point='<point>x y</point>')
    type(content='...')
    scroll(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
    drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
    open(app_name='...')
    back()
    home()
    wait(seconds='2')
    complete(content='xxx') / finished(content='xxx')

同时兼容初赛风格的 CLICK:[[x,y]] / TYPE:['...'] / SCROLL:[[..],[..]] 等写法。
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple

from agent_base import (
    ACTION_BACK,
    ACTION_CLICK,
    ACTION_COMPLETE,
    ACTION_DOUBLE_CLICK,
    ACTION_DRAG,
    ACTION_HOME,
    ACTION_LONG_PRESS,
    ACTION_OPEN,
    ACTION_SCROLL,
    ACTION_TYPE,
    ACTION_WAIT,
)

logger = logging.getLogger(__name__)

ParsedAction = Tuple[str, Dict[str, Any]]

# 坐标范围 [0, 1000]
COORD_MIN = 0
COORD_MAX = 1000

# 默认参数
DEFAULT_LONG_PRESS_DURATION = 1000
DEFAULT_WAIT_SECONDS = 2

# 单个点的多种写法：<point>x y</point> 、[x, y] 、x,y 、x y
_NUM = r"-?\d+(?:\.\d+)?"
_POINT_TAG = re.compile(r"<point>\s*(" + _NUM + r")\s+(" + _NUM + r")\s*</point>", re.IGNORECASE)
_POINT_PAIR = re.compile(r"\[?\s*(" + _NUM + r")\s*[,\s]\s*(" + _NUM + r")\s*\]?")


def _to_int(value: Any) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _clamp(value: Any) -> int:
    return max(COORD_MIN, min(COORD_MAX, _to_int(value)))


def _clamp_point(point: Any) -> Optional[list]:
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        return [_clamp(point[0]), _clamp(point[1])]
    return None


def _extract_point(segment: str) -> Optional[list]:
    """从一段文本中提取首个坐标点。"""
    matched = _POINT_TAG.search(segment)
    if matched:
        return [_clamp(matched.group(1)), _clamp(matched.group(2))]
    matched = _POINT_PAIR.search(segment)
    if matched:
        return [_clamp(matched.group(1)), _clamp(matched.group(2))]
    return None


def _extract_two_points(segment: str) -> Optional[Tuple[list, list]]:
    """从一段文本中按顺序提取两个坐标点（用于 scroll / drag）。"""
    tags = _POINT_TAG.findall(segment)
    if len(tags) >= 2:
        return (
            [_clamp(tags[0][0]), _clamp(tags[0][1])],
            [_clamp(tags[1][0]), _clamp(tags[1][1])],
        )
    pairs = _POINT_PAIR.findall(segment)
    if len(pairs) >= 2:
        return (
            [_clamp(pairs[0][0]), _clamp(pairs[0][1])],
            [_clamp(pairs[1][0]), _clamp(pairs[1][1])],
        )
    return None


def clamp_action_payload(action_name: str, payload: Dict[str, Any]) -> ParsedAction:
    """把动作参数中的坐标限制在 [0, 1000]，其余字段原样保留。"""
    normalized = dict(payload)
    if action_name in (ACTION_CLICK, ACTION_LONG_PRESS, ACTION_DOUBLE_CLICK):
        point = _clamp_point(normalized.get("point"))
        if point is not None:
            normalized["point"] = point
    elif action_name in (ACTION_SCROLL, ACTION_DRAG):
        for key in ("start_point", "end_point"):
            point = _clamp_point(normalized.get(key))
            if point is not None:
                normalized[key] = point
    return action_name, normalized


def _strip_think_blocks(text: str) -> str:
    """剥离模型可能输出的思考块，避免其中的"伪 Action/坐标"干扰解析。

    覆盖 <think>...</think>、<think_never_used_xxx>...</think_never_used_xxx>
    等带后缀的变体（豆包等模型偶发输出）。未闭合时也尽量截断。
    """
    if "<think" not in text.lower():
        return text
    # 闭合的思考块（标签名可带任意后缀，惰性匹配）
    cleaned = re.sub(r"<think[^>]*>.*?</think[^>]*>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    # 未闭合：丢弃从最后一个 <think...> 起到末尾，但若其后仍有正式 Action 则保留其后部分
    open_match = re.search(r"<think[^>]*>", cleaned, re.IGNORECASE)
    if open_match:
        tail = cleaned[open_match.end():]
        after_action = re.search(r"(?is)action\s*:", tail)
        if after_action:
            cleaned = cleaned[:open_match.start()] + " " + tail[after_action.start():]
        else:
            cleaned = cleaned[:open_match.start()]
    return cleaned


def _isolate_action_line(text: str) -> str:
    """抽取 Action: 行；存在多行 Action 时取【最后一条】（模型自我纠正后的最终决策）。"""
    last = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if re.match(r"^[Aa]ction\s*:", line):
            last = re.sub(r"^[Aa]ction\s*:\s*", "", line)
    return last if last is not None else text


def decode_action(raw_output: str) -> ParsedAction:
    """解析模型输出为 (action, params)，无法识别时兜底 COMPLETE。"""
    text = (raw_output or "").strip()
    if not text:
        return ACTION_COMPLETE, {}

    text = _strip_think_blocks(text).strip()
    if not text:
        return ACTION_COMPLETE, {}

    focused = _isolate_action_line(text)

    parsed = _parse_functional(focused)
    if parsed:
        return clamp_action_payload(*parsed)

    parsed = _parse_functional(text)
    if parsed:
        return clamp_action_payload(*parsed)

    parsed = _parse_legacy_colon(text)
    if parsed:
        return clamp_action_payload(*parsed)

    parsed = _parse_loose(text)
    if parsed:
        return clamp_action_payload(*parsed)

    logger.warning("[decode_action] 解析失败，兜底 COMPLETE。output=%s", text[:300])
    return ACTION_COMPLETE, {}


def _parse_functional(text: str) -> Optional[ParsedAction]:
    """解析函数式写法：name(args)。按动作逐一匹配。"""

    # double_click 必须在 click 之前判断（含子串 click）
    matched = re.search(r"double[_\s]*click\s*\((.*?)\)", text, re.IGNORECASE | re.DOTALL)
    if matched:
        point = _extract_point(matched.group(1))
        if point:
            return ACTION_DOUBLE_CLICK, {"point": point}

    matched = re.search(r"long[_\s]*press\s*\((.*?)\)", text, re.IGNORECASE | re.DOTALL)
    if matched:
        body = matched.group(1)
        point = _extract_point(body)
        if point:
            duration_match = re.search(r"duration\s*=\s*['\"]?(" + _NUM + r")", body, re.IGNORECASE)
            duration = _to_int(duration_match.group(1)) if duration_match else DEFAULT_LONG_PRESS_DURATION
            if duration <= 0:
                duration = DEFAULT_LONG_PRESS_DURATION
            return ACTION_LONG_PRESS, {"point": point, "duration": duration}

    matched = re.search(r"\bclick\s*\((.*?)\)", text, re.IGNORECASE | re.DOTALL)
    if matched:
        point = _extract_point(matched.group(1))
        if point:
            return ACTION_CLICK, {"point": point}

    matched = re.search(r"\bscroll\s*\((.*?)\)", text, re.IGNORECASE | re.DOTALL)
    if matched:
        two = _extract_two_points(matched.group(1))
        if two:
            return ACTION_SCROLL, {"start_point": two[0], "end_point": two[1]}
        # scroll(point=..., direction=...) 形式 → 根据方向推算终点
        body = matched.group(1)
        point = _extract_point(body)
        if point:
            direction_match = re.search(r"direction\s*=\s*['\"]?(\w+)", body, re.IGNORECASE)
            direction = (direction_match.group(1).lower() if direction_match else "down")
            return ACTION_SCROLL, _scroll_from_direction(point, direction)

    matched = re.search(r"\bdrag\s*\((.*?)\)", text, re.IGNORECASE | re.DOTALL)
    if matched:
        two = _extract_two_points(matched.group(1))
        if two:
            return ACTION_DRAG, {"start_point": two[0], "end_point": two[1]}

    matched = re.search(r"\btype\s*\(\s*(?:content|text)\s*=\s*(['\"])(.*?)\1", text, re.IGNORECASE | re.DOTALL)
    if matched:
        return ACTION_TYPE, {"text": _normalize_type_text(matched.group(2))}

    matched = re.search(r"\bopen\s*\(\s*(?:app_name|app)\s*=\s*(['\"])(.*?)\1", text, re.IGNORECASE | re.DOTALL)
    if matched:
        return ACTION_OPEN, {"app_name": matched.group(2)}

    matched = re.search(r"\bwait\s*\((.*?)\)", text, re.IGNORECASE | re.DOTALL)
    if matched:
        seconds_match = re.search(r"(" + _NUM + r")", matched.group(1))
        seconds = _to_int(seconds_match.group(1)) if seconds_match else DEFAULT_WAIT_SECONDS
        if seconds <= 0:
            seconds = DEFAULT_WAIT_SECONDS
        return ACTION_WAIT, {"seconds": seconds}

    if re.search(r"\bback\s*\(", text, re.IGNORECASE):
        return ACTION_BACK, {}

    if re.search(r"\bhome\s*\(", text, re.IGNORECASE):
        return ACTION_HOME, {}

    if re.search(r"\b(?:complete|finished|finish)\s*\(", text, re.IGNORECASE):
        return ACTION_COMPLETE, {}

    return None


def _parse_legacy_colon(text: str) -> Optional[ParsedAction]:
    """兼容初赛 CLICK:[[x,y]] / TYPE:['..'] / SCROLL:[[..],[..]] / OPEN:['..'] 写法。"""
    matched = re.search(r"DOUBLE_CLICK\s*:?\s*\[?\s*\[?\s*(" + _NUM + r")\s*[,\s]\s*(" + _NUM + r")", text, re.IGNORECASE)
    if matched:
        return ACTION_DOUBLE_CLICK, {"point": [_clamp(matched.group(1)), _clamp(matched.group(2))]}

    matched = re.search(r"LONG_PRESS\s*:?\s*\[?\s*\[?\s*(" + _NUM + r")\s*[,\s]\s*(" + _NUM + r")", text, re.IGNORECASE)
    if matched:
        return ACTION_LONG_PRESS, {"point": [_clamp(matched.group(1)), _clamp(matched.group(2))], "duration": DEFAULT_LONG_PRESS_DURATION}

    matched = re.search(
        r"SCROLL\s*:\s*\[\s*\[\s*(" + _NUM + r")\s*,\s*(" + _NUM + r")\s*\]\s*,\s*\[\s*(" + _NUM + r")\s*,\s*(" + _NUM + r")\s*\]",
        text,
        re.IGNORECASE,
    )
    if matched:
        return ACTION_SCROLL, {
            "start_point": [_clamp(matched.group(1)), _clamp(matched.group(2))],
            "end_point": [_clamp(matched.group(3)), _clamp(matched.group(4))],
        }

    matched = re.search(
        r"DRAG\s*:\s*\[\s*\[\s*(" + _NUM + r")\s*,\s*(" + _NUM + r")\s*\]\s*,\s*\[\s*(" + _NUM + r")\s*,\s*(" + _NUM + r")\s*\]",
        text,
        re.IGNORECASE,
    )
    if matched:
        return ACTION_DRAG, {
            "start_point": [_clamp(matched.group(1)), _clamp(matched.group(2))],
            "end_point": [_clamp(matched.group(3)), _clamp(matched.group(4))],
        }

    matched = re.search(r"CLICK\s*:?\s*\[?\s*\[?\s*(" + _NUM + r")\s*[,\s]\s*(" + _NUM + r")", text, re.IGNORECASE)
    if matched:
        return ACTION_CLICK, {"point": [_clamp(matched.group(1)), _clamp(matched.group(2))]}

    matched = re.search(r"TYPE\s*:\s*\[?\s*['\"]([\s\S]*?)['\"]\s*\]?", text, re.IGNORECASE)
    if matched:
        return ACTION_TYPE, {"text": _normalize_type_text(matched.group(1))}

    matched = re.search(r"OPEN\s*:\s*\[?\s*['\"]([\s\S]*?)['\"]\s*\]?", text, re.IGNORECASE)
    if matched:
        return ACTION_OPEN, {"app_name": matched.group(1)}

    if re.search(r"\bWAIT\b", text):
        seconds_match = re.search(r"WAIT\s*:?\s*\[?\s*(" + _NUM + r")", text, re.IGNORECASE)
        seconds = _to_int(seconds_match.group(1)) if seconds_match else DEFAULT_WAIT_SECONDS
        return ACTION_WAIT, {"seconds": max(1, seconds)}

    if re.search(r"\bBACK\b", text):
        return ACTION_BACK, {}

    if re.search(r"\bHOME\b", text):
        return ACTION_HOME, {}

    if re.search(r"COMPLETE\s*:?\s*\[", text, re.IGNORECASE):
        return ACTION_COMPLETE, {}

    return None


def _parse_loose(text: str) -> Optional[ParsedAction]:
    """兜底：从散落文本里提取一个坐标当作 CLICK。"""
    point = _extract_point(text)
    if point:
        logger.info("[decode_action] 宽松提取坐标作为 CLICK: %s", point)
        return ACTION_CLICK, {"point": point}
    return None


def _normalize_type_text(text: str) -> str:
    """规整 type 文本：把模型表达"回车/换行提交"的各种写法统一为真换行符 \\n。

    背景：部分 App（如"得到"）没有可点的搜索按钮，只能靠回车键提交搜索；
    框架 device_controller.input_text 仅当 text == "\\n" 时才发送回车键(keyevent 66)。
    模型常把回车写成字面的 \\n、<enter>、回车 等，这里统一成真正的换行符，
    使其能正确触发回车。普通输入文本原样返回。
    """
    if not text:
        return ""
    stripped = text.strip()
    if stripped in ("\\n", "\\r\\n", "\\r", "\n", "\r\n", "\r"):
        return "\n"
    if stripped.lower() in ("<enter>", "[enter]", "{enter}", "enter", "回车", "换行", "确认搜索"):
        return "\n"
    return text


def _scroll_from_direction(point: list, direction: str) -> Dict[str, Any]:
    """根据方向把单点 + direction 转为 start_point/end_point。

    direction 表示手指滑动方向（内容移动方向的反向由模型自行判断），
    这里按字面方向给出一个合理的滑动向量。
    """
    x, y = point
    distance = 300
    if direction in ("down", "下"):
        end = [x, _clamp(y + distance)]
    elif direction in ("up", "上"):
        end = [x, _clamp(y - distance)]
    elif direction in ("right", "右"):
        end = [_clamp(x + distance), y]
    elif direction in ("left", "左"):
        end = [_clamp(x - distance), y]
    else:
        end = [x, _clamp(y - distance)]
    return {"start_point": [x, y], "end_point": end}
