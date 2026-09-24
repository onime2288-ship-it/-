"""时间线模型 —— 整个工作流的核心。

关键设计：分析类步骤（去静音、场景切分、手动裁切）**不做任何转码**，
它们只修改一份「保留哪些片段」的清单。真正的编码只在最后 render 时发生一次。

好处很直接：叠加十个剪辑步骤的耗时和画质损失，跟只剪一刀是一样的。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .errors import EmptyTimeline

# 短于这个长度的片段直接丢掉：ffmpeg 的 trim 在亚帧级别上不可靠，
# 而且这种碎片在成片里只会表现为一声爆音。
MIN_SEGMENT = 0.04


@dataclass(frozen=True)
class Segment:
    """源素材上的一段。start/end 是**源文件**里的时间，单位秒。"""

    start: float
    end: float
    speed: float = 1.0

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"片段结束时间早于开始时间: {self.start} -> {self.end}")
        if self.speed <= 0:
            raise ValueError(f"速度必须为正数，收到 {self.speed}")

    @property
    def source_duration(self) -> float:
        """在源素材上占的时长。"""
        return self.end - self.start

    @property
    def output_duration(self) -> float:
        """变速后，在成片里占的时长。"""
        return self.source_duration / self.speed


@dataclass
class Timeline:
    """一个输入文件 + 它的保留片段清单。"""

    source: str
    duration: float
    segments: list[Segment]

    @classmethod
    def full(cls, source: str, duration: float) -> "Timeline":
        """整段素材，未做任何裁切。"""
        return cls(source=source, duration=duration, segments=[Segment(0.0, duration)])

    @property
    def output_duration(self) -> float:
        return sum(s.output_duration for s in self.segments)

    @property
    def source_duration(self) -> float:
        return sum(s.source_duration for s in self.segments)

    @property
    def is_empty(self) -> bool:
        return not self.segments

    def require_content(self, context: str) -> None:
        if self.is_empty:
            raise EmptyTimeline(
                f"{context} 之后时间线上没有任何内容了。"
                "检查一下参数是不是太激进（比如去静音的阈值设得太高）。"
            )

    # ---- 片段运算 ----------------------------------------------------

    def remove(self, spans: list[tuple[float, float]]) -> "Timeline":
        """从时间线上挖掉若干区间，返回新的 Timeline。"""
        if not spans:
            return self

        kept: list[Segment] = []
        for seg in self.segments:
            pieces = [(seg.start, seg.end)]
            for cut_start, cut_end in spans:
                nxt: list[tuple[float, float]] = []
                for piece_start, piece_end in pieces:
                    # 完全不重叠
                    if cut_end <= piece_start or cut_start >= piece_end:
                        nxt.append((piece_start, piece_end))
                        continue
                    # 左侧残留
                    if cut_start > piece_start:
                        nxt.append((piece_start, cut_start))
                    # 右侧残留
                    if cut_end < piece_end:
                        nxt.append((cut_end, piece_end))
                pieces = nxt
            kept.extend(
                replace(seg, start=ps, end=pe)
                for ps, pe in pieces
                if pe - ps >= MIN_SEGMENT
            )

        return Timeline(source=self.source, duration=self.duration, segments=kept)

    def keep_only(self, spans: list[tuple[float, float]]) -> "Timeline":
        """只保留若干区间，其余全部剪掉。"""
        normalized = normalize_spans(spans)
        inverted = invert_spans(normalized, self.duration)
        return self.remove(inverted)

    def with_speed(self, factor: float) -> "Timeline":
        """给所有片段叠加一个速度系数。"""
        return Timeline(
            source=self.source,
            duration=self.duration,
            segments=[replace(s, speed=s.speed * factor) for s in self.segments],
        )

    def pad(self, seconds: float) -> "Timeline":
        """把每个片段前后各延长 seconds 秒，然后合并重叠的部分。

        去静音之后一定要做这一步，否则每句话的头尾会被削掉，听起来很急促。
        """
        if seconds <= 0:
            return self

        widened = [
            (max(0.0, s.start - seconds), min(self.duration, s.end + seconds))
            for s in self.segments
        ]
        # pad 会让速度信息失去意义（合并后的片段可能来自不同速度），
        # 所以这一步只在变速之前调用。
        merged = normalize_spans(widened)
        return Timeline(
            source=self.source,
            duration=self.duration,
            segments=[Segment(a, b) for a, b in merged],
        )

    def drop_shorter_than(self, seconds: float) -> "Timeline":
        """丢掉过短的碎片段。"""
        return Timeline(
            source=self.source,
            duration=self.duration,
            segments=[s for s in self.segments if s.source_duration >= seconds],
        )

    def summary(self) -> str:
        saved = self.duration - self.output_duration
        pct = (saved / self.duration * 100) if self.duration else 0.0
        return (
            f"{len(self.segments)} 段 | "
            f"{_fmt(self.duration)} → {_fmt(self.output_duration)} "
            f"(省掉 {_fmt(saved)}, {pct:.0f}%)"
        )



    # ---- 时间映射（字幕对轴用）----------------------------------------

    def map_time(self, source_time: float) -> float | None:
        """源素材上的时间点 → 成片里的时间点。

        如果这个时刻落在被剪掉的区间里，返回 None。
        """
        elapsed = 0.0
        for seg in self.segments:
            if source_time < seg.start:
                return None  # 落在两段之间的缝隙里，已被剪掉
            if source_time <= seg.end:
                return elapsed + (source_time - seg.start) / seg.speed
            elapsed += seg.output_duration
        return None

    def snap_time(self, source_time: float) -> float:
        """跟 map_time 一样，但落在缝隙里时吸附到最近的保留边界。

        字幕对轴用这个：一句话的开头即使压在静音上，也要有个落点。
        """
        elapsed = 0.0
        for seg in self.segments:
            if source_time < seg.start:
                return elapsed  # 吸附到下一段的开头
            if source_time <= seg.end:
                return elapsed + (source_time - seg.start) / seg.speed
            elapsed += seg.output_duration
        return elapsed  # 超出末尾，吸附到片尾

    def to_dict(self) -> dict:
        """序列化，供字幕对轴等后续工具读取。"""
        return {
            "source": self.source,
            "duration": self.duration,
            "segments": [
                {"start": s.start, "end": s.end, "speed": s.speed}
                for s in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Timeline":
        return cls(
            source=data["source"],
            duration=float(data["duration"]),
            segments=[
                Segment(float(s["start"]), float(s["end"]), float(s.get("speed", 1.0)))
                for s in data["segments"]
            ],
        )


def normalize_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """排序 + 合并重叠/相邻的区间。"""
    cleaned = sorted((a, b) for a, b in spans if b > a)
    if not cleaned:
        return []

    merged = [cleaned[0]]
    for start, end in cleaned[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def invert_spans(
    spans: list[tuple[float, float]], duration: float
) -> list[tuple[float, float]]:
    """求区间集合在 [0, duration] 上的补集。"""
    merged = normalize_spans(spans)
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        gaps.append((cursor, duration))
    return gaps


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:04.1f}"
    return f"{minutes}:{secs:04.1f}"
