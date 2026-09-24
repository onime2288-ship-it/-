"""命令行入口。

    vedit run recipe.yml          # 跑一条流水线
    vedit probe raw/take1.mp4     # 看素材信息
    vedit analyze raw/take1.mp4   # 预估去静音能剪掉多少（不产出文件）
    vedit retime-srt ...          # 剪完之后给字幕重新对轴
    vedit init my-flow            # 生成一份 recipe 模板
    vedit steps                   # 列出所有可用步骤
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ffmpeg, pipeline, recipe as recipe_mod, srt
from .errors import VeditError
from .steps import available_steps
from .timeline import Timeline, _fmt

TEMPLATE = """\
# {name} —— 剪辑流水线配置
#
# 跑起来:  vedit run {name}.yml -v
# 试参数:  vedit run {name}.yml --dry-run   （只打印 ffmpeg 命令，不实际渲染）

name: {name}

input: raw/take1.mp4          # 也可以用 inputs: [a.mp4, b.mp4] 或 input_dir: raw/
output: out/{name}.mp4

steps:
  # 1) 自动去掉说话之间的静音停顿
  - type: silence_cut
    noise: "-32dB"            # 环境噪音大就调高，比如 -26dB
    min_silence: 0.6          # 停顿超过 0.6 秒才剪
    padding: 0.12             # 每句前后各留 0.12 秒，不然听起来很急
    min_keep: 0.3

  # 2) 轻微提速，观感更紧凑（可选）
  # - type: speed
  #   factor: 1.06

  # 3) 音频处理
  - type: denoise
    strength: 12
  - type: loudnorm
    target_i: -16             # 短视频平台建议 -14

  # 4) 片头淡入 / 片尾淡出
  - type: fade
    in_duration: 0.4
    out_duration: 0.6

  # 5) 烧字幕（时间轴要先用 vedit retime-srt 对过）
  # - type: subtitles
  #   file: subs/final.srt
  #   font_size: 42

  # 6) 输出规格
  - type: export
    preset: landscape         # landscape / vertical / square / portrait45
    fit: pad                  # pad=加黑边  crop=裁切铺满
    crf: 20

encode:
  preset: medium              # ultrafast…veryslow，越慢体积越小
  audio_bitrate: "192k"
