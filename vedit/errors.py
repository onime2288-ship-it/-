"""工作流里所有可预期的错误类型。

CLI 会把这些异常转成一行人话的报错，而不是抛一整页 traceback。
"""


class VeditError(Exception):
    """所有 vedit 异常的基类。"""


class FFmpegNotFound(VeditError):
    """系统里没装 ffmpeg / ffprobe。"""


class FFmpegFailed(VeditError):
    """ffmpeg 返回了非零退出码。"""


class ProbeFailed(VeditError):
    """读不出媒体文件信息。"""


class ConfigError(VeditError):
    """recipe 配置文件有问题。"""


class EmptyTimeline(VeditError):
    """所有素材都被剪光了，没有内容可以渲染。"""
