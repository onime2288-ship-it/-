"""时间码解析。

recipe 里写 "1:23"、"00:01:23.5"、"83.5" 都应该能认，
因为人在看播放器记时间点的时候不会去换算成秒。
"""

from __future__ import annotations

from .errors import ConfigError


def parse_time(value: str | int | float) -> float:
    """把时间码转成秒。

    支持: 83.5 / "83.5" / "1:23.5" / "00:01:23.5"
    """
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise ConfigError(f"时间不能为负数: {value!r}")
        return seconds

    if not isinstance(value, str):
        raise ConfigError(f"无法解析的时间格式: {value!r}")

    text = value.strip()
    if not text:
        raise ConfigError("时间为空")

    parts = text.split(":")
    if len(parts) > 3:
        raise ConfigError(f"无法解析的时间格式: {value!r}")

    try:
        numbers = [float(p) for p in parts]
    except ValueError as exc:
        raise ConfigError(f"无法解析的时间格式: {value!r}") from exc

    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number

    if seconds < 0:
        raise ConfigError(f"时间不能为负数: {value!r}")
    return seconds


def parse_span(value) -> tuple[float, float]:
    """解析一个时间区间。

    支持: "1:00-1:30" / [60, 90] / {"start": 60, "end": 90}
    """
    if isinstance(value, dict):
        if "start" not in value or "end" not in value:
            raise ConfigError(f"区间需要 start 和 end: {value!r}")
        start, end = parse_time(value["start"]), parse_time(value["end"])
    elif isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise ConfigError(f"区间需要正好两个值: {value!r}")
        start, end = parse_time(value[0]), parse_time(value[1])
    elif isinstance(value, str) and "-" in value:
        left, _, right = value.partition("-")
        start, end = parse_time(left), parse_time(right)
    else:
        raise ConfigError(f"无法解析的区间: {value!r}")

    if end <= start:
        raise ConfigError(f"区间结束时间必须晚于开始时间: {value!r}")
    return start, end


def format_time(seconds: float) -> str:
    """秒 → HH:MM:SS.mmm（写 srt / 给人看用）。"""
    seconds = max(0.0, seconds)
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:  # 四舍五入进位
        millis, secs = 0, secs + 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
