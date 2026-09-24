"""流水线编排：读 recipe → 建时间线 → 跑步骤 → 渲染。"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg
from .errors import ConfigError
from .recipe import Recipe
from .render import RenderOptions, render
from .steps import Context, build_step
from .timeline import Timeline


@dataclass
class Result:
    """一次流水线执行的结果。"""

    output: Path
    source_duration: float
    output_duration: float
    elapsed: float
    notes: list[str]

    @property
    def saved(self) -> float:
        return self.source_duration - self.output_duration


def run(recipe: Recipe, *, dry_run: bool = False, verbose: bool = False) -> Result:
    """执行一份 recipe。"""
    started = time.monotonic()

    for path in recipe.inputs:
        if not path.exists():
            raise ConfigError(f"输入文件不存在: {path}")

    if len(recipe.inputs) == 1:
        ctx = _process_one(recipe.inputs[0], recipe, dry_run=dry_run, verbose=verbose)
        render(ctx.timeline, recipe.output, ctx.options)
        if not dry_run:
            write_sidecar(ctx.timeline, recipe.output)
        return Result(
            output=recipe.output,
            source_duration=ctx.info.duration,
            output_duration=ctx.timeline.output_duration,
            elapsed=time.monotonic() - started,
            notes=ctx.notes,
        )

    return _run_multi(recipe, dry_run=dry_run, verbose=verbose, started=started)


def _process_one(
    source: Path, recipe: Recipe, *, dry_run: bool, verbose: bool
) -> Context:
    """对单个输入跑完所有步骤，返回最终状态（还没渲染）。"""
    info = ffmpeg.probe(source)
    if not info.has_video:
        raise ConfigError(f"{source} 里没有视频流")

    ctx = Context(
        timeline=Timeline.full(str(source), info.duration),
        info=info,
        options=recipe.render_options(dry_run=dry_run),
        verbose=verbose,
    )

    if verbose:
        print(f"\n[{source.name}] {info.width}x{info.height} "
              f"@{info.fps:.2f}fps, {info.duration:.1f}s")

    for spec in recipe.steps:
        step = build_step(spec)
        step.apply(ctx)

    ctx.timeline.require_content("所有步骤")
    return ctx


def _run_multi(
    recipe: Recipe, *, dry_run: bool, verbose: bool, started: float
) -> Result:
    """多个输入：各自剪好，再拼成一条。

    每段只编码一次，拼接阶段用 concat demuxer 流拷贝，不会二次损失画质。
    """
    recipe.output.parent.mkdir(parents=True, exist_ok=True)
    total_source = 0.0
    total_output = 0.0
    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="vedit-multi-") as tmpdir:
        tmp = Path(tmpdir)
        parts: list[Path] = []

        for i, source in enumerate(recipe.inputs):
            ctx = _process_one(source, recipe, dry_run=dry_run, verbose=verbose)
            part = tmp / f"part{i:04d}.mp4"
            render(ctx.timeline, part, ctx.options)
            parts.append(part)
            total_source += ctx.info.duration
            total_output += ctx.timeline.output_duration
            notes.extend(f"[{source.name}] {n}" for n in ctx.notes)

        if dry_run:
            print(f"[dry-run] 将拼接 {len(parts)} 段 → {recipe.output}")
        else:
            listfile = tmp / "concat.txt"
            listfile.write_text(
                "\n".join(f"file '{p}'" for p in parts) + "\n", encoding="utf-8"
            )
            ffmpeg.run(
                ["-f", "concat", "-safe", "0", "-i", str(listfile),
                 "-c", "copy", "-movflags", "+faststart", str(recipe.output)],
                dry_run=False,
            )
            notes.append(f"拼接 {len(parts)} 段素材")

    return Result(
        output=recipe.output,
        source_duration=total_source,
        output_duration=total_output,
        elapsed=time.monotonic() - started,
        notes=notes,
    )


def sidecar_path(output: Path) -> Path:
    """成片对应的时间线文件路径。"""
    return output.with_suffix(output.suffix + ".timeline.json")


def write_sidecar(timeline: Timeline, output: Path) -> Path:
    """把时间线存到成片旁边。

    字幕对轴（vedit retime-srt）要靠它才知道哪些片段被剪掉了。
    """
    path = sidecar_path(output)
    path.write_text(
        json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def read_sidecar(path: Path) -> Timeline:
    """读回时间线。传成片路径或 .timeline.json 都行。"""
    if path.suffix != ".json":
        path = sidecar_path(path)
    if not path.exists():
        raise ConfigError(
            f"找不到时间线文件: {path}\n"
            "它会在 vedit run 成功后自动生成在成片旁边。"
        )
    return Timeline.from_dict(json.loads(path.read_text(encoding="utf-8")))
