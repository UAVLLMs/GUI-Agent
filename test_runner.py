"""
测试执行引擎 -- 决赛版本

与初赛相比的核心变化：
- 去除 Checker / ref.json 验证逻辑
- 集成 DeviceController 进行手机操作
- 记录每步截图和动作数据
- 生成 data.json + summary.png 供后续打分使用
- 异常处理：ADB 断开重连、Agent 崩溃、超时、Token 超限
"""

import os
import json
import time
import logging
import traceback
from typing import Dict, Any, List, Tuple, Optional
from PIL import Image

from agent_base import (
    BaseAgent, AgentInput, AgentOutput,
    TokenLimitExceeded, VALID_ACTIONS, UsageInfo
)
from device_controller import DeviceController, DeviceError

logger = logging.getLogger(__name__)

# 全局配置
MAX_STEPS = 100
MAX_TOTAL_TOKENS = 2560000
STEP_TIMEOUT = 30
MAX_CONSECUTIVE_TIMEOUTS = 3


def _encode_image_for_history(image: Image.Image, image_format: str = "PNG") -> str:
    """将图片编码为 base64 URL"""
    import io
    import base64
    buffered = io.BytesIO()
    image.save(buffered, format=image_format)
    base64_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/{image_format.lower()};base64,{base64_str}"


def _format_params(params: Dict[str, Any]) -> str:
    """格式化参数字典为字符串"""
    if not params:
        return ""
    param_strs = []
    for key, value in params.items():
        if isinstance(value, list):
            param_strs.append(f"{key}={value}")
        elif isinstance(value, str):
            param_strs.append(f"{key}='{value}'")
        else:
            param_strs.append(f"{key}={value}")
    return ", ".join(param_strs)


