"""素材库索引 —— 扫描、切镜头、算画面指标、挑推荐入点。

设计前提：全部指标用 ffmpeg 算，不依赖 OpenCV / numpy。
装环境的成本越低，这套东西越可能真的被用起来。

一个重要的经验（有实测数据支撑）：清晰度的绝对值不能跨镜头比较。
浅景深特写天生高频细节少，分数会低于轻微虚焦的大景深空镜。
所以「哪一段最实」只在镜头内部比，绝对值只用来砍极端废片。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import ffmpeg
from .errors import ConfigError, FFmpegFailed

VIDEO_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".m2ts", ".mxf", ".webm",
}

# 低于这个清晰度的帧，任何题材都算糊了。由对照实验定：
# 严重虚焦 0.56 / 明显虚焦 1.85 / 正常浅景深特写 2.7 以上。
ABSOLUTE_BLUR_FLOOR = 1.2

# 采样密度。2fps 足够定位到秒级，再高只是徒增解码时间。
SAMPLE_FPS = 2.0
SAMPLE_WIDTH = 320

_META = re.compile(r"lavfi\.signalstats\.(\w+)=([-\d.]+)")
_PTS = re.compile(r"pts_time:([\d.]+)")


@dataclass
class FrameStat:
    """一个采样帧的指标。"""

    time: float
    luma: float = 0.0        # 平均亮度 0~255
    low: float = 0.0         # 暗部 10% 分位
    high: float = 0.0        # 亮部 90% 分位
    sharp: float = 0.0       # 清晰度（拉普拉斯响应均值）
    motion: float = 0.0      # 与前一采样帧的差异

    @property
    def clipped_shadows(self) -> bool:
        return self.low <= 2

    @property
    def clipped_highlights(self) -> bool:
        return self.high >= 253

    @property
    def well_exposed(self) -> bool:
        return 40 <= self.luma <= 210 and not self.clipped_shadows


@dataclass
class ShotCandidate:
    """素材里的一个候选镜头。"""

    source: str
    start: float
    end: float
    best_start: float = 0.0     # 推荐入点：镜头内最实、最稳的一段
    best_length: float = 5.0
    sharp: float = 0.0          # 推荐段的平均清晰度
    motion: float = 0.0         # 推荐段的平均运动量
    luma: float = 0.0
    score: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def motion_kind(self) -> str:
        """把运动量翻译成人话。品牌片要的是「缓慢运镜」那一档。"""
        if self.motion < 1.0:
            return "静止"
        if self.motion < 4.0:
            return "缓慢运镜"
        if self.motion < 12.0:
            return "明显运动"
        return "剧烈晃动"


@dataclass
class LibraryEntry:
    """一个素材文件及其切出来的镜头。"""

    path: str
    size: int
    mtime: float
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    shots: list[ShotCandidate] = field(default_factory=list)
    error: str = ""

    @property
    def fingerprint(self) -> str:
        """用于增量扫描：文件没变就不重新分析。"""
        return f"{self.size}:{self.mtime:.0f}"


def find_videos(root: Path) -> list[Path]:
    """递归找出所有视频文件，跳过隐藏文件和 macOS 的资源分叉。"""
    if not root.exists():
        raise ConfigError(f"素材目录不存在: {root}")

    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # macOS 在外接盘上会生成 ._xxx 的伴随文件，ffprobe 读它们会报错
        if path.name.startswith("._") or path.name.startswith("."):
            continue
        if path.suffix.lower() in VIDEO_SUFFIXES:
            found.append(path)
    return found


def measure(path: Path, *, sample_fps: float = SAMPLE_FPS) -> list[FrameStat]:
    """一次解码，同时算出曝光、清晰度、运动量。

    三个指标各走一条滤镜分支，共用同一次解码 —— 对 4K 素材来说，
    解码才是瓶颈，分三次跑会慢三倍。
    """
    with tempfile.TemporaryDirectory(prefix="vedit-measure-") as tmpdir:
        tmp = Path(tmpdir)
        f_exp = tmp / "exposure.txt"
        f_shp = tmp / "sharp.txt"
        f_mot = tmp / "motion.txt"

        graph = (
            f"[0:v]fps={sample_fps},scale={SAMPLE_WIDTH}:-2,format=gray,split=3[a][b][c];"
            f"[a]signalstats,metadata=print:file={f_exp}[o1];"
            f"[b]convolution=0m='0 -1 0 -1 4 -1 0 -1 0':0rdiv=1:0bias=0,"
            f"signalstats,metadata=print:file={f_shp}[o2];"
            f"[c]tblend=all_mode=difference,signalstats,metadata=print:file={f_mot}[o3]"
        )

        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-i", str(path),
            "-filter_complex", graph,
            "-map", "[o1]", "-f", "null", "-",
            "-map", "[o2]", "-f", "null", "-",
            "-map", "[o3]", "-f", "null", "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if proc.returncode != 0:
            raise FFmpegFailed(
                f"分析失败 ({path.name}): {proc.stderr.strip()[-300:]}"
            )

        exposure = _parse_metadata(f_exp)
        sharp = _parse_metadata(f_shp)
        motion = _parse_metadata(f_mot)

    stats: list[FrameStat] = []
    for i, (time, values) in enumerate(exposure):
        stats.append(
            FrameStat(
                time=time,
                luma=values.get("YAVG", 0.0),
                low=values.get("YLOW", 0.0),
                high=values.get("YHIGH", 0.0),
                sharp=sharp[i][1].get("YAVG", 0.0) if i < len(sharp) else 0.0,
                # 帧差分支的第一帧没有前一帧可比，恒为 0，跳过它
                motion=motion[i][1].get("YAVG", 0.0) if 0 < i < len(motion) else 0.0,
            )
        )
    return stats


def _parse_metadata(path: Path) -> list[tuple[float, dict[str, float]]]:
    """解析 metadata=print 的输出。

    格式形如:
        frame:0    pts:0       pts_time:0
        lavfi.signalstats.YAVG=128.22
    """
    if not path.exists():
        return []

    entries: list[tuple[float, dict[str, float]]] = []
    current: dict[str, float] = {}
    time = 0.0

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if (m := _PTS.search(line)) is not None:
            if current:
                entries.append((time, current))
            time = float(m.group(1))
            current = {}
        elif (m := _META.search(line)) is not None:
            current[m.group(1)] = float(m.group(2))

    if current:
        entries.append((time, current))
    return entries


# ---- 分镜与入点推荐 -------------------------------------------------

# 短于这个长度的镜头没有使用价值，直接丢弃
MIN_SHOT = 1.5


def split_into_shots(
    path: Path, duration: float, *, threshold: float = 0.25
) -> list[tuple[float, float]]:
    """按场景切换把一个素材切成若干镜头。

    很多摄影师一条素材里就是一个镜头，那样这里只会返回一整段 —— 没问题。
    但一镜到底的长素材里往往含多次重新构图，切开才好挑。
    """
    try:
        cuts = ffmpeg.detect_scenes(path, threshold=threshold)
    except FFmpegFailed:
        cuts = []

    bounds = [0.0, *[c for c in cuts if MIN_SHOT < c < duration - MIN_SHOT], duration]
    shots = [
        (bounds[i], bounds[i + 1])
        for i in range(len(bounds) - 1)
        if bounds[i + 1] - bounds[i] >= MIN_SHOT
    ]
    return shots or [(0.0, duration)]


def pick_best_window(
    stats: list[FrameStat],
    start: float,
    end: float,
    *,
    length: float = 5.0,
) -> tuple[float, float, float, float]:
    """在一个镜头内找出最好的一段，返回 (入点, 实际长度, 清晰度, 运动量)。

    只在镜头内部做相对比较 —— 跨镜头比清晰度是没有意义的，
    浅景深特写永远比不过大景深空镜。
    """
    window = stats_in(stats, start, end)
    if not window:
        return start, min(length, end - start), 0.0, 0.0

    available = end - start
    take = min(length, available)
    if available <= length + 0.5:
        # 镜头本身就不长，整段拿走
        return start, take, _mean(s.sharp for s in window), _mean(s.motion for s in window)

    best: tuple[float, float] | None = None   # (评分, 入点)
    step = 0.5
    cursor = start
    while cursor + take <= end + 1e-6:
        chunk = stats_in(stats, cursor, cursor + take)
        if chunk:
            sharp = _mean(s.sharp for s in chunk)
            motion = _mean(s.motion for s in chunk)
            # 清晰优先；运动量越平稳越好，但完全静止不加分也不扣分
            steadiness = 1.0 / (1.0 + _stdev(s.motion for s in chunk))
            rating = sharp * (0.7 + 0.3 * steadiness)
            if best is None or rating > best[0]:
                best = (rating, cursor)
        cursor += step

    chosen = best[1] if best else start
    chunk = stats_in(stats, chosen, chosen + take)
    return (
        chosen,
        take,
        _mean(s.sharp for s in chunk),
        _mean(s.motion for s in chunk),
    )


def stats_in(stats: list[FrameStat], start: float, end: float) -> list[FrameStat]:
    return [s for s in stats if start <= s.time < end]


def _mean(values) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _stdev(values) -> float:
    items = list(values)
    if len(items) < 2:
        return 0.0
    avg = sum(items) / len(items)
    return (sum((v - avg) ** 2 for v in items) / len(items)) ** 0.5


def evaluate(candidate: ShotCandidate, stats: list[FrameStat]) -> ShotCandidate:
    """给候选镜头打分并标注问题。

    分数只用于排序，不用于自动淘汰 —— 淘汰由 flags 里的硬问题决定。
    低分不代表不能用，很多时候是题材本身的特点。
    """
    window = stats_in(stats, candidate.best_start, candidate.best_start + candidate.best_length)
    flags: list[str] = []
    score = 50.0

    # 清晰度：只有低于绝对地板才算硬伤
    if candidate.sharp < ABSOLUTE_BLUR_FLOOR:
        flags.append("虚焦")
        score -= 35
    else:
        score += min(20.0, (candidate.sharp - ABSOLUTE_BLUR_FLOOR) * 4)

    # 曝光
    if window:
        clipped_high = sum(1 for s in window if s.clipped_highlights) / len(window)
        clipped_low = sum(1 for s in window if s.clipped_shadows) / len(window)
        candidate.luma = _mean(s.luma for s in window)

        if clipped_high > 0.5:
            flags.append("过曝")
            score -= 20
        if clipped_low > 0.5:
            flags.append("死黑")
            score -= 15
        if candidate.luma < 35:
            flags.append("偏暗")
            score -= 10

    # 运动：品牌片偏爱缓慢运镜，剧烈晃动基本不可用
    kind = candidate.motion_kind
    if kind == "剧烈晃动":
        flags.append("晃动")
        score -= 30
    elif kind == "缓慢运镜":
        score += 12      # 这正是想要的
    elif kind == "静止":
        score += 4       # 能用，但略显呆板

    # 长度：太短的镜头在慢节奏片子里用不上
    if candidate.duration < 3.0:
        flags.append("过短")
        score -= 12
    elif candidate.duration >= 6.0:
        score += 6

    candidate.flags = flags
    candidate.score = round(max(0.0, min(100.0, score)), 1)
    return candidate


def analyze_file(path: Path, *, window: float = 5.0) -> LibraryEntry:
    """分析一个素材文件，切镜头并给每个镜头打分。"""
    info = ffmpeg.probe(path)
    stat = path.stat()
    entry = LibraryEntry(
        path=str(path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        duration=info.duration,
        width=info.width,
        height=info.height,
        fps=info.fps,
        has_audio=info.has_audio,
    )

    if not info.has_video or info.duration <= 0:
        entry.error = "没有视频流"
        return entry

    stats = measure(path)
    for start, end in split_into_shots(path, info.duration):
        best_start, length, sharp, motion = pick_best_window(
            stats, start, end, length=window
        )
        candidate = ShotCandidate(
            source=str(path),
            start=round(start, 2),
            end=round(end, 2),
            best_start=round(best_start, 2),
            best_length=round(length, 2),
            sharp=round(sharp, 3),
            motion=round(motion, 3),
        )
        entry.shots.append(evaluate(candidate, stats))

    return entry


# ---- 索引持久化 -----------------------------------------------------

@dataclass
class Library:
    """整个素材库的索引。"""

    root: str
    entries: list[LibraryEntry] = field(default_factory=list)

    @property
    def all_shots(self) -> list[ShotCandidate]:
        return [s for e in self.entries for s in e.shots]

    def usable_shots(self, *, min_score: float = 0.0) -> list[ShotCandidate]:
        """排除有硬伤的镜头，按分数从高到低排序。"""
        blocking = {"虚焦", "晃动", "过曝", "死黑"}
        return sorted(
            (
                s for s in self.all_shots
                if s.score >= min_score and not (blocking & set(s.flags))
            ),
            key=lambda s: -s.score,
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "root": self.root,
            "entries": [asdict(e) for e in self.entries],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "Library":
        if not path.exists():
            raise ConfigError(f"找不到索引文件: {path}\n先运行 vedit scan 生成它。")
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = []
        for raw in data.get("entries", []):
            shots = [ShotCandidate(**s) for s in raw.pop("shots", [])]
            entries.append(LibraryEntry(**raw, shots=shots))
        return cls(root=data.get("root", ""), entries=entries)

    def index_by_path(self) -> dict[str, LibraryEntry]:
        return {e.path: e for e in self.entries}
