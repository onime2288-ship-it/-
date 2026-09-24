"""裁切类步骤：只改时间线，不做编码。"""

from __future__ import annotations

from typing import Any

from .. import ffmpeg
from ..errors import ConfigError
from ..timecode import parse_span, parse_time
from .base import Context, Step, register


@register
class Trim(Step):
    """手动裁切：保留或删除指定的时间区间。

    recipe 示例:
        - type: trim
          keep: ["0:05-2:30"]        # 只保留这些
        - type: trim
          drop: ["0:00-0:08", "5:00-5:20"]   # 删掉这些
    """

    name = "trim"

    def configure(self, **params: Any) -> None:
        keep = params.get("keep")
        drop = params.get("drop")
        if keep and drop:
            raise ConfigError("trim 步骤的 keep 和 drop 只能二选一")
        if not keep and not drop:
            raise ConfigError("trim 步骤需要 keep 或 drop 之一")

        self.keep = [parse_span(s) for s in keep] if keep else None
        self.drop = [parse_span(s) for s in drop] if drop else None

    def apply(self, ctx: Context) -> None:
        before = ctx.timeline.output_duration
        if self.keep is not None:
            ctx.timeline = ctx.timeline.keep_only(self.keep)
        else:
            ctx.timeline = ctx.timeline.remove(self.drop)
        ctx.timeline.require_content("trim")
        ctx.log(f"trim: {before:.1f}s → {ctx.timeline.output_duration:.1f}s")


@register
class SilenceCut(Step):
    """自动去掉静音段 —— 口播类素材最省时间的一步。

    recipe 示例:
        - type: silence_cut
          noise: "-32dB"       # 低于这个音量算静音，环境噪音大就调高（如 -26dB）
          min_silence: 0.6     # 静音持续超过这么久才剪
          padding: 0.12        # 每句话前后各留一点，避免听起来被削掉
          min_keep: 0.3        # 剪完后短于这个长度的碎片直接丢掉
    """

    name = "silence_cut"

    def configure(self, **params: Any) -> None:
        self.noise = str(params.get("noise", "-32dB"))
        self.min_silence = self._get(params, "min_silence", 0.6, float, "数字")
        self.padding = self._get(params, "padding", 0.12, float, "数字")
        self.min_keep = self._get(params, "min_keep", 0.3, float, "数字")

        if self.min_silence <= 0:
            raise ConfigError("min_silence 必须大于 0")
        if self.padding < 0:
            raise ConfigError("padding 不能为负数")

    def apply(self, ctx: Context) -> None:
        if not ctx.info.has_audio:
            ctx.log("silence_cut: 素材没有音轨，跳过")
            return

        spans = ffmpeg.detect_silence(
            ctx.timeline.source,
            noise=self.noise,
            min_duration=self.min_silence,
        )
        if not spans:
            ctx.log(f"silence_cut: 没检测到静音段（阈值 {self.noise}）")
            return

        before = ctx.timeline.output_duration
        ctx.timeline = ctx.timeline.remove(spans)
        # 先挖掉静音，再把每段前后补回一点，最后清掉碎片
        ctx.timeline = ctx.timeline.pad(self.padding)
        ctx.timeline = ctx.timeline.drop_shorter_than(self.min_keep)
        ctx.timeline.require_content("silence_cut")

        removed = before - ctx.timeline.output_duration
        ctx.log(
            f"silence_cut: 检测到 {len(spans)} 段静音, "
            f"剪掉 {removed:.1f}s, 剩 {len(ctx.timeline.segments)} 段"
        )


@register
class Speed(Step):
    """整体变速。

    recipe 示例:
        - type: speed
          factor: 1.08     # 口播提速 5~10% 观感更紧凑，几乎听不出来
    """

    name = "speed"

    def configure(self, **params: Any) -> None:
        self.factor = self._get(params, "factor", 1.0, float, "数字")
        if self.factor <= 0:
            raise ConfigError("speed 的 factor 必须大于 0")

    def apply(self, ctx: Context) -> None:
        if abs(self.factor - 1.0) < 1e-6:
            return
        ctx.timeline = ctx.timeline.with_speed(self.factor)
        ctx.log(f"speed: ×{self.factor}, 成片时长 {ctx.timeline.output_duration:.1f}s")


@register
class Head(Step):
    """只保留开头一段 —— 做预览样片时很方便。

    recipe 示例:
        - type: head
          duration: 30
    """

    name = "head"

    def configure(self, **params: Any) -> None:
        if "duration" not in params:
            raise ConfigError("head 步骤需要 duration")
        self.duration = parse_time(params["duration"])

    def apply(self, ctx: Context) -> None:
        kept = []
        remaining = self.duration
        for seg in ctx.timeline.segments:
            if remaining <= 0:
                break
            take = min(seg.output_duration, remaining)
            source_take = take * seg.speed
            kept.append(
                seg if source_take >= seg.source_duration
                else type(seg)(seg.start, seg.start + source_take, seg.speed)
            )
            remaining -= take

        ctx.timeline.segments = kept
        ctx.timeline.require_content("head")
        ctx.log(f"head: 截取前 {ctx.timeline.output_duration:.1f}s")