class TestRunner:
    """测试执行器"""

    def __init__(self, agent: BaseAgent, device: DeviceController,
                 max_steps: int = None, step_timeout: int = None):
        self.agent = agent
        self.device = device
        self.max_steps = max_steps or MAX_STEPS
        self.step_timeout = step_timeout or STEP_TIMEOUT

        # Token 消耗监控
        self._total_tokens = 0
        self._max_total_tokens = MAX_TOTAL_TOKENS

    def _check_token_limit(self, usage: UsageInfo) -> None:
        """检查 token 使用量是否超过限制"""
        if usage:
            self._total_tokens += usage.total_tokens
            logger.info(
                f"Token usage: +{usage.total_tokens} "
                f"(input: {usage.input_tokens}, output: {usage.output_tokens}), "
                f"total: {self._total_tokens}/{self._max_total_tokens}"
            )
            if self._total_tokens > self._max_total_tokens:
                raise TokenLimitExceeded(self._total_tokens, self._max_total_tokens)

    @staticmethod
    def _validate_action(action: str, params: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """验证动作的合法性

        Args:
            action: 动作类型字符串
            params: 动作参数字典

        Returns:
            (is_valid, error_message)
        """
        if not action:
            return False, "动作类型为空"

        if action not in VALID_ACTIONS:
            return False, f"未知动作类型: {action}"

        if action == "CLICK":
            point = params.get("point")
            if not point or len(point) != 2:
                return False, "CLICK 缺少有效参数 point=[x, y]"

        elif action == "SCROLL":
            sp = params.get("start_point")
            ep = params.get("end_point")
            if not sp or not ep or len(sp) != 2 or len(ep) != 2:
                return False, "SCROLL 缺少有效参数 start_point/end_point"

        elif action == "TYPE":
            if "text" not in params:
                return False, "TYPE 缺少有效参数 text"

        elif action == "OPEN":
            if "app_name" not in params:
                return False, "OPEN 缺少有效参数 app_name"

        elif action == "LONG_PRESS":
            point = params.get("point")
            if not point or len(point) != 2:
                return False, "LONG_PRESS 缺少有效参数 point=[x, y]"

        elif action == "DOUBLE_CLICK":
            point = params.get("point")
            if not point or len(point) != 2:
                return False, "DOUBLE_CLICK 缺少有效参数 point=[x, y]"

        elif action == "DRAG":
            sp = params.get("start_point")
            ep = params.get("end_point")
            if not sp or not ep or len(sp) != 2 or len(ep) != 2:
                return False, "DRAG 缺少有效参数 start_point/end_point"

        elif action == "WAIT":
            if "seconds" not in params:
                return False, "WAIT 缺少有效参数 seconds"

        return True, None

    def run_task(self, case_config: Dict[str, Any], output_dir: str) -> Dict[str, Any]:
        """执行单个测试用例

        Args:
            case_config: 包含 id, instruction, max_steps 的字典
            output_dir: 该用例的输出目录 (如 output/jingdong_01/)

        Returns:
            执行结果字典 (即 data.json 内容)
        """
        case_id = case_config["id"]
        instruction = case_config["instruction"]
        case_max_steps = case_config.get("max_steps", self.max_steps)

        logger.info(f"===== 开始执行用例: {case_id} =====")
        logger.info(f"指令: {instruction}")

        # 重置 Agent 状态
        try:
            self.agent.reset()
        except Exception as e:
            logger.warning(f"Agent reset 失败: {e}")

        # 回到桌面并等待稳定
        self.device.press_home()
        time.sleep(2)

        steps_record = []
        errors = []
        exit_reason = ""
        total_steps = 0
        consecutive_timeouts = 0

        try:
            # 执行循环
            for step_count in range(1, case_max_steps + 1):
                logger.info(f"--- Step {step_count} ---")

                # 1. 截图
                try:
                    screenshot = self.device.screenshot()
                except (DeviceError, Exception) as e:
                    exit_reason = "device_error"
                    errors.append(f"Step {step_count}: 截图失败 - {e}")
                    logger.error(f"截图失败: {e}")
                    break

                # 2. 调用 Agent
                agent_input = AgentInput(
                    instruction=instruction,
                    current_image=screenshot,
                    step_count=step_count,
                )

                agent_output = None
                step_error = None

                try:
                    agent_output = self.agent.act(agent_input)
                    logger.info(
                        f"Agent Output: action={agent_output.action}, "
                        f"params={agent_output.parameters}"
                    )
                except TokenLimitExceeded as e:
                    exit_reason = "token_exceeded"
                    errors.append(f"Token 超限: {e}")
                    logger.error(f"Token 超限: {e}")
                    raise
                except Exception as e:
                    step_error = f"Agent 执行异常: {e}\n{traceback.format_exc()}"
                    errors.append(f"Step {step_count}: {step_error}")
                    logger.error(step_error)
                    exit_reason = "agent_error"
                    # 保存已执行步骤，终止当前 case
                    steps_record.append({
                        "step": step_count,
                        "action": "",
                        "params": {},
                        "raw_output": f"Error: {e}",
                        "screenshot": "",
                        "execution_error": step_error,
                    })
                    break

                if agent_output is None:
                    step_error = "Agent 返回 None"
                    errors.append(f"Step {step_count}: {step_error}")
                    exit_reason = "agent_error"
                    break

                # 3. 检查 token
                if agent_output.usage:
                    try:
                        self._check_token_limit(agent_output.usage)
                    except TokenLimitExceeded:
                        exit_reason = "token_exceeded"
                        raise

                # 4. 保存截图
                screenshot_filename = f"step_{step_count:02d}.png"
                screenshot_path = os.path.join(output_dir, screenshot_filename)
                try:
                    screenshot.save(screenshot_path, "PNG")
                except Exception as e:
                    logger.error(f"保存截图失败: {e}")
                    screenshot_path = ""

                # 5. 记录步骤数据
                step_record = {
                    "step": step_count,
                    "action": agent_output.action,
                    "params": agent_output.parameters,
                    "raw_output": agent_output.raw_output,
                    "screenshot": screenshot_filename,
                    "execution_error": None,
                }
                steps_record.append(step_record)
                total_steps = step_count

                # 6. 验证动作合法性
                is_valid, validation_error = self._validate_action(
                    agent_output.action, agent_output.parameters
                )
                if not is_valid:
                    step_record["execution_error"] = f"动作非法: {validation_error}"
                    errors.append(f"Step {step_count}: {validation_error}")
                    exit_reason = "invalid_action"
                    break

                # 7. 如果是 COMPLETE，结束循环
                if agent_output.action == "COMPLETE":
                    logger.info("Agent 返回 COMPLETE，任务完成")
                    exit_reason = "agent_completed"
                    break

                # 8. 执行动作
                success, exec_error = self.device.execute(
                    agent_output.action, agent_output.parameters
                )
                if not success:
                    step_record["execution_error"] = exec_error
                    errors.append(f"Step {step_count}: 动作执行失败 - {exec_error}")
                    logger.error(f"动作执行失败: {exec_error}")

                # 9. 动作后等待 (让 UI 稳定)
                time.sleep(1)

            else:
                exit_reason = exit_reason or "max_steps_reached"
                logger.info(f"达到最大步数限制 ({case_max_steps})")

        except TokenLimitExceeded:
            exit_reason = exit_reason or "token_exceeded"
        except DeviceError as e:
            exit_reason = "device_error"
            errors.append(f"设备异常: {e}")
            logger.error(f"设备异常: {e}")
        except Exception as e:
            exit_reason = exit_reason or "unknown_error"
            errors.append(f"未捕获异常: {e}\n{traceback.format_exc()}")
            logger.error(f"未捕获异常: {e}")

        logger.info(f"用例 {case_id} 执行结束: exit_reason={exit_reason}, steps={total_steps}")

        # 保存 data.json
        data = {
            "case_id": case_id,
            "instruction": instruction,
            "max_steps": case_max_steps,
            "device": {
                "width": self.device.screen_width,
                "height": self.device.screen_height,
            },
            "execution": {
                "total_steps": total_steps,
                "exit_reason": exit_reason,
                "errors": errors,
            },
            "steps": steps_record,
            "summary_visualization": "summary.png",
        }

        data_path = os.path.join(output_dir, "data.json")
        os.makedirs(output_dir, exist_ok=True)
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, ensure_ascii=False, indent=2, fp=f)
        logger.info(f"data.json 已保存: {data_path}")

        # 生成 summary.png
        summary_path = ""
        if steps_record:
            try:
                from utils.visualize import Visualizer
                visualizer = Visualizer()
                summary_path = visualizer.generate_summary(
                    steps_record=steps_record,
                    output_dir=output_dir,
                    instruction=instruction,
                    case_name=case_id,
                )
                logger.info(f"summary.png 已生成: {summary_path}")
            except Exception as e:
                logger.error(f"可视化生成失败: {e}")

        return data

    def run_all_tasks(self, tasks_file: str, output_dir: str,
                      resume: bool = False) -> Dict[str, Any]:
        """运行所有测试用例

        Args:
            tasks_file: tasks.json 文件路径
            output_dir: 输出根目录 (如 output/)
            resume: 是否断点续跑

        Returns:
            运行元数据字典
        """
        # 加载任务列表
        with open(tasks_file, "r", encoding="utf-8") as f:
            tasks = json.load(f)

        os.makedirs(output_dir, exist_ok=True)
        metadata_path = os.path.join(output_dir, "run_metadata.json")

        # 处理断点续跑
        completed_cases = set()
        if resume and os.path.exists(metadata_path):
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    prev_metadata = json.load(f)
                for c in prev_metadata.get("cases", []):
                    case_dir = os.path.join(output_dir, c["case_id"])
                    data_file = os.path.join(case_dir, "data.json")
                    if c.get("success") and os.path.exists(data_file):
                        try:
                            with open(data_file, "r", encoding="utf-8") as df:
                                case_data = json.load(df)
                            if "exit_reason" in case_data.get("execution", {}):
                                completed_cases.add(c["case_id"])
                                logger.info(f"[断点续跑] 跳过已完成用例: {c['case_id']}")
                        except Exception:
                            logger.warning(f"[断点续跑] 用例 {c['case_id']} 数据不完整，将重新执行")
            except Exception as e:
                logger.warning(f"[断点续跑] 读取历史元数据失败: {e}")

        if not resume:
            from datetime import datetime
            run_metadata = {
                "runner_version": "2.0",
                "device": {
                    "serial": self.device.serial,
                    "width": self.device.screen_width,
                    "height": self.device.screen_height,
                },
                "executed_at": datetime.now().isoformat(),
                "total_cases": len(tasks),
                "completed_cases": 0,
                "failed_cases": 0,
                "cases": []
            }

        cases_results = []
        total_cases = len(tasks)
        completed_count = 0
        failed_count = 0

        for task in tasks:
            case_id = task["id"]

            # 断点续跑跳过已完成
            if resume and case_id in completed_cases:
                completed_count += 1
                # 从历史元数据中获取用例信息
                with open(metadata_path, "r", encoding="utf-8") as f:
                    prev_meta = json.load(f)
                for c in prev_meta["cases"]:
                    if c["case_id"] == case_id:
                        cases_results.append(c)
                        break
                continue

            case_dir = os.path.join(output_dir, case_id)
            os.makedirs(case_dir, exist_ok=True)

            try:
                self.run_task(task, case_dir)
                cases_results.append({
                    "case_id": case_id,
                    "success": True,
                })
                completed_count += 1
            except Exception as e:
                logger.error(f"用例 {case_id} 执行异常: {e}")
                cases_results.append({
                    "case_id": case_id,
                    "success": False,
                    "error": str(e),
                })
                failed_count += 1

            # 每执行完一个用例，更新元数据
            from datetime import datetime
            run_metadata = {
                "runner_version": "2.0",
                "device": {
                    "serial": self.device.serial,
                    "width": self.device.screen_width,
                    "height": self.device.screen_height,
                },
                "executed_at": datetime.now().isoformat(),
                "total_cases": total_cases,
                "completed_cases": completed_count,
                "failed_cases": failed_count,
                "cases": cases_results,
            }
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(run_metadata, ensure_ascii=False, indent=2, fp=f)
            logger.info(f"进度已保存: {metadata_path}")

        logger.info("=" * 50)
        logger.info(f"全部用例执行完毕: {completed_count}/{total_cases} 完成, {failed_count} 失败")
        logger.info("=" * 50)

        # 恢复原始输入法
        try:
            self.device.restore_ime()
        except Exception as e:
            logger.warning(f"恢复输入法失败: {e}")

        return run_metadata
