"""效果类步骤：不动时间线，只往最终渲染里追加滤镜。

因为滤镜是在片段拼接**之后**施加的，所以像响度归一化、字幕烧录这些
都只会执行一次，跟时间线被切成多少段无关。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ConfigError
from .base import Context, Step, register

# 常用输出规格。crop 模式会裁掉画面，pad 模式会加黑边。
PRESETS: dict[str, tuple[int, int]] = {
    "landscape": (1920, 1080),   # 横屏 16:9
    "landscape720": (1280, 720),
    "vertical": (1080, 1920),    # 竖屏 9:16，抖音/Reels/Shorts
    "square": (1080, 1080),      # 方形 1:1
    "portrait45": (1080, 1350),  # 4:5，Instagram 信息流
}


def _escape_filter_path(path: str) -> str:
    """ffmpeg 滤镜参数里的路径要转义反斜杠、冒号和单引号。

    Windows 路径（C:\\subs\\a.srt）不转义会被解析成滤镜参数分隔符。
    """
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


@register
class Export(Step):
    """设定输出规格与编码质量。

    recipe 示例:
        - type: export
          preset: vertical     # landscape / vertical / square / portrait45
          fit: pad             # pad=加黑边保全画面, crop=裁切铺满
          crf: 20              # 越小越清晰越大，18~23 是常用区间
          fps: 30
    """

    name = "export"

    def configure(self, **params: Any) -> None:
        self.preset = params.get("preset")
        self.width = self._get(params, "width", None, int, "整数")
        self.height = self._get(params, "height", None, int, "整数")
        self.fit = str(params.get("fit", "pad")).lower()
        self.crf = self._get(params, "crf", None, int, "整数")
        self.fps = self._get(params, "fps", None, float, "数字")
        self.encode_preset = params.get("encode_preset")

        if self.fit not in ("pad", "crop", "stretch"):
            raise ConfigError(f"export 的 fit 只能是 pad/crop/stretch，收到 {self.fit!r}")

        if self.preset:
            if self.preset not in PRESETS:
                raise ConfigError(
                    f"未知的 preset '{self.preset}'。可用: {', '.join(PRESETS)}"
                )
            self.width, self.height = PRESETS[self.preset]

        if (self.width is None) != (self.height is None):
            raise ConfigError("export 的 width 和 height 必须同时提供")

    def apply(self, ctx: Context) -> None:
        if self.crf is not None:
            ctx.options.crf = self.crf
        if self.fps is not None:
            ctx.options.fps = self.fps
        if self.encode_preset:
            ctx.options.video_codec = ctx.options.video_codec  # 编码器不变
            ctx.options.preset = str(self.encode_preset)

        if self.width and self.height:
            ctx.options.video_filters.extend(self._scale_filters(self.width, self.height))
            ctx.log(f"export: {self.width}x{self.height} ({self.fit}), crf={ctx.options.crf}")
        else:
            ctx.log(f"export: 保持原始尺寸, crf={ctx.options.crf}")

    def _scale_filters(self, w: int, h: int) -> list[str]:
        if self.fit == "stretch":
            return [f"scale={w}:{h}", "setsar=1"]
        if self.fit == "crop":
            # 先按短边铺满，再从中心裁掉多余部分
            return [
                f"scale={w}:{h}:force_original_aspect_ratio=increase",
                f"crop={w}:{h}",
                "setsar=1",
            ]
        # pad: 完整保留画面，四周补黑边
        return [
            f"scale={w}:{h}:force_original_aspect_ratio=decrease",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
            "setsar=1",
        ]


@register
class Loudnorm(Step):
    """响度归一化 —— 让成片音量符合平台标准，不用手动拉增益。

    recipe 示例:
        - type: loudnorm
          target_i: -16      # YouTube/播客 -16, 抖音等短视频 -14
    """

    name = "loudnorm"

    def configure(self, **params: Any) -> None:
        self.target_i = self._get(params, "target_i", -16.0, float, "数字")
        self.target_lra = self._get(params, "target_lra", 11.0, float, "数字")
        self.target_tp = self._get(params, "target_tp", -1.5, float, "数字")

    def apply(self, ctx: Context) -> None:
        if not ctx.info.has_audio:
            ctx.log("loudnorm: 素材没有音轨，跳过")
            return
        ctx.options.audio_filters.append(
            f"loudnorm=I={self.target_i}:LRA={self.target_lra}:TP={self.target_tp}"
        )
        # loudnorm 内部会重采样（输出 192kHz），不还原的话成片音轨采样率
        # 会被莫名其妙地抬高，白白占码率。这里显式采样回原始采样率。
        rate = ctx.info.sample_rate or 48000
        ctx.options.audio_filters.append(f"aresample={rate}")
        ctx.log(f"loudnorm: 目标 {self.target_i} LUFS (采样率保持 {rate}Hz)")


@register
class Denoise(Step):
    """音频降噪，压掉底噪和空调声。

    recipe 示例:
        - type: denoise
          strength: 12       # dB，太大会让人声发闷，10~15 比较安全
    """

    name = "denoise"

    def configure(self, **params: Any) -> None:
        self.strength = self._get(params, "strength", 12.0, float, "数字")

    def apply(self, ctx: Context) -> None:
        if not ctx.info.has_audio:
            ctx.log("denoise: 素材没有音轨，跳过")
            return
        ctx.options.audio_filters.append(f"afftdn=nf=-{abs(self.strength)}")
        ctx.log(f"denoise: -{abs(self.strength)}dB")


@register
class Subtitles(Step):
    """烧录字幕（硬字幕）。

    注意：字幕的时间轴必须对应**成片**，不是原始素材。
    如果前面做了去静音/变速，请用 `vedit shift-srt` 先重新对轴。

    recipe 示例:
        - type: subtitles
          file: subs/final.srt
          font_size: 42
          margin_v: 80
    """

    name = "subtitles"

    def configure(self, **params: Any) -> None:
        if "file" not in params:
            raise ConfigError("subtitles 步骤需要 file")
        self.file = Path(params["file"])
        self.font_size = self._get(params, "font_size", 42, int, "整数")
        self.font_name = params.get("font_name")
        self.primary_colour = params.get("colour", "&H00FFFFFF")
        self.outline = self._get(params, "outline", 2, int, "整数")
        self.margin_v = self._get(params, "margin_v", 60, int, "整数")

    def apply(self, ctx: Context) -> None:
        if not self.file.exists():
            raise ConfigError(f"字幕文件不存在: {self.file}")

        style_parts = [
            f"FontSize={self.font_size}",
            f"PrimaryColour={self.primary_colour}",
            f"Outline={self.outline}",
            f"MarginV={self.margin_v}",
            "BorderStyle=1",
        ]
        if self.font_name:
            style_parts.insert(0, f"FontName={self.font_name}")

        path = _escape_filter_path(str(self.file.resolve()))
        style = ",".join(style_parts)
        ctx.options.video_filters.append(f"subtitles='{path}':force_style='{style}'")
        ctx.log(f"subtitles: {self.file.name} ({self.font_size}px)")


@register
class Fade(Step):
    """片头淡入 / 片尾淡出。

    recipe 示例:
        - type: fade
          in_duration: 0.5
          out_duration: 0.8
    """

    name = "fade"

    def configure(self, **params: Any) -> None:
        self.fade_in = self._get(params, "in_duration", 0.0, float, "数字")
        self.fade_out = self._get(params, "out_duration", 0.0, float, "数字")

    def apply(self, ctx: Context) -> None:
        total = ctx.timeline.output_duration

        if self.fade_in > 0:
            ctx.options.video_filters.append(f"fade=t=in:st=0:d={self.fade_in}")
            if ctx.info.has_audio:
                ctx.options.audio_filters.append(f"afade=t=in:st=0:d={self.fade_in}")

        if self.fade_out > 0:
            start = max(0.0, total - self.fade_out)
            ctx.options.video_filters.append(f"fade=t=out:st={start:.3f}:d={self.fade_out}")
            if ctx.info.has_audio:
                ctx.options.audio_filters.append(
                    f"afade=t=out:st={start:.3f}:d={self.fade_out}"
                )

        if self.fade_in or self.fade_out:
            ctx.log(f"fade: 入 {self.fade_in}s / 出 {self.fade_out}s")
