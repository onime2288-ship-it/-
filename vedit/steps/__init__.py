"""步骤集合。import 这个包就会把所有步骤注册进注册表。"""

from .base import Context, Step, available_steps, build_step, register
from . import cutting, effects  # noqa: F401  —— 触发 @register 副作用

__all__ = ["Context", "Step", "available_steps", "build_step", "register"]
