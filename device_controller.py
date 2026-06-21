"""
ADB 设备控制层 -- 决赛版本

封装 Android Debug Bridge (ADB) 命令，提供手机屏幕截图和执行动作的能力。
所有坐标使用归一化坐标 [0, 1000]，由控制器内部转换为像素坐标。
"""

import os
import subprocess
import time
import logging
import tempfile
from io import BytesIO
from typing import Optional, Tuple

from PIL import Image

logger = logging.getLogger(__name__)


# ==========================================
#               应用包名映射
# ==========================================

APP_PACKAGE_MAP = {
    "京东": "com.jingdong.app.mall",
    "淘宝": "com.taobao.taobao",
    "抖音": "com.ss.android.ugc.aweme",
    "美团": "com.sankuai.meituan",
    "拼多多": "com.xunmeng.pinduoduo",
    "哔哩哔哩": "tv.danmaku.bili",
    "百度地图": "com.baidu.BaiduMap",
    "喜马拉雅": "com.ximalaya.ting.android",
    "腾讯视频": "com.tencent.qqlive",
    "QQ音乐": "com.tencent.qqmusic",
    "爱奇艺": "com.qiyi.video",
    "快手": "com.smile.gifmaker",
    "芒果TV": "com.hunantv.imgo.activity",
    "QQ": "com.tencent.mobileqq",
    "去哪儿旅行": "com.Qunar",
    "汽水音乐": "com.luna.music",
    "铁路12306": "com.MobileTicket",
    "12306": "com.MobileTicket",
    "平安好医生": "com.pajk.health",
    "中国大学MOOC": "com.netease.edu.ucmooc",
    "微信": "com.tencent.mm",
    "平安医生": "com.pingan.pafm",
    "钉钉": "com.alibaba.android.rimet",
    "飞书": "com.ss.android.lark",
    "番茄小说": "com.dragon.read",
    "WPS Office": "cn.wps.moffice_eng",
    "携程旅行": "ctrip.android.view",
    "携程": "ctrip.android.view",
    "小红书": "com.xingin.xhs",
    "微博": "com.sina.weibo",
    "网易云音乐": "com.netease.cloudmusic",
    "高德地图": "com.autonavi.minimap",
    "大众点评": "com.dianping.v1",
    "今日头条": "com.ss.android.article.news",
    "腾讯地图": "com.tencent.map",
    "红果免费短剧": "com.phoenix.read",
    "飞猪旅行": "com.taobao.trip",
    "Keep": "com.gotokeep.keep",
    "夸克": "com.alibaba.android.quark",
    "百词斩": "com.jiongji.android.card",
    "酷狗音乐": "com.kugou.android",
    "支付宝": "com.eg.android.AlipayGphone",
    "滴滴出行": "com.sdu.didi.psnger",
    "好大夫在线": "com.haodf.android",
    "扫描全能王": "com.intsig.camscanner",
    "信息": "com.android.messaging",
    "时钟": "zte.com.cn.alarmclock",
    "豆瓣": "com.douban.frodo",
    "备忘录": "cn.nubia.notepad.preset",
    "得到":"com.luojilab.player"
}


class DeviceError(Exception):
    """设备操作异常"""
    pass


