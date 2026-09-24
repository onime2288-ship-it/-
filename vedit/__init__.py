"""vedit —— 配置驱动的视频剪辑流水线。

核心思路：分析步骤只修改「时间线」（保留哪些片段），
真正的编码只在最后发生一次。所以叠多少剪辑步骤，都不会二次损失画质。
"""

__version__ = "0.1.0"

from .errors import VeditError
from .timeline import Segment, Timeline

__all__ = ["Segment", "Timeline", "VeditError", "__version__"]
