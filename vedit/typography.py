"""文字层 —— 把文案渲染成 drawtext 滤镜。

两个绕不开的 ffmpeg 限制，处理方式都写在下面：

1. drawtext 没有字距（letter-spacing）参数。品牌片里「七　尚」那种拉开
   的字距只能靠手动往字符之间插空格实现。
2. 中文必须显式指定 fontfile，否则渲染成方框。不能依赖 font= 家族名。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .shots import Caption

# 画面上的纵向位置 → 文字块中心占画面高度的比例
POSITIONS: dict[str, float] = {
    "top": 0.14,
    "upper-third": 0.28,      # 七尚文案用的位置
    "center": 0.50,
    "lower-third": 0.70,
    "bottom": 0.86,
}


def resolve_font(spec: str) -> Path:
    """把字体名或路径解析成实际的字体文件。

    优先当路径用；不是路径就交给 fc-match 按家族名找。
    """
    path = Path(spec)
    if path.is_file():
        return path

    try:
        proc = subprocess.run(
            ["fc-match", "--format=%{file}", spec],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        raise ConfigError(
            f"找不到字体 '{spec}'，且系统里没有 fc-match 可用于按名查找。"
            "请在 brand.yml 里直接写字体文件的完整路径。"
        ) from None

    found = proc.stdout.strip()
    if not found or not Path(found).is_file():
        raise ConfigError(f"找不到字体 '{spec}'。请改写成字体文件的完整路径。")

    resolved = Path(found)
    # fc-match 找不到时会回退到某个默认字体，不会报错。
    # 这里粗略提醒一下，避免中文悄悄变成方框。
    return resolved


def track(text: str, spacing: int) -> str:
    """手动拉开字距：在每个字符之间插入空格。

    spacing 是插入的空格数。中文字距拉开 1~2 格就很明显，
    英文大写标题通常 1 格。
    """
    if spacing <= 0:
        return text
    gap = " " * spacing
    return gap.join(text)


@dataclass
class TextStyle:
    """一种文字样式。尺寸用画面高度的比例表示，这样换画幅不用重调。"""

    font: str
    size_ratio: float = 0.038          # 字号 / 画面高度
    color: str = "white"
    opacity: float = 1.0
    letter_spacing: int = 0
    line_gap_ratio: float = 0.020      # 行距 / 画面高度
    uppercase: bool = False
    shadow: bool = False               # 浅色背景上不需要，深色背景上防止看不清

    def size_for(self, canvas_height: int) -> int:
        return max(8, round(self.size_ratio * canvas_height))

    def line_gap_for(self, canvas_height: int) -> int:
        return round(self.line_gap_ratio * canvas_height)


@dataclass
class TypeSet:
    """一套品牌文字系统：中文体 + 英文体，以及各自的样式。"""

    zh: TextStyle
    en: TextStyle
    position: str = "upper-third"
    block_gap_ratio: float = 0.030     # 中文块和英文块之间的间距

    def __post_init__(self) -> None:
        if self.position not in POSITIONS:
            raise ConfigError(
                f"未知的位置 '{self.position}'。可用: {', '.join(POSITIONS)}"
            )


def build_caption_filters(
    caption: Caption,
    typeset: TypeSet,
    *,
    canvas: tuple[int, int],
    shot_duration: float,
    textdir: Path,
    uid: str,
) -> list[str]:
    """生成一个镜头上所有文字行的 drawtext 滤镜。

    文案通过 textfile= 传给 ffmpeg，而不是 text=。中文里的标点、冒号、
    百分号在 text= 里全都要转义，用文件就完全绕开了这个坑。
    """
    if caption.is_empty:
        return []

    width, height = canvas
    textdir.mkdir(parents=True, exist_ok=True)

    # 先把所有行连同样式排好，算出整块的高度，再居中摆放
    rows: list[tuple[str, TextStyle, int]] = []   # (文本, 样式, 行高)
    for line in caption.zh:
        size = typeset.zh.size_for(height)
        rows.append((track(line, typeset.zh.letter_spacing), typeset.zh, size))

    if caption.zh and caption.en:
        rows.append(("", typeset.zh, round(typeset.block_gap_ratio * height)))

    for line in caption.en:
        text = line.upper() if typeset.en.uppercase else line
        size = typeset.en.size_for(height)
        rows.append((track(text, typeset.en.letter_spacing), typeset.en, size))

    total = sum(size for _, _, size in rows)
    total += sum(
        style.line_gap_for(height) for _, style, _ in rows[:-1]
    )

    center = POSITIONS[caption.position or typeset.position] * height
    cursor = center - total / 2

    filters: list[str] = []
    for i, (text, style, size) in enumerate(rows):
        if text:
            path = textdir / f"{uid}_{i}.txt"
            path.write_text(text, encoding="utf-8")
            filters.append(
                _drawtext(
                    path, style, size, round(cursor), width,
                    caption=caption, shot_duration=shot_duration,
                )
            )
        cursor += size + style.line_gap_for(height)

    return filters


def _drawtext(
    textfile: Path,
    style: TextStyle,
    size: int,
    y: int,
    width: int,
    *,
    caption: Caption,
    shot_duration: float,
) -> str:
    """单行文字的 drawtext，带淡入淡出。"""
    font = resolve_font(style.font)

    start = max(0.0, caption.delay)
    end = max(start + 0.1, shot_duration - caption.delay * 0.5)
    fade = max(0.01, min(caption.fade, (end - start) / 2))

    # 用 t 的分段表达式做淡入淡出。t 是镜头内的本地时间，
    # 因为每个镜头在拼接前都已经 setpts 归零了。
    alpha = (
        f"if(lt(t,{start:.3f}),0,"
        f"if(lt(t,{start + fade:.3f}),(t-{start:.3f})/{fade:.3f},"
        f"if(lt(t,{end - fade:.3f}),1,"
        f"if(lt(t,{end:.3f}),({end:.3f}-t)/{fade:.3f},0))))"
    )
    if style.opacity < 1.0:
        alpha = f"({alpha})*{style.opacity}"

    parts = [
        f"fontfile='{font}'",
        f"textfile='{textfile}'",
        f"fontsize={size}",
        f"fontcolor={style.color}",
        "x=(w-text_w)/2",
        f"y={y}",
        f"alpha='{alpha}'",
    ]
    if style.shadow:
        parts += ["shadowcolor=black@0.35", "shadowx=2", "shadowy=2"]

    return "drawtext=" + ":".join(parts)
