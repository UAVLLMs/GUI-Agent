"""
可视化渲染模块 -- 决赛版本

与初赛相比：
- 无 ref.json 参考答案叠加，仅呈现 Agent 实际操作
- 统一中性色系 (蓝/青/灰/橙/黄/紫)
- 不做 PASS/FAIL 判定
- 生成步骤汇总网格图
"""

import os
import logging
from typing import Dict, List, Tuple, Any

import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from matplotlib.patches import Rectangle, Circle, FancyArrowPatch

plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

logger = logging.getLogger(__name__)

# 中性色系动作颜色
ACTION_COLORS = {
    'CLICK': 'cyan',
    'SCROLL': 'dodgerblue',
    'TYPE': 'gold',
    'OPEN': 'darkcyan',
    'COMPLETE': 'mediumblue',
    'LONG_PRESS': 'darkorange',
    'DOUBLE_CLICK': 'deepskyblue',
    'DRAG': 'mediumorchid',
    'BACK': 'gray',
    'HOME': 'gray',
    'WAIT': 'gray',
}


class Visualizer:
    """测试结果可视化器"""

    def __init__(self, max_cols: int = 5, fig_width: int = 30):
        self.max_cols = max_cols
        self.fig_width = fig_width

    @staticmethod
    def convert_normalized_to_pixels(params: dict, width: int, height: int) -> dict:
        """将归一化坐标 [0, 1000] 转换为实际像素坐标"""
        result = {}
        for key, value in params.items():
            if key in ('point', 'start_point', 'end_point'):
                if isinstance(value, list) and len(value) >= 2:
                    x_norm = float(value[0])
                    y_norm = float(value[1])
                    x_pixel = int(x_norm / 1000 * width)
                    y_pixel = int(y_norm / 1000 * height)
                    result[key] = [x_pixel, y_pixel]
                else:
                    result[key] = value
            else:
                result[key] = value
        return result

    def _plot_click(self, ax, point: list, color: str = 'cyan'):
        """绘制点击标记 - 青色圆圈 + 十字准心"""
        x, y = point[0], point[1]
        circle = Circle((x, y), radius=30, facecolor='none',
                        edgecolor=color, linewidth=4, alpha=0.9, zorder=10)
        ax.add_patch(circle)
        cross = 15
        ax.plot([x - cross, x + cross], [y, y], color=color, linewidth=3, zorder=11)
        ax.plot([x, x], [y - cross, y + cross], color=color, linewidth=3, zorder=11)

    def _plot_scroll(self, ax, start: list, end: list, color: str = 'dodgerblue'):
        """绘制滑动箭头"""
        ax.annotate('', xy=(end[0], end[1]), xytext=(start[0], start[1]),
                    arrowprops=dict(arrowstyle='->', color=color, lw=4, mutation_scale=20),
                    zorder=10)

    def _plot_type(self, ax, text: str, screen_w: int, screen_h: int,
                   color: str = 'gold'):
        """绘制文本输入浮层"""
        display = text[:25] + '...' if len(text) > 25 else text
        ax.text(screen_w / 2, screen_h * 0.9,
                f'TYPE: "{display}"',
                color='white', fontsize=14, ha='center', va='center',
                fontweight='bold',
                bbox=dict(facecolor=color, alpha=0.9, edgecolor='white',
                          boxstyle='round,pad=0.5'),
                zorder=10)

    def _plot_open(self, ax, app_name: str, screen_w: int, screen_h: int,
                   color: str = 'darkcyan'):
        """绘制打开应用标记"""
        ax.text(screen_w / 2, screen_h / 2,
                f'OPEN: {app_name}',
                color='white', fontsize=16, ha='center', va='center',
                fontweight='bold',
                bbox=dict(facecolor=color, alpha=0.9, edgecolor='white',
                          boxstyle='round,pad=0.5'),
                zorder=10)

    def _plot_complete(self, ax, screen_w: int, screen_h: int,
                       color: str = 'mediumblue'):
        """绘制完成标记"""
        ax.text(screen_w / 2, screen_h / 2,
                'COMPLETE',
                color='white', fontsize=18, ha='center', va='center',
                fontweight='bold',
                bbox=dict(facecolor=color, alpha=0.9, edgecolor='white',
                          boxstyle='round,pad=0.5'),
                zorder=10)

    def _plot_long_press(self, ax, point: list, duration: int,
                         color: str = 'darkorange'):
        """绘制长按标记 - 橙色圆圈 + 时钟标注"""
        x, y = point[0], point[1]
        circle = Circle((x, y), radius=35, facecolor='none',
                        edgecolor=color, linewidth=4, alpha=0.9,
                        linestyle='--', zorder=10)
        ax.add_patch(circle)
        circle2 = Circle((x, y), radius=30, facecolor='none',
                         edgecolor=color, linewidth=3, alpha=0.7,
                         zorder=10)
        ax.add_patch(circle2)
        ax.text(x, y - 60, f'{duration}ms', color=color,
                fontsize=10, ha='center', va='center', fontweight='bold',
                zorder=11)

    def _plot_double_click(self, ax, point: list, color: str = 'deepskyblue'):
        """绘制双击标记 - 双层重叠青色圆圈"""
        x, y = point[0], point[1]
        circle1 = Circle((x, y), radius=30, facecolor='none',
                         edgecolor=color, linewidth=3, alpha=0.9, zorder=10)
        ax.add_patch(circle1)
        circle2 = Circle((x, y), radius=38, facecolor='none',
                         edgecolor=color, linewidth=2, alpha=0.6, zorder=10)
        ax.add_patch(circle2)
        ax.text(x, y - 50, '2x', color=color,
                fontsize=10, ha='center', va='center', fontweight='bold',
                zorder=11)

    def _plot_drag(self, ax, start: list, end: list, color: str = 'mediumorchid'):
        """绘制拖拽箭头 + DRAG 标签"""
        ax.annotate('', xy=(end[0], end[1]), xytext=(start[0], start[1]),
                    arrowprops=dict(arrowstyle='->', color=color, lw=4, mutation_scale=20),
                    zorder=10)
        mid_x = (start[0] + end[0]) / 2
        mid_y = (start[1] + end[1]) / 2
        ax.text(mid_x, mid_y, 'DRAG', color='white', fontsize=10,
                ha='center', va='center', fontweight='bold',
                bbox=dict(facecolor=color, alpha=0.85, edgecolor='none',
                          boxstyle='round,pad=0.3'),
                zorder=12)

    def _plot_back(self, ax, screen_w: int, screen_h: int, color: str = 'gray'):
        """绘制返回标记"""
        ax.text(screen_w / 2, screen_h / 2,
                '\u2190 BACK', color='white', fontsize=16,
                ha='center', va='center', fontweight='bold',
                bbox=dict(facecolor=color, alpha=0.85, edgecolor='white',
                          boxstyle='round,pad=0.5'),
                zorder=10)

    def _plot_home(self, ax, screen_w: int, screen_h: int, color: str = 'gray'):
        """绘制 Home 标记"""
        ax.text(screen_w / 2, screen_h / 2,
                'HOME', color='white', fontsize=16,
                ha='center', va='center', fontweight='bold',
                bbox=dict(facecolor=color, alpha=0.85, edgecolor='white',
                          boxstyle='round,pad=0.5'),
                zorder=10)

    def _plot_wait(self, ax, seconds: float, screen_w: int, screen_h: int,
                   color: str = 'gray'):
        """绘制等待标记"""
        ax.text(screen_w / 2, screen_h / 2,
                f'WAIT: {seconds}s', color='white', fontsize=16,
                ha='center', va='center', fontweight='bold',
                bbox=dict(facecolor=color, alpha=0.85, edgecolor='white',
                          boxstyle='round,pad=0.5'),
                zorder=10)

    def _create_step_subplot(self, ax, step_record: Dict[str, Any],
                             default_shape: Tuple[int, int] = (1080, 1920)):
        """创建单步子图，在截图上叠加动作标注"""
        screenshot_path = step_record.get('screenshot', '')
        screen_w, screen_h = default_shape

        # 尝试从步骤数据中确定输出目录，拼接完整截图路径
        if screenshot_path and os.path.exists(screenshot_path):
            full_path = screenshot_path
        else:
            full_path = None
            logger.warning(f"截图文件不存在: {screenshot_path}")

        if full_path and os.path.exists(full_path):
            try:
                image = Image.open(full_path)
                img_array = np.array(image)
                ax.imshow(img_array)
                screen_w, screen_h = image.size
            except Exception as e:
                logger.warning(f"加载截图失败 {full_path}: {e}")
                ax.set_facecolor('#E8E8E8')
        else:
            ax.set_facecolor('#E8E8E8')
            ax.text(0.5, 0.5, 'No Screenshot',
                    transform=ax.transAxes, fontsize=14,
                    ha='center', va='center', color='gray')

        # 绘制动作标注
        action = step_record.get('action', '')
        params = step_record.get('params', {})
        is_error = bool(step_record.get('execution_error'))

        color = ACTION_COLORS.get(action, 'white')
        if is_error:
            color = 'red'

        params_pixel = self.convert_normalized_to_pixels(params, screen_w, screen_h)

        if action == 'CLICK':
            point = params_pixel.get('point', [0, 0])
            self._plot_click(ax, point, color)
        elif action == 'SCROLL':
            sp = params_pixel.get('start_point', [0, 0])
            ep = params_pixel.get('end_point', [0, 0])
            self._plot_scroll(ax, sp, ep, color)
        elif action == 'TYPE':
            text = params.get('text', '')
            self._plot_type(ax, text, screen_w, screen_h, color)
        elif action == 'OPEN':
            app_name = params.get('app_name', '')
            self._plot_open(ax, app_name, screen_w, screen_h, color)
        elif action == 'COMPLETE':
            self._plot_complete(ax, screen_w, screen_h, color)
        elif action == 'LONG_PRESS':
            point = params_pixel.get('point', [0, 0])
            duration = params.get('duration', 1000)
            self._plot_long_press(ax, point, duration, color)
        elif action == 'DOUBLE_CLICK':
            point = params_pixel.get('point', [0, 0])
            self._plot_double_click(ax, point, color)
        elif action == 'DRAG':
            sp = params_pixel.get('start_point', [0, 0])
            ep = params_pixel.get('end_point', [0, 0])
            self._plot_drag(ax, sp, ep, color)
        elif action == 'BACK':
            self._plot_back(ax, screen_w, screen_h, color)
        elif action == 'HOME':
            self._plot_home(ax, screen_w, screen_h, color)
        elif action == 'WAIT':
            seconds = params.get('seconds', 2)
            self._plot_wait(ax, seconds, screen_w, screen_h, color)

        # 设置标题
        step_num = step_record.get('step', 0)
        error_tag = ' [ERROR]' if is_error else ''
        title = f"Step {step_num}: {action}{error_tag}"
        ax.set_title(title, fontsize=12, fontweight='bold',
                     color='red' if is_error else 'black', pad=10)
        ax.axis('off')

    def generate_summary(self, steps_record: List[Dict[str, Any]],
                         output_dir: str,
                         instruction: str = '',
                         case_name: str = '') -> str:
        """生成所有步骤的汇总图 (网格布局)

        Args:
            steps_record: 步骤记录列表
            output_dir: 输出目录
            instruction: 用户指令
            case_name: 用例名称

        Returns:
            汇总图片保存路径
        """
        if not steps_record:
            logger.warning("没有步骤记录，跳过可视化")
            return ''

        os.makedirs(output_dir, exist_ok=True)

        n_steps = len(steps_record)
        n_cols = min(n_steps, self.max_cols)
        n_rows = (n_steps + n_cols - 1) // n_cols

        row_height = self.fig_width * 0.75
        fig_height = row_height * n_rows

        fig, axes = plt.subplots(n_rows, n_cols,
                                 figsize=(self.fig_width, fig_height))
        if n_steps == 1:
            axes = np.array([axes])
        axes = axes.flatten()

        for idx, step_record in enumerate(steps_record):
            ax = axes[idx]
            # 拼接截图完整路径
            full_screenshot = step_record.get('screenshot', '')
            if full_screenshot and not os.path.isabs(full_screenshot):
                full_screenshot = os.path.join(output_dir, full_screenshot)
                step_record = dict(step_record)
                step_record['screenshot'] = full_screenshot
            self._create_step_subplot(ax, step_record)

        for idx in range(n_steps, len(axes)):
            axes[idx].axis('off')

        if instruction:
            fig.suptitle(f"[{case_name}] {instruction}",
                         fontsize=16, fontweight='bold', y=0.98)

        plt.tight_layout(rect=[0, 0, 1, 0.95])

        summary_path = os.path.join(output_dir, 'summary.png')
        plt.savefig(summary_path, dpi=150, bbox_inches='tight', pad_inches=0.2)
        plt.close(fig)

        logger.info(f"汇总图已保存: {summary_path}")
        return summary_path
