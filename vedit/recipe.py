"""recipe（YAML 配置）的加载与校验。

一份 recipe 就是一条可复现的剪辑流程。把它提交进 git，
下次同类素材直接跑同一份配置，不用再回忆「上次那几个参数是多少」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .render import RenderOptions

_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".flv", ".wmv"}


@dataclass
class Recipe:
    """一份剪辑配置。"""

    name: str
    inputs: list[Path]
    output: Path
    steps: list[dict[str, Any]] = field(default_factory=list)
    encode: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    def render_options(self, *, dry_run: bool = False) -> RenderOptions:
        opts = RenderOptions(dry_run=dry_run)
        for key, value in self.encode.items():
            if not hasattr(opts, key):
                raise ConfigError(
                    f"encode 里有未知字段 '{key}'。"
                    "可用: crf, preset, video_codec, audio_codec, audio_bitrate, fps"
                )
            setattr(opts, key, value)
        return opts


def load(path: str | Path) -> Recipe:
    """从 YAML 文件加载 recipe。相对路径都以 recipe 文件所在目录为基准。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"找不到 recipe 文件: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"recipe 不是合法的 YAML ({path}): {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"recipe 顶层必须是一个字典: {path}")

    return from_dict(raw, base_dir=path.parent, source_path=path)


def from_dict(
    raw: dict[str, Any],
    *,
    base_dir: Path | None = None,
    source_path: Path | None = None,
) -> Recipe:
    base_dir = base_dir or Path.cwd()

    def resolve(p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (base_dir / p)

    inputs = _collect_inputs(raw, resolve)

    if "output" not in raw:
        raise ConfigError("recipe 缺少 output 字段")
    output = resolve(raw["output"])

    steps = raw.get("steps") or []
    if not isinstance(steps, list):
        raise ConfigError("steps 必须是一个列表")

    encode = raw.get("encode") or {}
    if not isinstance(encode, dict):
        raise ConfigError("encode 必须是一个字典")

    # 字幕等步骤里的路径也要相对 recipe 解析
    resolved_steps = []
    for step in steps:
        if isinstance(step, dict) and "file" in step:
            step = {**step, "file": str(resolve(step["file"]))}
        resolved_steps.append(step)

    return Recipe(
        name=str(raw.get("name") or (source_path.stem if source_path else "untitled")),
        inputs=inputs,
        output=output,
        steps=resolved_steps,
        encode=encode,
        source_path=source_path,
    )


def _collect_inputs(raw: dict[str, Any], resolve) -> list[Path]:
    """解析 input / inputs / input_dir 三种写法。"""
    if "input" in raw and "inputs" in raw:
        raise ConfigError("input 和 inputs 只能二选一")

    if "input" in raw:
        return [resolve(raw["input"])]

    if "inputs" in raw:
        values = raw["inputs"]
        if not isinstance(values, list) or not values:
            raise ConfigError("inputs 必须是非空列表")
        return [resolve(v) for v in values]

    if "input_dir" in raw:
        directory = resolve(raw["input_dir"])
        if not directory.is_dir():
            raise ConfigError(f"input_dir 不是目录: {directory}")
        found = sorted(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in _VIDEO_SUFFIXES
        )
        if not found:
            raise ConfigError(f"{directory} 里没有找到视频文件")
        return found

    raise ConfigError("recipe 需要 input、inputs 或 input_dir 之一")
