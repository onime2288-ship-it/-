"""镜头表模型 —— 品牌片工作流的核心数据结构。

跟 timeline.py 的区别：
- Timeline 处理「一个素材，剪掉哪些段」，适合口播
- Sequence 处理「多个镜头按顺序排列，之间溶解」，适合品牌片

两者共存，各管各的场景。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError


@dataclass
class Caption:
    """压在画面上的文案。

    中英上下排是这类片子的标配：中文在上（宋体），英文在下（无衬线大写）。
    """

    zh: list[str] = field(default_factory=list)
    en: list[str] = field(default_factory=list)
    style: str = "caption"        # 引用 brand.yml 里的样式名
    position: str | None = None   # 覆盖样式里的位置
    fade: float = 0.8             # 文字自身的淡入淡出时长
    delay: float = 0.5            # 镜头开始多久后文字出现

    @property
    def is_empty(self) -> bool:
        return not self.zh and not self.en


@dataclass
class Shot:
    """一个镜头。"""

    source: Path
    start: float = 0.0            # 在源素材里的入点
    duration: float = 5.0         # 在成片里占的时长（已计入变速）
    speed: float = 1.0
    grade: str | None = None      # 引用 brand.yml 里的调色预设名
    frame: str = "full"           # full=满画幅  letterbox=上下留黑边
    caption: Caption | None = None
    transition: float | None = None   # 进入这个镜头的溶解时长，None=用品牌默认
    note: str = ""                # 给人看的备注，不影响渲染

    def __post_init__(self) -> None:
        if self.duration <= 0:
            raise ConfigError(f"镜头时长必须大于 0: {self.source}")
        if self.speed <= 0:
            raise ConfigError(f"镜头速度必须大于 0: {self.source}")
        if self.start < 0:
            raise ConfigError(f"镜头入点不能为负: {self.source}")
        if self.frame not in ("full", "letterbox"):
            raise ConfigError(
                f"frame 只能是 full 或 letterbox，收到 {self.frame!r}"
            )

    @property
    def source_duration(self) -> float:
        """需要从源素材里取多长。变速 2 倍时，5 秒成片要取 10 秒素材。"""
        return self.duration * self.speed

    @property
    def end(self) -> float:
        return self.start + self.source_duration


@dataclass
class Sequence:
    """整条片子的镜头表。"""

    shots: list[Shot]
    default_transition: float = 1.0

    def __post_init__(self) -> None:
        if not self.shots:
            raise ConfigError("镜头表是空的，至少需要一个镜头")

    def transition_for(self, index: int) -> float:
        """进入第 index 个镜头的溶解时长。第一个镜头没有转场。"""
        if index == 0:
            return 0.0
        shot = self.shots[index]
        value = self.default_transition if shot.transition is None else shot.transition
        # 溶解不能长过相邻任一镜头，否则 xfade 的偏移会算成负数
        return max(0.0, min(value, self.shots[index - 1].duration, shot.duration))

    @property
    def duration(self) -> float:
        """成片总时长。溶解是重叠的，所以要把重叠部分减掉。"""
        total = sum(s.duration for s in self.shots)
        overlap = sum(self.transition_for(i) for i in range(1, len(self.shots)))
        return total - overlap

    def offset_for(self, index: int) -> float:
        """第 index 个镜头的 xfade 起始偏移。

        xfade 的 offset 是「溶解开始的时刻」，等于前面所有内容的累计时长
        减去本次溶解的时长。
        """
        if index == 0:
            return 0.0
        accumulated = self.shots[0].duration
        for i in range(1, index):
            accumulated += self.shots[i].duration - self.transition_for(i)
        return accumulated - self.transition_for(index)

    def validate_sources(self) -> None:
        """渲染前先确认素材都在，避免跑到一半才报错。"""
        missing = [s.source for s in self.shots if not s.source.exists()]
        if missing:
            listed = "\n  ".join(str(p) for p in missing)
            raise ConfigError(f"找不到这些素材:\n  {listed}")

    def summary(self) -> str:
        lines = [f"{len(self.shots)} 个镜头, 成片 {self.duration:.1f}s"]
        for i, shot in enumerate(self.shots):
            trans = self.transition_for(i)
            mark = f" ←溶解{trans:.1f}s" if trans else ""
            cap = ""
            if shot.caption and not shot.caption.is_empty:
                first = (shot.caption.zh or shot.caption.en)[0]
                cap = f'  「{first}」'
            grade = f" [{shot.grade}]" if shot.grade else ""
            frame = " ▭" if shot.frame == "letterbox" else ""
            lines.append(
                f"  {i + 1:2d}. {shot.source.name}  "
                f"{shot.start:.1f}s +{shot.duration:.1f}s{grade}{frame}{mark}{cap}"
            )
        return "\n".join(lines)