class DeviceController:
    """ADB 设备控制器"""

    def __init__(self, serial: str = None):
        self.serial = serial
        self.screen_width: int = 0
        self.screen_height: int = 0
        self._connected: bool = False
        self._original_ime: str = ""

    @staticmethod
    def list_devices() -> list:
        """列出所有已连接的 ADB 设备

        Returns:
            list of dict: [{"serial": "...", "state": "device"}, ...]
        """
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True, text=True
        )
        devices = []
        for line in result.stdout.strip().split("\n")[1:]:
            if line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    devices.append({"serial": parts[0], "state": parts[1]})
        return devices

    def connect(self) -> bool:
        """连接设备并获取屏幕分辨率

        Returns:
            bool: 是否连接成功
        """
        devices = self.list_devices()
        online_devices = [d for d in devices if d["state"] == "device"]

        if not online_devices:
            logger.error("未检测到已连接的 ADB 设备")
            raise DeviceError("未检测到已连接的 ADB 设备。请确保手机已连接并开启 USB 调试。")

        if self.serial:
            matching = [d for d in online_devices if d["serial"] == self.serial]
            if not matching:
                raise DeviceError(
                    f"未找到指定设备 {self.serial}。"
                    f"已连接设备: {[d['serial'] for d in online_devices]}"
                )
            target = matching[0]
        elif len(online_devices) == 1:
            target = online_devices[0]
        else:
            device_list = "\n".join([f"  {d['serial']}" for d in online_devices])
            raise DeviceError(
                f"检测到多个设备，请通过 --serial 参数指定:\n{device_list}"
            )

        self.serial = target["serial"]
        logger.info(f"已连接设备: {self.serial}")

        # 获取屏幕分辨率
        self.screen_width, self.screen_height = self._acquire_device_resolution()
        logger.info(f"屏幕分辨率: {self.screen_width}x{self.screen_height}")

        # 切换到 ADB Keyboard 输入法 (跑测全程使用)
        self._switch_ime_to_adb()

        self._connected = True
        return True

    def _switch_ime_to_adb(self):
        """切换到 ADB Keyboard 输入法，并保存原输入法以便恢复"""
        try:
            result = self._run_adb("shell", "settings", "get", "secure", "default_input_method")
            self._original_ime = result.stdout.strip()
            adb_ime = "com.android.adbkeyboard/.AdbIME"
            if self._original_ime != adb_ime:
                self._run_adb("shell", "ime", "set", adb_ime)
                logger.info(f"已切换到 ADB Keyboard 输入法 (原: {self._original_ime})")
            else:
                logger.info("ADB Keyboard 输入法已为当前输入法")
        except Exception as e:
            logger.warning(f"切换 ADB Keyboard 输入法失败: {e}")

    def restore_ime(self):
        """恢复原始输入法"""
        if self._original_ime:
            try:
                adb_ime = "com.android.adbkeyboard/.AdbIME"
                if self._original_ime != adb_ime:
                    self._run_adb("shell", "ime", "set", self._original_ime)
                    logger.info(f"已恢复输入法: {self._original_ime}")
            except Exception as e:
                logger.warning(f"恢复输入法失败: {e}")

    def _run_adb(self, *args: str, timeout: int = 10) -> subprocess.CompletedProcess:
        """执行 ADB 命令

        Args:
            *args: ADB 命令参数
            timeout: 超时秒数

        Returns:
            subprocess.CompletedProcess
        """
        cmd = ["adb"]
        if self.serial:
            cmd.extend(["-s", self.serial])
        cmd.extend(args)

        logger.debug(f"执行 ADB: {' '.join(cmd)}")
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _acquire_device_resolution(self) -> Tuple[int, int]:
        """获取设备屏幕分辨率 (通过 wm size)

        Returns:
            (width, height)
        """
        result = self._run_adb("shell", "wm", "size")
        for line in result.stdout.strip().split("\n"):
            if "Physical size:" in line:
                size_str = line.split("Physical size:")[-1].strip()
                w, h = size_str.split("x")
                return int(w), int(h)
            if "Override size:" in line:
                size_str = line.split("Override size:")[-1].strip()
                w, h = size_str.split("x")
                return int(w), int(h)
        raise DeviceError(f"无法获取设备分辨率。wm size 输出: {result.stdout}")

    def _norm_to_pixel(self, x: float, y: float) -> Tuple[int, int]:
        """归一化坐标 [0, 1000] 转换为像素坐标

        Args:
            x: 归一化 x 坐标 [0-1000]
            y: 归一化 y 坐标 [0-1000]

        Returns:
            (pixel_x, pixel_y)
        """
        px = int(x / 1000.0 * self.screen_width)
        py = int(y / 1000.0 * self.screen_height)
        return px, py

    def screenshot(self) -> Image.Image:
        """截取手机屏幕

        通过先存设备临时文件再 pull 的方式，避免 Windows 上
        exec-out 管道可能破坏 PNG 二进制数据的问题。

        Returns:
            PIL.Image 对象
        """
        import tempfile

        remote_path = "/sdcard/.tmp_screencap.png"

        # 1. 在设备上截图并保存到临时路径
        result = self._run_adb("shell", "screencap", "-p", remote_path, timeout=10)
        if result.returncode != 0:
            raise DeviceError(f"设备截图失败: {result.stderr}")

        # 2. 拉取到本地临时文件
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            local_path = tmp.name

        result = self._run_adb("pull", remote_path, local_path, timeout=15)
        if result.returncode != 0:
            try:
                os.unlink(local_path)
            except Exception:
                pass
            raise DeviceError(f"截图拉取失败: {result.stderr}")

        # 3. 读取图片
        try:
            image = Image.open(local_path)
            image = image.convert("RGB")
        finally:
            try:
                os.unlink(local_path)
            except Exception:
                pass
            self._run_adb("shell", "rm", "-f", remote_path, timeout=5)

        return image

    def tap(self, x: int, y: int) -> bool:
        """点击指定坐标 (像素)

        Args:
            x: 像素 x 坐标
            y: 像素 y 坐标

        Returns:
            bool: 是否执行成功
        """
        result = self._run_adb("shell", "input", "tap", str(x), str(y))
        return result.returncode == 0

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300) -> bool:
        """滑动操作

        Args:
            x1, y1: 起始像素坐标
            x2, y2: 结束像素坐标
            duration: 滑动持续时间 (ms)

        Returns:
            bool: 是否执行成功
        """
        result = self._run_adb(
            "shell", "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(duration)
        )
        return result.returncode == 0

    def long_press(self, x: int, y: int, duration: int = 1000) -> bool:
        """长按操作 (模拟为同起点的 swipe)

        Args:
            x, y: 像素坐标
            duration: 长按持续时间 (ms)

        Returns:
            bool: 是否执行成功
        """
        return self.swipe(x, y, x, y, duration)

    def double_click(self, x: int, y: int) -> bool:
        """双击操作 (快速连续点击两次)

        Args:
            x, y: 像素坐标

        Returns:
            bool: 是否执行成功
        """
        if not self.tap(x, y):
            return False
        time.sleep(0.1)
        return self.tap(x, y)

    def input_text(self, text: str) -> bool:
        """输入文本

        纯 ASCII 使用 adb shell input text。
        包含非 ASCII 字符时自动切换到 ADB Keyboard 输入法发送。

        Args:
            text: 要输入的文本

        Returns:
            bool: 是否执行成功
        """
        if not text:
            return True

        # 主办方补充：用 TYPE("\n") 发送回车键（部分 App 如"得到"只能靠回车键提交搜索）
        if text == "\n":
            result = self._run_adb("shell", "input", "keyevent", "66")
            return result.returncode == 0

        has_non_ascii = any(ord(c) >= 128 for c in text)

        if has_non_ascii:
            return self._input_text_chinese_adbkeyboard(text)
        else:
            escaped = text.replace(" ", "%s").replace("&", "\\&")
            result = self._run_adb("shell", "input", "text", escaped)
            return result.returncode == 0

    def _input_text_chinese_adbkeyboard(self, text: str) -> bool:
        """通过 ADB Keyboard 广播输入中文 (需已在 connect() 中切换输入法)"""
        logger.debug(f"通过 ADB Keyboard 输入文本: {text[:50]}")

        result = self._run_adb(
            "shell", "am", "broadcast",
            "-a", "ADB_INPUT_TEXT",
            "--es", "msg", text
        )

        if result.returncode != 0:
            logger.warning(
                f"ADB Keyboard 输入失败，请确保手机上已安装 "
                f"ADB Keyboard 输入法。\n安装方法: adb install ADBKeyboard.apk"
            )
            return False
        return True

    def open_app(self, app_name: str) -> bool:
        """打开指定应用

        Args:
            app_name: 应用中文名称 (需在 APP_PACKAGE_MAP 中)

        Returns:
            bool: 是否执行成功
        """
        if app_name not in APP_PACKAGE_MAP:
            logger.warning(
                f"未知应用 '{app_name}'，不在 APP_PACKAGE_MAP 中。"
                f"尝试作为包名直接使用。"
            )
            package = app_name
        else:
            package = APP_PACKAGE_MAP[app_name]

        result = self._run_adb(
            "shell", "monkey",
            "-p", package,
            "-c", "android.intent.category.LAUNCHER",
            "1"
        )
        return result.returncode == 0

    def press_home(self) -> bool:
        """按 Home 键回到桌面

        Returns:
            bool: 是否执行成功
        """
        result = self._run_adb("shell", "input", "keyevent", "3")
        return result.returncode == 0

    def press_back(self) -> bool:
        """按返回键

        Returns:
            bool: 是否执行成功
        """
        result = self._run_adb("shell", "input", "keyevent", "4")
        return result.returncode == 0

    def execute(self, action: str, params: dict) -> Tuple[bool, Optional[str]]:
        """执行 Agent 产生的动作

        Args:
            action: 动作类型 (必须是 VALID_ACTIONS 中的常量)
            params: 动作参数 (归一化坐标)

        Returns:
            (success, error_message)
        """
        error = None

        try:
            if action == "CLICK":
                point = params.get("point", [0, 0])
                px, py = self._norm_to_pixel(point[0], point[1])
                self.tap(px, py)

            elif action == "SCROLL":
                sp = params.get("start_point", [0, 0])
                ep = params.get("end_point", [0, 0])
                sx, sy = self._norm_to_pixel(sp[0], sp[1])
                ex, ey = self._norm_to_pixel(ep[0], ep[1])
                self.swipe(sx, sy, ex, ey)

            elif action == "TYPE":
                text = params.get("text", "")
                self.input_text(text)

            elif action == "OPEN":
                app_name = params.get("app_name", "")
                self.open_app(app_name)

            elif action == "COMPLETE":
                pass

            elif action == "LONG_PRESS":
                point = params.get("point", [0, 0])
                duration = params.get("duration", 1000)
                px, py = self._norm_to_pixel(point[0], point[1])
                self.long_press(px, py, int(duration))

            elif action == "DOUBLE_CLICK":
                point = params.get("point", [0, 0])
                px, py = self._norm_to_pixel(point[0], point[1])
                self.double_click(px, py)

            elif action == "DRAG":
                sp = params.get("start_point", [0, 0])
                ep = params.get("end_point", [0, 0])
                sx, sy = self._norm_to_pixel(sp[0], sp[1])
                ex, ey = self._norm_to_pixel(ep[0], ep[1])
                self.swipe(sx, sy, ex, ey, duration=500)

            elif action == "BACK":
                self.press_back()

            elif action == "HOME":
                self.press_home()

            elif action == "WAIT":
                seconds = params.get("seconds", 2)
                time.sleep(float(seconds))

            else:
                error = f"未知动作类型: {action}"
                return False, error

            return True, None

        except Exception as e:
            error = f"{e}"
            logger.error(f"执行动作 {action} 失败: {e}")
            return False, error
