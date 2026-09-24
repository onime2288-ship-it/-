"""调色预设 —— 把可读的参数翻译成 ffmpeg 滤镜链。

刻意不做 LUT。LUT 是黑盒，改不动也看不懂；这里每个参数都有名字，
你想让绿色再灰一点，就把 saturation 往下调，所见即所得。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigError


@dataclass
class Grade:
    """一套调色参数。默认值全部是「不做任何改变」。"""

    # 基础
    saturation: float = 1.0      # <1 去饱和
    contrast: float = 1.0        # <1 降对比
    brightness: float = 0.0      # -1 ~ 1
    gamma: float = 1.0           # >1 提亮中间调

    # 影调
    lift: float = 0.0            # 抬黑，胶片感的关键。0.05~0.08 就很明显
    rolloff: float = 1.0         # 压高光，<1 让高光更柔

    # 色彩
    temperature: int | None = None   # 色温 K。低于 6500 偏暖，高于偏冷
    shadows: tuple[float, float, float] = (0.0, 0.0, 0.0)     # 阴影 RGB 偏移
    midtones: tuple[float, float, float] = (0.0, 0.0, 0.0)    # 中间调
    highlights: tuple[float, float, float] = (0.0, 0.0, 0.0)  # 高光

    # 收尾
    vignette: float = 0.0        # 暗角强度 0~1
    sharpen: float = 0.0         # 锐化 0~1.5

    def to_filters(self) -> list[str]:
        """生成 ffmpeg 滤镜列表，按正确的顺序。"""
        filters: list[str] = []

        # 1) 白平衡要最先做，后面的调整才建立在正确的色温上
        if self.temperature is not None:
            filters.append(f"colortemperature=temperature={self.temperature}")

        # 2) 影调曲线：抬黑 + 压高光
        if self.lift != 0.0 or self.rolloff != 1.0:
            low = max(0.0, min(0.5, self.lift))
            high = max(0.5, min(1.0, self.rolloff))
            filters.append(f"curves=all='0/{low:.4f} 0.5/0.5 1/{high:.4f}'")

        # 3) 三段色彩平衡
        if any(v != 0.0 for v in (*self.shadows, *self.midtones, *self.highlights)):
            sr, sg, sb = self.shadows
            mr, mg, mb = self.midtones
            hr, hg, hb = self.highlights
            filters.append(
                f"colorbalance=rs={sr}:gs={sg}:bs={sb}"
                f":rm={mr}:gm={mg}:bm={mb}"
                f":rh={hr}:gh={hg}:bh={hb}"
            )

        # 4) 基础的亮度/对比/饱和
        if (self.contrast, self.brightness, self.saturation, self.gamma) != (1.0, 0.0, 1.0, 1.0):
            filters.append(
                f"eq=contrast={self.contrast}:brightness={self.brightness}"
                f":saturation={self.saturation}:gamma={self.gamma}"
            )

        # 5) 暗角和锐化放最后
        if self.vignette > 0:
            # PI/5 是很轻的暗角，PI/2.5 已经相当重
            angle = 3.14159 / (5.0 - 2.5 * min(1.0, self.vignette))
            filters.append(f"vignette=angle={angle:.4f}")

        if self.sharpen > 0:
            filters.append(f"unsharp=5:5:{self.sharpen:.2f}:5:5:0")

        return filters

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, name: str = "") -> "Grade":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(
                f"调色预设 '{name}' 里有未知参数: {', '.join(sorted(unknown))}\n"
                f"可用: {', '.join(sorted(known))}"
            )

        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key in ("shadows", "midtones", "highlights"):
                if not isinstance(value, (list, tuple)) or len(value) != 3:
                    raise ConfigError(f"'{name}' 的 {key} 需要三个数字 [r, g, b]")
                kwargs[key] = tuple(float(v) for v in value)
            elif key == "temperature":
                kwargs[key] = int(value)
            else:
                kwargs[key] = float(value)
        return cls(**kwargs)


# 内置预设。参考七尚那一类酒店品牌片的观感做的起点，不是终点 ——
# 每个品牌都该在 brand.yml 里微调成自己的。
BUILTIN: dict[str, Grade] = {
    # 户外自然：低饱和、雾感、绿色往橄榄压、阴影抬起不压死黑
    "outdoor": Grade(
        saturation=0.90,
        contrast=0.94,
        brightness=0.05,
        gamma=1.10,                       # 提中间调，这是「通透」的主来源
        lift=0.075,                       # 抬黑，阴影要灰不要黑，雾感靠它
        rolloff=0.97,                     # 高光只微收，压太狠会发闷
        midtones=(0.008, 0.00, -0.020),   # 绿往橄榄走一点点，过了就发黄
        highlights=(0.012, 0.008, -0.012),
    ),
    # 室内窗光：冷白平衡、对比拉高、黑位压实
    "indoor": Grade(
        saturation=0.85,
        contrast=1.10,
        lift=0.010,
        temperature=7200,                 # 高于 6500 偏冷
        shadows=(-0.02, 0.00, 0.03),      # 阴影压一点蓝进去
        highlights=(0.00, 0.00, 0.01),
        sharpen=0.35,
    ),
    # 高调：背景过曝到白、明亮通透，适合茶席那种逆光场景
    "highkey": Grade(
        saturation=0.88,
        contrast=0.95,
        brightness=0.04,
        lift=0.045,
        gamma=1.06,
        highlights=(0.01, 0.01, 0.00),
    ),
    # 不做任何处理，素材本身就够好的时候用
    "none": Grade(),
}


def resolve(name: str, custom: dict[str, Grade] | None = None) -> Grade:
    """按名字取调色预设，brand.yml 里自定义的优先于内置。"""
    if custom and name in custom:
        return custom[name]
    if name in BUILTIN:
        return BUILTIN[name]
    available = sorted(set(BUILTIN) | set(custom or {}))
    raise ConfigError(f"未知的调色预设 '{name}'。可用: {', '.join(available)}")
