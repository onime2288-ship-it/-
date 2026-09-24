"""SRT 字幕的读写与重新对轴。

去静音、变速之后，原始字幕的时间轴跟成片就对不上了。
这里按时间线把每条字幕重新映射到成片的时间轴上，
整条落在被剪掉区间里的字幕会被丢弃。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError
from .timeline import Timeline

_TIME_LINE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)

# 短于这个长度的字幕留着也读不清，直接丢
MIN_CUE_DURATION = 0.15


@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _to_srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse(path: str | Path) -> list[Cue]:
    """读取 SRT 文件。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"字幕文件不存在: {path}")

    # BOM 很常见（Windows 上的字幕工具几乎都会加），utf-8-sig 顺手吃掉
    content = path.read_text(encoding="utf-8-sig", errors="replace")

    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue

        time_idx = next(
            (i for i, ln in enumerate(lines) if _TIME_LINE.search(ln)), None
        )
        if time_idx is None:
            continue

        match = _TIME_LINE.search(lines[time_idx])
        start = _to_seconds(*match.group(1, 2, 3, 4))
        end = _to_seconds(*match.group(5, 6, 7, 8))
        text = "\n".join(lines[time_idx + 1:]).strip()
        if text:
            cues.append(Cue(index=len(cues) + 1, start=start, end=end, text=text))

    if not cues:
        raise ConfigError(f"{path} 里没有解析出任何字幕条目")
    return cues


def write(cues: list[Cue], path: str | Path) -> Path:
    """写出 SRT 文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    blocks = [
        f"{i}\n{_to_srt_time(c.start)} --> {_to_srt_time(c.end)}\n{c.text}"
        for i, c in enumerate(cues, start=1)
    ]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


def retime(cues: list[Cue], timeline: Timeline) -> list[Cue]:
    """按时间线把字幕重新映射到成片时间轴。

    规则：
    - 起止都被剪掉，且中间也没有保留内容 → 整条丢弃
    - 只有一端被剪掉 → 吸附到最近的保留边界
    - 映射后过短 → 丢弃
    """
    result: list[Cue] = []

    for cue in cues:
        mapped_start = timeline.map_time(cue.start)
        mapped_end = timeline.map_time(cue.end)

        if mapped_start is None and mapped_end is None:
            # 两端都在缝隙里：只有当这条字幕整个跨过了某段保留内容时才留下
            snapped_start = timeline.snap_time(cue.start)
            snapped_end = timeline.snap_time(cue.end)
            if snapped_end - snapped_start < MIN_CUE_DURATION:
                continue
            start, end = snapped_start, snapped_end
        else:
            start = mapped_start if mapped_start is not None else timeline.snap_time(cue.start)
            end = mapped_end if mapped_end is not None else timeline.snap_time(cue.end)

        if end - start < MIN_CUE_DURATION:
            continue

        result.append(Cue(index=len(result) + 1, start=start, end=end, text=cue.text))

    return result
