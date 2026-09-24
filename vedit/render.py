"""把 Timeline 渲染成成片。

整个工作流里唯一会做编码的地方。两条路径：

1. filter_complex（默认）—— 一条命令搞定，最快，但片段太多时命令行会爆。
2. 分块渲染 —— 逐段编码到临时文件，再用 concat demuxer 拼起来。
   片段数超过阈值时自动切换。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg
from .timeline import Timeline

# 超过这个片段数就改用分块渲染。filter_complex 的长度上限取决于系统的
# ARG_MAX，实测几百段就可能出问题，留足余量。
INLINE_SEGMENT_LIMIT = 150

# atempo 单次只接受 0.5~2.0，超出范围要串联多个。
_ATEMPO_MIN = 0.5
_ATEMPO_MAX = 2.0


@dataclass
class RenderOptions:
    """编码参数。"""

    crf: int = 20
    preset: str = "medium"
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    pixel_format: str = "yuv420p"
    fps: float | None = None
    # 追加在片段拼接之后的滤镜，由各 step 贡献（缩放、字幕、响度归一化……）
    video_filters: list[str] = field(default_factory=list)
    audio_filters: list[str] = field(default_factory=list)
    dry_run: bool = False


def atempo_chain(speed: float) -> list[str]:
    """把任意速度系数拆成若干个合法的 atempo。"""
    if abs(speed - 1.0) < 1e-6:
        return []

    chain: list[str] = []
    remaining = speed
    while remaining > _ATEMPO_MAX:
        chain.append(f"atempo={_ATEMPO_MAX}")
        remaining /= _ATEMPO_MAX
    while remaining < _ATEMPO_MIN:
        chain.append(f"atempo={_ATEMPO_MIN}")
        remaining /= _ATEMPO_MIN
    if abs(remaining - 1.0) > 1e-6:
        chain.append(f"atempo={remaining:.6f}")
    return chain


def build_filter_complex(
    timeline: Timeline,
    *,
    has_audio: bool,
    video_filters: list[str],
    audio_filters: list[str],
) -> tuple[str, str, str | None]:
    """构造 filter_complex 字符串，返回 (filtergraph, 视频输出标签, 音频输出标签)。"""
    parts: list[str] = []
    v_labels: list[str] = []
    a_labels: list[str] = []

    for i, seg in enumerate(timeline.segments):
        vl = f"v{i}"
        chain = [
            f"trim=start={seg.start:.6f}:end={seg.end:.6f}",
            "setpts=PTS-STARTPTS",
        ]
        if abs(seg.speed - 1.0) > 1e-6:
            # setpts 里的分母就是倍速，2.0 倍速 = 时长减半
            chain.insert(2, f"setpts=PTS/{seg.speed:.6f}")
        parts.append(f"[0:v]{','.join(chain)}[{vl}]")
        v_labels.append(vl)

        if has_audio:
            al = f"a{i}"
            achain = [
                f"atrim=start={seg.start:.6f}:end={seg.end:.6f}",
                "asetpts=PTS-STARTPTS",
            ]
            achain.extend(atempo_chain(seg.speed))
            parts.append(f"[0:a]{','.join(achain)}[{al}]")
            a_labels.append(al)

    # 拼接
    n = len(timeline.segments)
    if has_audio:
        interleaved = "".join(f"[{v}][{a}]" for v, a in zip(v_labels, a_labels))
        parts.append(f"{interleaved}concat=n={n}:v=1:a=1[vcat][acat]")
        v_out, a_out = "vcat", "acat"
    else:
        interleaved = "".join(f"[{v}]" for v in v_labels)
        parts.append(f"{interleaved}concat=n={n}:v=1:a=0[vcat]")
        v_out, a_out = "vcat", None

    # 后期滤镜
    if video_filters:
        parts.append(f"[{v_out}]{','.join(video_filters)}[vout]")
        v_out = "vout"
    if a_out and audio_filters:
        parts.append(f"[{a_out}]{','.join(audio_filters)}[aout]")
        a_out = "aout"

    return ";".join(parts), v_out, a_out


def _encode_args(opts: RenderOptions, *, with_audio: bool) -> list[str]:
    args = [
        "-c:v", opts.video_codec,
        "-crf", str(opts.crf),
        "-preset", opts.preset,
        "-pix_fmt", opts.pixel_format,
    ]
    if opts.fps:
        args += ["-r", str(opts.fps)]
    if with_audio:
        args += ["-c:a", opts.audio_codec, "-b:a", opts.audio_bitrate]
    else:
        args += ["-an"]
    # 让成片能在浏览器里边下边播
    args += ["-movflags", "+faststart"]
    return args


def render(timeline: Timeline, output: str | Path, opts: RenderOptions) -> Path:
    """渲染时间线到 output。"""
    timeline.require_content("渲染")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    info = ffmpeg.probe(timeline.source)
    has_audio = info.has_audio

    if len(timeline.segments) > INLINE_SEGMENT_LIMIT:
        return _render_chunked(timeline, output, opts, has_audio=has_audio)

    graph, v_out, a_out = build_filter_complex(
        timeline,
        has_audio=has_audio,
        video_filters=opts.video_filters,
        audio_filters=opts.audio_filters,
    )

    args = ["-i", str(timeline.source), "-filter_complex", graph, "-map", f"[{v_out}]"]
    if a_out:
        args += ["-map", f"[{a_out}]"]
    args += _encode_args(opts, with_audio=bool(a_out))
    args.append(str(output))

    ffmpeg.run(args, dry_run=opts.dry_run)
    return output


def _render_chunked(
    timeline: Timeline,
    output: Path,
    opts: RenderOptions,
    *,
    has_audio: bool,
) -> Path:
    """片段数太多时：逐段编码再拼接。

    比 filter_complex 慢，但不受命令行长度限制，几千段也能跑。
    """
    with tempfile.TemporaryDirectory(prefix="vedit-") as tmpdir:
        tmp = Path(tmpdir)
        chunk_paths: list[Path] = []

        for i, seg in enumerate(timeline.segments):
            chunk = tmp / f"chunk{i:05d}.mp4"
            vf = ["setpts=PTS-STARTPTS"]
            af = ["asetpts=PTS-STARTPTS"]
            if abs(seg.speed - 1.0) > 1e-6:
                vf.append(f"setpts=PTS/{seg.speed:.6f}")
                af.extend(atempo_chain(seg.speed))

            # -ss 放在 -i 之前是快速定位，但会对齐到关键帧；
            # 放在之后才是精确到帧的裁切，这里精度优先。
            args = [
                "-i", str(timeline.source),
                "-ss", f"{seg.start:.6f}",
                "-to", f"{seg.end:.6f}",
                "-vf", ",".join(vf),
            ]
            if has_audio:
                args += ["-af", ",".join(af)]
            args += _encode_args(opts, with_audio=has_audio)
            args.append(str(chunk))

            ffmpeg.run(args, dry_run=opts.dry_run)
            chunk_paths.append(chunk)

        if opts.dry_run:
            print(f"[dry-run] 将拼接 {len(chunk_paths)} 个分块 → {output}")
            return output

        listfile = tmp / "concat.txt"
        listfile.write_text(
            "\n".join(f"file '{p}'" for p in chunk_paths) + "\n",
            encoding="utf-8",
        )

        concat_args = [
            "-f", "concat", "-safe", "0",
            "-i", str(listfile),
        ]
        # 后期滤镜只能在拼接后施加
        if opts.video_filters:
            concat_args += ["-vf", ",".join(opts.video_filters)]
        if has_audio and opts.audio_filters:
            concat_args += ["-af", ",".join(opts.audio_filters)]

        if opts.video_filters or (has_audio and opts.audio_filters):
            concat_args += _encode_args(opts, with_audio=has_audio)
        else:
            concat_args += ["-c", "copy"]
        concat_args.append(str(output))

        ffmpeg.run(concat_args, dry_run=opts.dry_run)

    return output
