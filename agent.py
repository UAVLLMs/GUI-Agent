"""决赛 GUI Agent —— 通用、模型驱动、自维护历史。

设计要点
========
- 继承官方 BaseAgent，实现 _initialize / reset / generate_messages / act。
- 决赛框架 (test_runner.py) 每步只传 instruction/current_image/step_count，
  不回传历史，因此 Agent 在内部自行维护「已执行动作历史」，并在每个用例
  开始时由框架调用 reset() 清空。
- 支持 11 种标准动作，输出严格符合框架 _validate_action 的标准参数格式。
- temperature=0，调用必须经由 self._call_api()（基类统一配置，禁止自建客户端）。
- 轻量通用护栏：坐标钳制、过早 COMPLETE 拦截、动作循环检测、OPEN 应用名规范化。
- 不含任何针对具体任务的硬编码坐标 / 分支，适应未知且实时的评测场景。

模型配置与安全
==============
- API Key 始终从环境变量 VLM_API_KEY 读取（基类已实现），绝不硬编码进代码。
- 模型 ID / API URL 完全沿用基类 agent_base.py 的官方配置，不做任何环境变量覆盖，
  保证本地运行与正式评测的执行完全一致。
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

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
    AgentInput,
    AgentOutput,
    BaseAgent,
    VALID_ACTIONS,
)
from utils.finals_decoder import clamp_action_payload, decode_action
from utils.finals_prompt import build_messages

logger = logging.getLogger(__name__)

# 应用中文名集合：用于 OPEN 应用名规范化。
# 优先复用框架 device_controller 的 APP_PACKAGE_MAP；若官方框架版本未导出该表，
# 则降级用内置副本，保证 agent.py 自包含、不会因 import 失败而无法加载（兼容官方命令）。
_BUILTIN_APP_NAMES = {
    "京东", "淘宝", "抖音", "美团", "拼多多", "哔哩哔哩", "百度地图", "喜马拉雅",
    "腾讯视频", "QQ音乐", "爱奇艺", "快手", "芒果TV", "QQ", "去哪儿旅行", "汽水音乐",
    "铁路12306", "12306", "平安好医生", "中国大学MOOC", "微信", "平安医生", "钉钉",
    "飞书", "番茄小说", "WPS Office", "携程旅行", "携程", "小红书", "微博",
    "网易云音乐", "高德地图", "大众点评", "今日头条", "腾讯地图", "红果免费短剧",
    "飞猪旅行", "Keep", "夸克", "百词斩", "酷狗音乐", "支付宝", "滴滴出行",
    "好大夫在线", "扫描全能王", "信息", "时钟", "豆瓣", "备忘录", "得到",
}
try:
    from device_controller import APP_PACKAGE_MAP as _DEVICE_APP_MAP
    _KNOWN_APP_NAMES = set(_DEVICE_APP_MAP.keys()) | _BUILTIN_APP_NAMES
except Exception:  # noqa: BLE001 - 官方框架若未导出该表也不应影响 Agent 加载
    _KNOWN_APP_NAMES = set(_BUILTIN_APP_NAMES)

# 需要坐标点的动作
_POINT_ACTIONS = {ACTION_CLICK, ACTION_LONG_PRESS, ACTION_DOUBLE_CLICK}
# 需要双点（起止）的动作
_TWO_POINT_ACTIONS = {ACTION_SCROLL, ACTION_DRAG}
# 默认温和向上滚动（约 20% 屏，用于过早完成 / 循环时的探索动作，与 _MAX_SCROLL_DIST 对齐，避免一次滑太多漏掉目标）
_EXPLORE_SCROLL = {"start_point": [500, 600], "end_point": [500, 400]}

# 「实质操作」动作：执行后界面理应发生变化（用于停滞反思）。
# WAIT/OPEN（应用启动有加载延迟）/COMPLETE 不参与停滞判定。
_ACTIONABLE = {
    ACTION_CLICK, ACTION_DOUBLE_CLICK, ACTION_LONG_PRESS,
    ACTION_SCROLL, ACTION_DRAG, ACTION_TYPE, ACTION_BACK,
}

# 平均哈希位数 32x32=1024 位；汉明距离 <= 阈值视为界面几乎未变。
# 用较细的网格 + 较小阈值，能识别"展开下拉/勾选筛选项"等小幅界面变化，避免误判停滞。
_AHASH_SIDE = 32
_STAGNATION_THRESHOLD = 10
_STAGNATION_NOTICE = (
    "上一步是一次实质操作，但当前界面与上一步几乎没有变化，说明该操作可能没有生效"
    "（点错了空白处、目标不可点、或元素位置不同）。请不要重复同样的点击：仔细观察当前截图，"
    "换一个更明确的目标元素，或先滚动/返回，再继续推进任务。"
)
# 连续相同点击/滚动等被打破时给模型的反思提示
_LOOP_NOTICE = (
    "你已经连续多次重复完全相同的操作但任务没有推进，系统已强制打断。"
    "请停止重复：重新观察当前截图，换一个不同的目标元素或不同的交互方式（如改点别处、滚动、返回）。"
)
# 连续等待加载仍无进展时的温和提示（软干预，不改动作；网络慢时仍可继续等）
_STUCK_LOADING_NOTICE = (
    "你已连续多次 wait 但界面没有明显变化。如果确实还在加载，可以再等一次；"
    "若页面其实已加载好或疑似卡住，请直接操作可见元素或换一条路径，不要无谓地一直 wait。"
)
# 重复打开同一应用却没反应时的提示
_APP_UNAVAILABLE_NOTICE = (
    "应用「{app}」多次 open 后界面仍停留在桌面，说明该应用可能未安装或无法直接打开。"
    "请改用本机已安装的、能完成同类任务的其它应用来达成目标"
    "（例如：找地点/附近商家/导航 → 高德地图；听歌 → QQ音乐；看视频 → 哔哩哔哩/爱奇艺），"
    "不要再反复 open 同一个应用。"
)
# 界面连续多步「画面无变化 + 重复点同一处」达到此阈值 → 判定真卡死，强制返回脱困。
_STUCK_ESCAPE_STREAK = 6
# 判定"重复相似动作"的坐标接近阈值（归一化 [0,1000]）
_SIMILAR_POINT_DIST = 60

# 置顶记忆（模型 REMEMBER 标记）的容量上限：条数 + 单条字符
# 置顶记忆（模型 REMEMBER 标记）的容量上限：条数 + 单条字符。
# 单条上限需足够容纳"5 首歌名+歌手"这类整条清单（英文名较长），过小会截断丢失后几项。
# 几十步的长任务里关键事实较多（榜单、已加/已拒清单、各项筛选条件、各子目标进度），
# 故放宽容量，避免后期把前面记住的内容挤掉；单条调大以容纳更长的清单。
_MEMORY_MAX_ITEMS = 16
_MEMORY_MAX_CHARS = 500
_STUCK_PAGE_NOTICE = (
    "界面已连续多步没有任何变化，可能卡在加载失败/弹窗/死页面上，系统已为你执行一次返回(back)。"
    "请重新观察当前界面：换一条路径、重新进入目标页面，或换一个目标元素，不要再做无效的重复操作。"
)
# 最近帧滑动窗口大小（供 _dead_loading 等使用）
_NOPROGRESS_WINDOW = 6
# 方案A 死加载兜底：连续 N 步纯 WAIT 且画面完全冻结（去重=1）→ 判定加载失败，强制 BACK。
# 覆盖"纯 WAIT 参数交替 + 黑屏/冻结"这种连续相同护栏与全局无进展都漏掉的永久卡死。
_DEAD_LOADING_STREAK = 5
_DEAD_LOADING_NOTICE = (
    "页面长时间停留在加载/黑屏状态且画面毫无变化，很可能加载失败，系统已为你执行一次返回(back)。"
    "请换一条路径、重新进入目标页面或改用其它入口/应用，不要再持续等待。"
)
# 方案B 动作原地打转：连续 N 步都点同一小块区域（画面可能因动画/轮播一直在变）→ 打断为探索滚动。
# 覆盖"动态背景导致画面哈希每帧不同、但模型反复戳同一无效区域(坐标微抖)"的盲区。
_SAME_SPOT_STREAK = 5
# 单次 SCROLL/DRAG 的最大幅度（归一化 [0,1000]，约 15% 屏）：防止一次滑太多把目标（如某行房型/某首歌）划走。
# 小步滑动让列表选行更精准、更不易越过目标行；长列表顶多多滑几次，30/50/80 步预算足够。
_MAX_SCROLL_DIST = 150

# 常见 App 口语化 / 简称 → APP_PACKAGE_MAP 标准中文键 的别名表（问题6：名称泛化）
_APP_ALIASES = {
    "b站": "哔哩哔哩", "bilibili": "哔哩哔哩", "哔哩": "哔哩哔哩", "小破站": "哔哩哔哩",
    "滴滴": "滴滴出行", "滴滴打车": "滴滴出行",
    "高德": "高德地图", "高德导航": "高德地图", "amap": "高德地图",
    "携程": "携程旅行",
    "qq音乐": "QQ音乐", "qqmusic": "QQ音乐",
    "网易云": "网易云音乐", "网易云音乐app": "网易云音乐",
    "去哪儿": "去哪儿旅行",
    "飞猪": "飞猪旅行",
    "wps": "WPS Office",
    "酷狗": "酷狗音乐",
    "12306": "铁路12306",
    # 扩充：常见口语简称 / 拼音 / 英文名（仅映射到本机已知应用，避免歧义词如"百度""腾讯"单独成项）
    "头条": "今日头条", "今日头条app": "今日头条",
    "点评": "大众点评", "dianping": "大众点评",
    "奇艺": "爱奇艺", "iqiyi": "爱奇艺",
    "芒果": "芒果TV", "芒果tv": "芒果TV", "mgtv": "芒果TV",
    "weibo": "微博",
    "红书": "小红书", "xhs": "小红书", "小红薯": "小红书",
    "taobao": "淘宝", "tb": "淘宝",
    "jd": "京东", "jingdong": "京东",
    "meituan": "美团",
    "douyin": "抖音",
    "kuaishou": "快手",
    "alipay": "支付宝", "zfb": "支付宝",
    "weixin": "微信", "wechat": "微信",
    "喜马": "喜马拉雅", "ximalaya": "喜马拉雅",
    "pdd": "拼多多",
    "quark": "夸克",
    "keep": "Keep",
    "汽水": "汽水音乐", "汽水音乐app": "汽水音乐",
    "番茄": "番茄小说", "番茄免费小说": "番茄小说",
    "慕课": "中国大学MOOC", "mooc": "中国大学MOOC",
}


class Agent(BaseAgent):
    """决赛通用 GUI Agent。"""

    def _initialize(self):
        # 调用模型的重试次数
        self.retry_count = 3
        # 采样温度（用户要求设为 0，输出更确定、更稳定）
        self.temperature = 0
        # 单次输出 token 上限：给足余量，避免模型把 Thought 写太长时把后面的 Action 行挤掉被截断。
        # 注意这是「单次回复的天花板」而非目标值：正常一步只用 ~150-250 token，调大只在模型啰嗦时才会用到，
        # 不会平白消耗总预算。设较宽松值确保 Action / 长 REMEMBER 清单永不被截。
        self.max_output_tokens = 1500
        # 自维护的结构化动作历史：[{"step", "action", "parameters"}, ...]
        self._action_history: List[Dict[str, Any]] = []
        # 界面停滞反思：上一帧截图哈希 + 上一步是否实质操作
        self._prev_hash: Optional[int] = None
        self._prev_actionable: bool = False
        self._cur_hash: Optional[int] = None
        # 卡死脱困：连续"实质操作但界面无变化"的步数 + 本步是否停滞
        self._stuck_streak: int = 0
        self._stuck_now: bool = False
        # 全局无进展检测：最近若干帧截图哈希滑动窗口（跨动作类型，抓画面横跳死循环）
        self._recent_frames: List[int] = []
        # 待注入下一步 prompt 的反思提示（由护栏在本步设置，下一步消费）
        self._pending_notice: str = ""
        # 置顶关键记忆（模型 REMEMBER 标记）：支持「带键覆盖」语义。
        # 结构为有序字典 store_key -> (展示键, 值)：带键(REMEMBER【键】:)同键覆盖、
        # 无键(REMEMBER:)按内容去重追加。用于持久化「计划/进度/清单」等跨步信息。
        self._memory: Dict[str, Tuple[str, str]] = {}

        # 模型 ID / API URL / API Key 完全沿用基类（agent_base.py）的官方配置，
        # 不做任何环境变量覆盖，保证与赛题执行完全一致。
        logger.info(
            "[Agent] 初始化完成 model=%s url=%s temperature=%s",
            self.model_id, self.api_url, self.temperature,
        )

    def reset(self):
        """每个测试用例开始前由框架调用，清空内部历史。"""
        self._action_history = []
        self._prev_hash = None
        self._prev_actionable = False
        self._cur_hash = None
        self._stuck_streak = 0
        self._stuck_now = False
        self._recent_frames = []
        self._pending_notice = ""
        self._memory = {}
        logger.info("[Agent] 历史已重置")

    def generate_messages(self, input_data: AgentInput) -> List[Dict[str, Any]]:
        """构造发给模型的消息：通用 prompt（注入自维护历史 + 护栏/停滞反思）+ 当前截图。

        注：截图哈希与停滞判定已在 act() 开头完成（self._stuck_now），此处只组装提示。
        """
        notices: List[str] = []
        # 护栏在上一步设置的提示（应用不可用 / 循环打破 / 卡加载 / 强制脱困）
        if self._pending_notice:
            notices.append(self._pending_notice)
            self._pending_notice = ""
        # 界面停滞反思：连续 ≥2 步实质操作后画面仍无变化才提示（单步无变化属正常重试，不打扰）
        if self._stuck_streak >= 2:
            notices.append(_STAGNATION_NOTICE)
        notice = "\n".join(notices)
        image_url = self._encode_image(input_data.current_image)
        # 把带键记忆渲染成展示行：有键则「键：值」，无键直接值
        memory_lines = [
            (f"{key}：{val}" if key else val) for (key, val) in self._memory.values()
        ]
        return build_messages(
            instruction=input_data.instruction,
            history=self._action_history,
            image_url=image_url,
            notice=notice,
            memory=memory_lines,
        )

    def act(self, input_data: AgentInput) -> AgentOutput:
        """核心方法：根据指令与当前截图，输出下一步标准动作。"""
        # 0. 先做界面停滞/卡死检测（与上一帧比较）
        self._cur_hash = self._average_hash(input_data.current_image)
        # 维护最近帧滑动窗口（用于"全局无进展"横跳检测）
        self._recent_frames.append(self._cur_hash)
        if len(self._recent_frames) > _NOPROGRESS_WINDOW:
            self._recent_frames = self._recent_frames[-_NOPROGRESS_WINDOW:]
        self._stuck_now = bool(
            self._prev_actionable
            and self._prev_hash is not None
            and self._hamming(self._cur_hash, self._prev_hash) <= _STAGNATION_THRESHOLD
        )
        self._stuck_streak = self._stuck_streak + 1 if self._stuck_now else 0

        # 卡死脱困：连续多步「画面无变化」**且**一直在「重复点同一处」→ 判定真卡死，强制 BACK。
        # 多选筛选每步点不同坐标，_recent_actions_repetitive 要求"同处重复"而不满足，全程不触发。
        # 不再做"软提示自救"——它在模型正常重试时只是干扰；真卡死交由本兜底 + dead_loading 处理。
        if self._stuck_streak >= _STUCK_ESCAPE_STREAK and self._recent_actions_repetitive():
            logger.info("[Agent] 界面连续 %s 步无变化且重复点同处，强制 BACK 脱困", self._stuck_streak)
            self._stuck_streak = 0
            self._recent_frames = []  # 脱困后清窗，避免连环触发
            action, params = ACTION_BACK, {}
            self._record(input_data.step_count, action, params)
            self._prev_hash = self._cur_hash
            self._prev_actionable = True
            self._pending_notice = _STUCK_PAGE_NOTICE
            return AgentOutput(
                action=action, parameters=params,
                raw_output="[guard] screen frozen -> forced back", usage=None,
            )

        # 死加载兜底（方案A）：连续多步纯 WAIT 且画面完全冻结 → 加载失败，强制 BACK 换路径。
        if self._dead_loading():
            logger.info("[Agent] 连续纯 WAIT 且画面冻结，判定死加载，强制 BACK 脱困")
            self._recent_frames = []
            self._stuck_streak = 0
            action, params = ACTION_BACK, {}
            self._record(input_data.step_count, action, params)
            self._prev_hash = self._cur_hash
            self._prev_actionable = True
            self._pending_notice = _DEAD_LOADING_NOTICE
            return AgentOutput(
                action=action, parameters=params,
                raw_output="[guard] dead loading -> forced back", usage=None,
            )

        # 动作原地打转（方案B）：连续多步戳同一小块区域（画面可能因动画在变）→ 打断为探索滚动。
        if self._action_stuck_same_spot():
            logger.info("[Agent] 连续多步点击同一小块区域，判定原地打转，打断为探索滚动")
            self._stuck_streak = 0
            action, params = ACTION_SCROLL, dict(_EXPLORE_SCROLL)
            self._record(input_data.step_count, action, params)
            self._prev_hash = self._cur_hash
            self._prev_actionable = True
            self._pending_notice = _LOOP_NOTICE
            return AgentOutput(
                action=action, parameters=params,
                raw_output="[guard] same-spot loop -> forced scroll", usage=None,
            )

        # 反复点击同一小区域的"软提示"已移除：它在模型正常重试时只是噪声，
        # 且真正的原地打转已由上方 _action_stuck_same_spot（强制滚动）兜底。

        messages = self.generate_messages(input_data)
        content, usage = self._call_llm_with_retry(messages)

        if not content:
            # 模型多次重试仍无有效返回（网络/限流/内容过滤等瞬时问题）。
            # 关键：绝不能输出空动作——框架会判定「动作类型为空」直接中止整条用例。
            # 这里降级为一次 WAIT，让用例存活到下一步重新尝试。
            logger.error("[Agent] 模型无有效返回，降级为 WAIT 保持用例继续")
            action, params = ACTION_WAIT, {"seconds": 2}
            self._record(input_data.step_count, action, params)
            self._prev_hash = self._cur_hash
            self._prev_actionable = False
            return AgentOutput(action=action, parameters=params, raw_output="", usage=usage)

        logger.info("[Agent] step=%s raw=%s", input_data.step_count, content[:300])

        # 捕获模型 REMEMBER 标记，写入置顶记忆（跨步长期保留）
        self._capture_memory(content)

        # 1. 解析为标准动作
        action, params = decode_action(content)

        # 1b. 误判 COMPLETE 防护：decode 在「完全无法解析 / 本步只有思考块没有 Action」时
        #     会兜底 COMPLETE，这与模型真想结束无法区分。若模型本步文本里并无任何"完成/complete"
        #     意图，说明这是一次解析失败（瞬时乱码/纯思考），绝不能就此结束整条任务——降级为 WAIT
        #     保活，让下一步重新决策。真正的 complete(...) 文本必含 complete 关键字，不会被误降级。
        if action == ACTION_COMPLETE and not re.search(
            r"complete|finished|finish|完成|done|结束|搞定|已.*成功", content, re.IGNORECASE
        ):
            logger.warning("[Agent] decode 兜底 COMPLETE 但无完成意图，判为解析失败，降级 WAIT 保活")
            action, params = ACTION_WAIT, {"seconds": 1}

        # 2. 通用护栏修正
        action, params = self._apply_guards(input_data, action, params)

        # 3. 坐标钳制 + 标准化兜底
        action, params = clamp_action_payload(action, params)
        action, params = self._ensure_valid(action, params)

        # 4. 记录到历史（COMPLETE 也记录，便于上下文）；附带精简 Thought 作为跨步记忆
        self._record(input_data.step_count, action, params, self._extract_thought(content))

        # 5. 更新停滞反思状态：本帧哈希 + 本步是否实质操作
        self._prev_hash = self._cur_hash
        self._prev_actionable = action in _ACTIONABLE

        return AgentOutput(action=action, parameters=params, raw_output=content, usage=usage)

    # ------------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------------
    def _call_llm_with_retry(self, messages: List[Dict[str, Any]]) -> Tuple[str, Any]:
        for attempt in range(self.retry_count):
            try:
                completion = self._call_api(
                    messages, temperature=self.temperature, max_tokens=self.max_output_tokens
                )
                content = completion.choices[0].message.content or ""
                usage = self.extract_usage_info(completion)
                return content, usage
            except Exception as exc:  # noqa: BLE001 - 网络/接口异常需重试
                logger.error(
                    "[Agent] 模型调用失败 (%s/%s): %s",
                    attempt + 1, self.retry_count, exc,
                )
                if attempt < self.retry_count - 1:
                    time.sleep(1.5 * (attempt + 1))  # 线性退避，缓解限流/瞬时网络抖动
        return "", None

    # ------------------------------------------------------------------
    # 通用护栏（无任务硬编码）
    # ------------------------------------------------------------------
    def _apply_guards(
        self, input_data: AgentInput, action: str, params: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        history = self._action_history
        step_total = len(history)

        # 1. 过早 COMPLETE 拦截：还没做任何实质操作就想完成 → 改为探索性滚动
        if action == ACTION_COMPLETE and step_total <= 1:
            logger.info("[Agent] 拦截过早 COMPLETE，改为探索滚动")
            return ACTION_SCROLL, dict(_EXPLORE_SCROLL)

        # 1b. SCROLL 幅度钳制：单次滑动过大易把目标划走（如低价房型），限制到合理幅度
        if action == ACTION_SCROLL:
            params = self._limit_scroll(params)

        # 2. OPEN 应用名规范化 + 重复打开治理
        if action == ACTION_OPEN:
            app_name = self._normalize_app_name(params.get("app_name", ""))
            params = {"app_name": app_name}
            # 最近 3 步里对同一 App 的 OPEN 次数（含本次之前已发生的）
            same_open = [
                r for r in history[-3:]
                if r.get("action") == ACTION_OPEN
                and (r.get("parameters") or {}).get("app_name") == app_name
            ]
            # 第 3 次仍在 open 同一个 App → 大概率未安装/打不开：回桌面 + 提示换用同类已装应用
            if len(same_open) >= 2:
                logger.info("[Agent] 应用 %s 多次 open 无效，回桌面并提示换应用", app_name)
                self._pending_notice = _APP_UNAVAILABLE_NOTICE.format(app=app_name)
                return ACTION_HOME, {}

        # 3. 动作循环检测：连续 3 次完全相同的动作+参数 → 升级式打破（绝不强制 COMPLETE）
        if step_total >= 3:
            recent = history[-3:]
            same_loop = all(
                r.get("action") == action and str(r.get("parameters")) == str(params)
                for r in recent
            )
            if same_loop:
                logger.info("[Agent] 检测到动作循环（连续3次相同 %s）", action)
                if action == ACTION_WAIT:
                    # WAIT 循环不再强制 BACK：网络慢时页面可能仍在加载，盲目返回会丢掉正在加载的页面。
                    # 仅软提示；真正"画面长时间冻结"的死加载由 _dead_loading 统一兜底返回（它会判画面是否冻结）。
                    self._pending_notice = _STUCK_LOADING_NOTICE
                    return action, params
                # 点击/滚动等循环 → 探索滚动打破 + 反思提示
                self._pending_notice = _LOOP_NOTICE
                return ACTION_SCROLL, dict(_EXPLORE_SCROLL)

        # 4. 连续 2 次 WAIT（参数可不同）→ 软提示别再傻等（不改动作，给模型自决机会）
        if action == ACTION_WAIT and step_total >= 2:
            if history[-1].get("action") == ACTION_WAIT and history[-2].get("action") == ACTION_WAIT:
                self._pending_notice = _STUCK_LOADING_NOTICE

        return action, params

    @staticmethod
    def _normalize_app_name(raw: str) -> str:
        name = str(raw or "").strip().strip("'\"").strip()
        if not name:
            return name
        # 1. 别名表（大小写不敏感）：b站→哔哩哔哩、滴滴→滴滴出行 等
        lowered = name.lower()
        for alias, target in _APP_ALIASES.items():
            if lowered == alias.lower():
                return target
        # 2. 精确命中
        if name in _KNOWN_APP_NAMES:
            return name
        # 3. 去空格紧凑匹配
        compact = name.replace(" ", "")
        for known in _KNOWN_APP_NAMES:
            if compact == known.replace(" ", ""):
                return known
        # 4. 包含匹配（如「得到App」→得到、「QQ音乐app」→QQ音乐）；长键优先避免「QQ音乐」误命中「QQ」
        for known in sorted(_KNOWN_APP_NAMES, key=len, reverse=True):
            if known in name:
                return known
        return name

    # ------------------------------------------------------------------
    # 输出标准化兜底，确保通过框架 _validate_action
    # ------------------------------------------------------------------
    def _ensure_valid(self, action: str, params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        if action not in VALID_ACTIONS:
            logger.warning("[Agent] 非法动作 %s，兜底 COMPLETE", action)
            return ACTION_COMPLETE, {}

        if action in _POINT_ACTIONS:
            point = params.get("point")
            if not (isinstance(point, list) and len(point) == 2):
                logger.warning("[Agent] %s 缺少有效 point，兜底 COMPLETE", action)
                return ACTION_COMPLETE, {}
            if action == ACTION_LONG_PRESS:
                duration = params.get("duration", 1000)
                try:
                    duration = int(float(duration))
                except (TypeError, ValueError):
                    duration = 1000
                return action, {"point": point, "duration": max(1, duration)}
            return action, {"point": point}

        if action in _TWO_POINT_ACTIONS:
            sp, ep = params.get("start_point"), params.get("end_point")
            if not (isinstance(sp, list) and len(sp) == 2 and isinstance(ep, list) and len(ep) == 2):
                logger.warning("[Agent] %s 缺少有效起止点，兜底 COMPLETE", action)
                return ACTION_COMPLETE, {}
            return action, {"start_point": sp, "end_point": ep}

        if action == ACTION_TYPE:
            return action, {"text": str(params.get("text", ""))}

        if action == ACTION_OPEN:
            app_name = params.get("app_name", "")
            if not app_name:
                logger.warning("[Agent] OPEN 缺少 app_name，兜底 COMPLETE")
                return ACTION_COMPLETE, {}
            return action, {"app_name": app_name}

        if action == ACTION_WAIT:
            seconds = params.get("seconds", 2)
            try:
                seconds = int(float(seconds))
            except (TypeError, ValueError):
                seconds = 2
            return action, {"seconds": max(1, seconds)}

        # COMPLETE / BACK / HOME 无参数
        return action, {}

    # ------------------------------------------------------------------
    def _record(self, step: int, action: str, params: Dict[str, Any], note: str = "") -> None:
        record = {"step": step, "action": action, "parameters": params}
        if note:
            record["note"] = note
        self._action_history.append(record)

    @staticmethod
    def _extract_thought(content: str) -> str:
        """从模型输出里抽取精简的 Thought，作为跨步"观察记忆"。

        只取 Thought 首句、压成单行并截断，避免长篇内心独白污染历史/撑大 token。
        """
        text = content or ""
        match = re.search(r"[Tt]hought\s*[:：]\s*(.+)", text)
        thought = match.group(1) if match else text
        # 去掉后续可能跟着的 Action 段，只保留思考本身
        thought = re.split(r"\n?\s*[Aa]ction\s*[:：]", thought)[0]
        thought = " ".join(thought.split())  # 折叠空白为单行
        limit = 90
        if len(thought) > limit:
            thought = thought[:limit] + "…"
        return thought

    def _capture_memory(self, content: str) -> None:
        """抽取模型 `REMEMBER` 标记写入置顶记忆，支持「带键覆盖」语义。

        - 带键：`REMEMBER【键】: 值` / `REMEMBER[键]: 值` / `REMEMBER(键): 值`
          → 同键覆盖（用于「计划/进度/已加清单」等会随步骤变化的信息，避免旧快照堆积矛盾）。
        - 无键：`REMEMBER: 值` → 按内容去重追加（用于固定事实，如榜单 5 首歌）。
        超过条数上限时保留最近若干条。
        """
        if not content or "remember" not in content.lower():
            return
        pattern = re.compile(
            r"REMEMBER\s*(?:[\[【(（]\s*(?P<key>[^\]】)）\r\n]{1,24})\s*[\]】)）])?\s*[:：]\s*(?P<val>.+)",
            re.IGNORECASE,
        )
        for m in pattern.finditer(content):
            key = (m.group("key") or "").strip()
            val = " ".join(m.group("val").split())
            # 去掉可能误带进来的后续标记
            val = re.split(
                r"\b(?:REMEMBER|Action|Thought)\b\s*[:：]", val, flags=re.IGNORECASE
            )[0].strip()
            if not val:
                continue
            if len(val) > _MEMORY_MAX_CHARS:
                val = val[:_MEMORY_MAX_CHARS] + "…"
            store_key = key if key else val  # 无键时以内容为键去重
            existed = store_key in self._memory
            self._memory[store_key] = (key, val)
            if existed:
                logger.info("[Agent] 置顶记忆更新：%s%s", f"[{key}] " if key else "", val)
            else:
                logger.info("[Agent] 置顶记忆新增：%s%s", f"[{key}] " if key else "", val)
        # 超出条数上限时保留最近的若干条（dict 按插入有序）
        if len(self._memory) > _MEMORY_MAX_ITEMS:
            for stale in list(self._memory.keys())[:-_MEMORY_MAX_ITEMS]:
                del self._memory[stale]

    # ------------------------------------------------------------------
    # 界面停滞反思：对截图做平均哈希，比较相邻帧相似度
    # ------------------------------------------------------------------
    @staticmethod
    def _average_hash(image) -> int:
        """32x32 灰度平均哈希，返回 1024 位整数。"""
        try:
            small = image.convert("L").resize((_AHASH_SIDE, _AHASH_SIDE))
            pixels = list(small.getdata())
            avg = sum(pixels) / len(pixels)
            bits = 0
            for index, value in enumerate(pixels):
                if value >= avg:
                    bits |= (1 << index)
            return bits
        except Exception:  # noqa: BLE001 - 哈希失败不应影响主流程
            return 0

    @staticmethod
    def _hamming(hash_a: int, hash_b: int) -> int:
        return bin(hash_a ^ hash_b).count("1")

    # ------------------------------------------------------------------
    # 判定最近两次实质操作是否"重复相似"（用于卡死脱困门控）
    # ------------------------------------------------------------------
    def _recent_actions_repetitive(self) -> bool:
        actionable = [r for r in self._action_history if r.get("action") in _ACTIONABLE]
        if len(actionable) < 2:
            return False
        last, prev = actionable[-1], actionable[-2]
        if last.get("action") != prev.get("action"):
            return False
        pa = last.get("parameters") or {}
        pb = prev.get("parameters") or {}
        # 点类动作：坐标相近即视为重复（反复点同一处）
        if "point" in pa and "point" in pb:
            return self._point_close(pa["point"], pb["point"])
        # 双点动作（滚动/拖拽）：起止点都相近才算重复
        if "start_point" in pa and "start_point" in pb:
            return (
                self._point_close(pa.get("start_point"), pb.get("start_point"))
                and self._point_close(pa.get("end_point"), pb.get("end_point"))
            )
        # 无坐标动作（TYPE/BACK/HOME 等）：参数完全相同才算重复
        return pa == pb

    @staticmethod
    def _point_close(p: Any, q: Any, dist: int = _SIMILAR_POINT_DIST) -> bool:
        if not (isinstance(p, (list, tuple)) and isinstance(q, (list, tuple))
                and len(p) >= 2 and len(q) >= 2):
            return False
        return abs(p[0] - q[0]) <= dist and abs(p[1] - q[1]) <= dist

    def _dead_loading(self) -> bool:
        """死加载兜底：连续 N 步纯 WAIT 且画面完全冻结（去重=1）→ 判定加载失败。

        覆盖"纯 WAIT 参数交替 + 黑屏/冻结"这种连续相同护栏与全局无进展都漏掉的永久卡死。
        要求连续多步纯等待且一帧未变，正常加载页几秒内必有变化，几乎不会误伤。
        """
        n = _DEAD_LOADING_STREAK
        if len(self._recent_frames) < n:
            return False
        # 历史里最近 n-1 步必须全是 WAIT（当前步尚未决策）
        recent_actions = [r.get("action") for r in self._action_history[-(n - 1):]]
        if len(recent_actions) < (n - 1) or any(a != ACTION_WAIT for a in recent_actions):
            return False
        # 最近 n 帧画面完全冻结（彼此都相似 → 去重=1）
        frames = self._recent_frames[-n:]
        base = frames[0]
        return all(self._hamming(base, h) <= _STAGNATION_THRESHOLD for h in frames)

    def _action_stuck_same_spot(self) -> bool:
        """动作原地打转：连续 N 步都是同一种点类动作且坐标集中在小范围内。

        覆盖"页面因动画/轮播导致画面哈希每帧不同（画面类护栏失效），但模型反复戳
        同一无效小区域（坐标微抖）"的盲区。打断为探索滚动而非 BACK，避免误退出有效页面。
        """
        n = _SAME_SPOT_STREAK
        recent = self._action_history[-n:]
        if len(recent) < n:
            return False
        acts = [r.get("action") for r in recent]
        if acts[0] not in _POINT_ACTIONS or any(a != acts[0] for a in acts):
            return False
        pts = [(r.get("parameters") or {}).get("point") for r in recent]
        if any(not (isinstance(p, (list, tuple)) and len(p) >= 2) for p in pts):
            return False
        p0 = pts[0]
        return all(self._point_close(p0, p) for p in pts)

    @staticmethod
    def _limit_scroll(params: Dict[str, Any]) -> Dict[str, Any]:
        """限制单次 scroll/drag 幅度，避免一次滑太多把目标（如低价房型）划走。

        保持起点与滑动方向不变，仅把过大的位移截断到 _MAX_SCROLL_DIST，并夹到 [0,1000]。
        """
        sp = params.get("start_point")
        ep = params.get("end_point")
        if not (isinstance(sp, (list, tuple)) and len(sp) >= 2
                and isinstance(ep, (list, tuple)) and len(ep) >= 2):
            return params
        sx, sy, ex, ey = sp[0], sp[1], ep[0], ep[1]
        dx, dy = ex - sx, ey - sy
        if abs(dx) > _MAX_SCROLL_DIST:
            ex = sx + (_MAX_SCROLL_DIST if dx > 0 else -_MAX_SCROLL_DIST)
        if abs(dy) > _MAX_SCROLL_DIST:
            ey = sy + (_MAX_SCROLL_DIST if dy > 0 else -_MAX_SCROLL_DIST)
        clip = lambda v: max(0, min(1000, int(v)))
        return {"start_point": [clip(sx), clip(sy)], "end_point": [clip(ex), clip(ey)]}
