"""步骤基类与注册表。

一个 step 拿到 Context，修改它（改时间线，或往渲染选项里加滤镜），然后返回。
加新步骤只要写一个类 + 一个 @register 装饰器，recipe 里就能用了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

from ..errors import ConfigError
from ..ffmpeg import MediaInfo
from ..render import RenderOptions
from ..timeline import Timeline


@dataclass
class Context:
    """在整条流水线里传递的状态。"""

    timeline: Timeline
    info: MediaInfo
    options: RenderOptions
    verbose: bool = False
    notes: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.notes.append(message)
        if self.verbose:
            print(f"  {message}")


class Step:
    """所有步骤的基类。"""

    name: ClassVar[str] = ""

    def __init__(self, **params: Any) -> None:
        self.params = params
        self.configure(**params)

    def configure(self, **params: Any) -> None:
        """子类在这里校验并保存参数。"""

    def apply(self, ctx: Context) -> None:  # pragma: no cover - 抽象方法
        raise NotImplementedError

    # ---- 参数读取小工具（带类型校验和清晰报错）------------------------

    def _get(self, params: dict, key: str, default: Any, cast: Callable, label: str) -> Any:
        if key not in params or params[key] is None:
            return default
        try:
            return cast(params[key])
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"步骤 '{self.name}' 的参数 {key} 需要是{label}，收到 {params[key]!r}"
            ) from exc


_REGISTRY: dict[str, type[Step]] = {}


def register(cls: type[Step]) -> type[Step]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} 缺少 name 属性")
    _REGISTRY[cls.name] = cls
    return cls


def build_step(spec: dict[str, Any]) -> Step:
    """从 recipe 里的一条配置构造 Step 实例。"""
    if not isinstance(spec, dict):
        raise ConfigError(f"每个步骤必须是一个字典，收到: {spec!r}")

    params = dict(spec)
    kind = params.pop("type", None)
    if not kind:
        raise ConfigError(f"步骤缺少 type 字段: {spec!r}")

    cls = _REGISTRY.get(kind)
    if cls is None:
        available = ", ".join(sorted(_REGISTRY))
        raise ConfigError(f"未知的步骤类型 '{kind}'。可用: {available}")

    return cls(**params)


def available_steps() -> dict[str, type[Step]]:
    return dict(_REGISTRY)
