"""
Script 1: 批量跑测脚本

功能：
- 连接 ADB 设备，批量执行测试用例
- 生成每题的 data.json + summary.png
- 保存 run_metadata.json

用法:
    python run_tests.py
    python run_tests.py --serial <serial>
    python run_tests.py --resume
    python run_tests.py --tasks custom_tasks.json --output my_output/
"""

import os
import sys
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

from device_controller import DeviceController, DeviceError
from agent_base import BaseAgent


def setup_logging(output_dir: str) -> None:
    """配置日志输出"""
    log_dir = Path(output_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                str(log_dir / 'run.log'), encoding='utf-8'
            ),
        ]
    )


logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='GUI Agent 决赛批量跑测脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_tests.py                        # 自动检测设备
  python run_tests.py --serial emulator-5554 # 指定设备
  python run_tests.py --resume               # 断点续跑
  python run_tests.py --tasks my_tasks.json --output my_output/
        """
    )
    parser.add_argument(
        '--tasks', '-t',
        type=str,
        default='tasks/tasks.json',
        help='测试用例文件路径 (默认: tasks/tasks.json)'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        default='output',
        help='结果输出目录 (默认: output/)'
    )
    parser.add_argument(
        '--serial', '-s',
        type=str,
        default=None,
        help='ADB 设备序列号 (多设备时必须指定)'
    )
    parser.add_argument(
        '--resume', '-r',
        action='store_true',
        help='从上次中断处继续执行'
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    setup_logging(args.output)

    logger.info("=" * 60)
    logger.info("决赛 GUI Agent 批量跑测")
    logger.info(f"启动时间: {datetime.now().isoformat()}")
    logger.info("=" * 60)

    # 1. 检查 tasks.json
    tasks_file = args.tasks
    if not os.path.exists(tasks_file):
        logger.error(f"测试用例文件不存在: {tasks_file}")
        sys.exit(1)

    # 2. 连接设备
    logger.info("正在连接 ADB 设备...")
    device = DeviceController(serial=args.serial)

    try:
        device.connect()
    except DeviceError as e:
        logger.error(f"设备连接失败: {e}")
        sys.exit(1)

    logger.info(f"设备已就绪: {device.serial} ({device.screen_width}x{device.screen_height})")

    # 3. 加载 Agent
    logger.info("正在加载 Agent...")
    try:
        from agent import Agent
        agent = Agent()
    except ImportError as e:
        logger.error(f"加载 agent.py 失败: {e}")
        logger.error("请确保 agent.py 文件存在于当前目录")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Agent 初始化失败: {e}")
        sys.exit(1)

    logger.info("Agent 已加载")

    # 4. 运行测试
    from test_runner import TestRunner
    runner = TestRunner(agent=agent, device=device)

    logger.info(f"测试用例文件: {tasks_file}")
    logger.info(f"输出目录: {args.output}")
    logger.info(f"断点续跑: {'是' if args.resume else '否'}")
    logger.info("-" * 60)

    start_time = time.time()

    try:
        metadata = runner.run_all_tasks(
            tasks_file=tasks_file,
            output_dir=args.output,
            resume=args.resume
        )
    except Exception as e:
        logger.error(f"跑测过程异常: {e}", exc_info=True)
        sys.exit(1)

    elapsed = time.time() - start_time

    # 5. 输出摘要
    total = metadata.get('total_cases', 0)
    completed = metadata.get('completed_cases', 0)
    failed = metadata.get('failed_cases', 0)

    logger.info("=" * 60)
    logger.info("跑测完成!")
    logger.info(f"总用例数: {total}")
    logger.info(f"完成: {completed}")
    logger.info(f"失败: {failed}")
    logger.info(f"总耗时: {elapsed:.1f}s")
    logger.info(f"结果目录: {os.path.abspath(args.output)}")
    logger.info("=" * 60)

    if failed > 0:
        logger.info("\n失败用例:")
        for case in metadata.get('cases', []):
            if not case.get('success'):
                logger.info(f"  - {case['case_id']}: {case.get('error', 'unknown')}")

    logger.info("\n下一步: python generate_html.py  # 生成打分页面")


if __name__ == '__main__':
    main()
