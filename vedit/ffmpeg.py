"""ffmpeg / ffprobe 的薄封装。

这里只负责「把命令跑起来」和「把探测结果解析成 Python 对象」，
不含任何剪辑逻辑 —— 剪辑逻辑在 timeline.py 和 steps/ 里。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import FFmpegNotFound, FFmpegFailed, ProbeFailed


def _binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise FFmpegNotFound(
            f"找不到 {name}。请先安装 ffmpeg："
            "\n  macOS:  brew install ffmpeg"
            "\n  Ubuntu: sudo apt-get install ffmpeg"
            "\n  Windows: winget install Gyan.FFmpeg"
        )
    return path


@dataclass(frozen=True)
class MediaInfo:
    """ffprobe 结果里我们真正会用到的那几项。"""

    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    has_video: bool
    sample_rate: int | None = None

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width


def _parse_fps(rate: str) -> float:
    """ffprobe 的 r_frame_rate 形如 "30000/1001"。"""
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(rate)
    except ValueError:
        return 0.0


def probe(path: str | Path) -> MediaInfo:
    """读取媒体文件的基本信息。"""
    path = Path(path)
    if not path.exists():
        raise ProbeFailed(f"文件不存在: {path}")

    cmd = [
        _binary("ffprobe"),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProbeFailed(f"ffprobe 读取失败 ({path}): {proc.stderr.strip()}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeFailed(f"ffprobe 输出无法解析 ({path}): {exc}") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    fmt_dur = data.get("format", {}).get("duration")
    if fmt_dur:
        duration = float(fmt_dur)
    elif video and video.get("duration"):
        duration = float(video["duration"])

    return MediaInfo(
        path=path,
        duration=duration,
        width=int(video["width"]) if video and video.get("width") else 0,
        height=int(video["height"]) if video and video.get("height") else 0,
        fps=_parse_fps(video.get("r_frame_rate", "0/1")) if video else 0.0,
        has_audio=audio is not None,
        has_video=video is not None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
    )


def run(args: list[str], *, dry_run: bool = False, quiet: bool = True) -> None:
    """执行一条 ffmpeg 命令。

    dry_run 时只打印不执行 —— 调参阶段很有用，可以把命令拷出来手动跑。
    """
    cmd = [_binary("ffmpeg"), "-hide_banner", "-nostdin", "-y", *args]

    if dry_run:
        print("[dry-run]", " ".join(_quote(a) for a in cmd))
        return

    proc = subprocess.run(
        cmd,
        capture_output=quiet,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        tail = ""
        if quiet and proc.stderr:
            tail = "\n" + "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegFailed(f"ffmpeg 执行失败 (exit {proc.returncode}){tail}")


_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silence(
    path: str | Path,
    *,
    noise: str = "-32dB",
    min_duration: float = 0.5,
) -> list[tuple[float, float]]:
    """用 silencedetect 滤镜找出所有静音区间，返回 [(start, end), ...]。"""
    cmd = [
        _binary("ffmpeg"),
        "-hide_banner", "-nostdin",
        "-i", str(path),
        "-af", f"silencedetect=noise={noise}:d={min_duration}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise FFmpegFailed(f"静音检测失败: {proc.stderr.strip()[-500:]}")

    spans: list[tuple[float, float]] = []
    pending: float | None = None
    for line in proc.stderr.splitlines():
        if (m := _SILENCE_START.search(line)) is not None:
            pending = max(0.0, float(m.group(1)))
        elif (m := _SILENCE_END.search(line)) is not None:
            end = float(m.group(1))
            start = pending if pending is not None else 0.0
            if end > start:
                spans.append((start, end))
            pending = None

    # 文件结尾处的静音只有 silence_start，没有配对的 silence_end。
    if pending is not None:
        spans.append((pending, probe(path).duration))

    return spans


_SCENE_TS = re.compile(r"pts_time:([\d.]+)")


def detect_scenes(path: str | Path, *, threshold: float = 0.3) -> list[float]:
    """场景切换检测，返回切换点的时间戳列表（秒）。"""
    cmd = [
        _binary("ffmpeg"),
        "-hide_banner", "-nostdin",
        "-i", str(path),
        "-filter_complex", f"select='gt(scene,{threshold})',metadata=print:file=-",
        "-an", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise FFmpegFailed(f"场景检测失败: {proc.stderr.strip()[-500:]}")

    combined = proc.stdout + "\n" + proc.stderr
    return sorted({float(m.group(1)) for m in _SCENE_TS.finditer(combined)})


def _quote(arg: str) -> str:
    return f"'{arg}'" if " " in arg or ";" in arg else arg