"""


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "handler", None):
        parser.print_help()
        return 1

    try:
        return args.handler(args)
    except VeditError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vedit",
        description="配置驱动的视频剪辑流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="按 recipe 执行剪辑")
    p_run.add_argument("recipe", help="recipe YAML 文件")
    p_run.add_argument("-o", "--output", help="覆盖 recipe 里的 output")
    p_run.add_argument("--dry-run", action="store_true", help="只打印 ffmpeg 命令，不渲染")
    p_run.add_argument("-v", "--verbose", action="store_true", help="显示每一步的详情")
    p_run.set_defaults(handler=cmd_run)

    p_probe = sub.add_parser("probe", help="显示素材信息")
    p_probe.add_argument("files", nargs="+")
    p_probe.set_defaults(handler=cmd_probe)

    p_an = sub.add_parser("analyze", help="预估去静音效果，不产出文件")
    p_an.add_argument("file")
    p_an.add_argument("--noise", default="-32dB")
    p_an.add_argument("--min-silence", type=float, default=0.6)
    p_an.add_argument("--padding", type=float, default=0.12)
    p_an.add_argument("--scenes", action="store_true", help="同时做场景切换检测")
    p_an.set_defaults(handler=cmd_analyze)

    p_srt = sub.add_parser("retime-srt", help="按剪辑结果给字幕重新对轴")
    p_srt.add_argument("subtitle", help="原始 SRT（对应未剪辑的素材）")
    p_srt.add_argument("-t", "--timeline", required=True,
                       help="成片路径或 .timeline.json")
    p_srt.add_argument("-o", "--output", required=True, help="输出 SRT")
    p_srt.set_defaults(handler=cmd_retime)

    p_init = sub.add_parser("init", help="生成一份 recipe 模板")
    p_init.add_argument("name", help="流程名，会生成 <name>.yml")
    p_init.set_defaults(handler=cmd_init)

    p_steps = sub.add_parser("steps", help="列出所有可用步骤")
    p_steps.set_defaults(handler=cmd_steps)

    return parser


def cmd_run(args) -> int:
    rec = recipe_mod.load(args.recipe)
    if args.output:
        rec.output = Path(args.output)

    print(f"▶ {rec.name}: {len(rec.inputs)} 个输入 → {rec.output}")
    result = pipeline.run(rec, dry_run=args.dry_run, verbose=args.verbose)

    if args.dry_run:
        print("\n(dry-run，没有实际渲染)")
        return 0

    if not args.verbose:
        for note in result.notes:
            print(f"  {note}")

    print(
        f"\n✓ {result.output}"
        f"\n  时长 {_fmt(result.source_duration)} → {_fmt(result.output_duration)}"
        f"  (省掉 {_fmt(result.saved)})"
        f"\n  耗时 {result.elapsed:.1f}s"
    )
    if len(rec.inputs) == 1:
        print(f"  时间线 {pipeline.sidecar_path(result.output).name}（字幕对轴用）")
    return 0


def cmd_probe(args) -> int:
    for path in args.files:
        info = ffmpeg.probe(path)
        audio = f"{info.sample_rate}Hz" if info.has_audio else "无音轨"
        print(
            f"{Path(path).name}\n"
            f"  {info.width}x{info.height}"
            f"{' (竖屏)' if info.is_vertical else ''}"
            f"  {info.fps:.2f}fps  {_fmt(info.duration)}  音频: {audio}"
        )
    return 0


def cmd_analyze(args) -> int:
    info = ffmpeg.probe(args.file)
    print(f"{Path(args.file).name}: {info.width}x{info.height} {_fmt(info.duration)}")

    if not info.has_audio:
        print("  没有音轨，无法做静音检测")
    else:
        spans = ffmpeg.detect_silence(
            args.file, noise=args.noise, min_duration=args.min_silence
        )
        timeline = (
            Timeline.full(str(args.file), info.duration)
            .remove(spans)
            .pad(args.padding)
        )
        silent_total = sum(e - s for s, e in spans)
        print(
            f"\n静音检测 (noise={args.noise}, min_silence={args.min_silence}s):"
            f"\n  {len(spans)} 段静音，共 {_fmt(silent_total)}"
            f"\n  剪完: {timeline.summary()}"
        )
        if spans:
            print("\n  前几段静音:")
            for start, end in spans[:8]:
                print(f"    {_fmt(start)} - {_fmt(end)}  ({end - start:.1f}s)")
            if len(spans) > 8:
                print(f"    … 还有 {len(spans) - 8} 段")

    if args.scenes:
        cuts = ffmpeg.detect_scenes(args.file)
        print(f"\n场景切换: {len(cuts)} 处")
        for t in cuts[:10]:
            print(f"    {_fmt(t)}")

    return 0


def cmd_retime(args) -> int:
    timeline = pipeline.read_sidecar(Path(args.timeline))
    cues = srt.parse(args.subtitle)
    retimed = srt.retime(cues, timeline)
    out = srt.write(retimed, args.output)
    dropped = len(cues) - len(retimed)
    print(f"✓ {out}  {len(cues)} 条 → {len(retimed)} 条（丢弃 {dropped} 条落在剪掉区间里的）")
    return 0


def cmd_init(args) -> int:
    path = Path(f"{args.name}.yml")
    if path.exists():
        print(f"错误: {path} 已存在", file=sys.stderr)
        return 1
    path.write_text(TEMPLATE.format(name=args.name), encoding="utf-8")
    print(f"✓ 已生成 {path}\n  改好路径后运行: vedit run {path} -v")
    return 0


def cmd_steps(args) -> int:
    print("可用步骤:\n")
    for name, cls in sorted(available_steps().items()):
        doc = (cls.__doc__ or "").strip().splitlines()
        summary = doc[0] if doc else ""
        print(f"  {name:<14} {summary}")
    print("\n每个步骤的完整参数见 README.md 或源码 vedit/steps/。")
    return 0
