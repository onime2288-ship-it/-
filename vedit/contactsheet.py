"""联系表 —— 把候选镜头拼成一张图，让人（和我）一眼看完整个素材库。

这是整套工作流里唯一「机器做不了、必须靠看」的环节：
指标能判断清不清楚、稳不稳，但判断不了这是茶山还是屋顶、构图好不好、
有没有电线穿帮。所以把代表帧拼成联系表，交给眼睛。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .errors import FFmpegFailed
from .library import ShotCandidate
from .typography import resolve_font

# 缩略图尺寸。太小看不清构图，太大则一张表放不下几个镜头。
THUMB_WIDTH = 480
LABEL_HEIGHT = 54
DEFAULT_COLUMNS = 4
DEFAULT_ROWS = 5


def _label_for(index: int, shot: ShotCandidate) -> str:
    """缩略图下方的标注。编号是关键 —— 后面靠它指代镜头。"""
    name = Path(shot.source).name
    if len(name) > 22:
        name = name[:19] + "..."
    flags = (" ⚠" + "/".join(shot.flags)) if shot.flags else ""
    return (
        f"#{index:03d}  {name}\n"
        f"{shot.best_start:.1f}s +{shot.best_length:.1f}s  "
        f"{shot.motion_kind}  {shot.score:.0f}分{flags}"
    )


def _extract_thumb(
    shot: ShotCandidate,
    index: int,
    out_path: Path,
    textdir: Path,
    *,
    font: Path,
    width: int,
) -> bool:
    """抽一帧并在下方压上标注。取推荐段的中点，比首帧有代表性。"""
    moment = shot.best_start + shot.best_length / 2

    label_file = textdir / f"label_{index:04d}.txt"
    label_file.write_text(_label_for(index, shot), encoding="utf-8")

    height_expr = f"{width}:-2"
    vf = (
        f"scale={height_expr},"
        f"pad=iw:ih+{LABEL_HEIGHT}:0:0:color=#1a1a1a,"
        f"drawtext=fontfile='{font}':textfile='{label_file}'"
        f":fontsize=17:fontcolor=#e8e8e8:line_spacing=4"
        f":x=10:y=h-{LABEL_HEIGHT}+7"
    )

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-ss", f"{moment:.3f}",
        "-i", shot.source,
        "-frames:v", "1",
        "-vf", vf,
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return proc.returncode == 0 and out_path.exists()


def build(
    shots: list[ShotCandidate],
    output: Path,
    *,
    columns: int = DEFAULT_COLUMNS,
    rows: int = DEFAULT_ROWS,
    thumb_width: int = THUMB_WIDTH,
    font: str = "Noto Sans CJK SC",
) -> list[Path]:
    """生成联系表，镜头多时自动分成多页。返回生成的图片路径列表。"""
    if not shots:
        raise FFmpegFailed("没有可以放进联系表的镜头")

    font_file = resolve_font(font)
    output.parent.mkdir(parents=True, exist_ok=True)
    per_page = columns * rows
    pages: list[Path] = []

    with tempfile.TemporaryDirectory(prefix="vedit-sheet-") as tmpdir:
        tmp = Path(tmpdir)
        textdir = tmp / "labels"
        textdir.mkdir()

        for page_no, offset in enumerate(range(0, len(shots), per_page), start=1):
            chunk = shots[offset:offset + per_page]
            thumbdir = tmp / f"page{page_no}"
            thumbdir.mkdir()

            made = 0
            for i, shot in enumerate(chunk):
                # 编号在整个联系表里连续，跨页也不重置
                number = offset + i + 1
                target = thumbdir / f"t{made + 1:04d}.png"
                if _extract_thumb(
                    shot, number, target, textdir, font=font_file, width=thumb_width
                ):
                    made += 1

            if made == 0:
                continue

            page_path = (
                output if len(shots) <= per_page
                else output.with_name(f"{output.stem}_{page_no}{output.suffix}")
            )
            _tile(thumbdir, page_path, columns=columns, count=made)
            pages.append(page_path)

    return pages


def _tile(thumbdir: Path, output: Path, *, columns: int, count: int) -> None:
    """把缩略图拼成网格。"""
    rows_needed = (count + columns - 1) // columns
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-i", str(thumbdir / "t%04d.png"),
        "-vf", f"tile={columns}x{rows_needed}:padding=6:margin=10:color=#111111",
        "-frames:v", "1",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise FFmpegFailed(f"拼合联系表失败: {proc.stderr.strip()[-300:]}")
