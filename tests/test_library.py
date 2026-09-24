"""素材库评估逻辑的测试。

这里只测纯计算部分，不跑 ffmpeg —— 指标提取本身在真实素材上验证过，
这里要锁住的是「什么该排除、什么不该排除」这类判断。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vedit.library import (
    ABSOLUTE_BLUR_FLOOR,
    FrameStat,
    Library,
    LibraryEntry,
    ShotCandidate,
    evaluate,
    pick_best_window,
    stats_in,
)


def flat_stats(count=40, *, sharp=3.0, motion=2.0, luma=128.0, step=0.5):
    return [
        FrameStat(time=i * step, luma=luma, low=30, high=220, sharp=sharp, motion=motion)
        for i in range(count)
    ]


def make(**kwargs) -> ShotCandidate:
    base = dict(source="a.mov", start=0.0, end=10.0, best_start=0.0,
                best_length=5.0, sharp=3.0, motion=2.0)
    base.update(kwargs)
    return ShotCandidate(**base)


# ---- 运动量分类 ----------------------------------------------------

@pytest.mark.parametrize(
    "motion,expected",
    [(0.3, "静止"), (2.0, "缓慢运镜"), (8.0, "明显运动"), (20.0, "剧烈晃动")],
)
def test_motion_kind(motion, expected):
    assert make(motion=motion).motion_kind == expected


# ---- 什么该被排除 --------------------------------------------------

def test_blurry_shot_is_flagged():
    shot = evaluate(make(sharp=ABSOLUTE_BLUR_FLOOR - 0.5), flat_stats())
    assert "虚焦" in shot.flags


def test_shaky_shot_is_flagged():
    shot = evaluate(make(motion=25.0), flat_stats(motion=25.0))
    assert "晃动" in shot.flags


def test_shallow_depth_closeup_is_not_flagged():
    """浅景深特写的清晰度天生偏低，但不能当虚焦排除。

    实测：大景深空镜 5.84、浅景深特写 2.73、轻微虚焦空镜 4.48。
    如果按绝对值一刀切，特写会被误杀，而特写正是品牌片最需要的。
    """
    shot = evaluate(make(sharp=2.73), flat_stats(sharp=2.73))
    assert "虚焦" not in shot.flags


def test_blown_highlights_do_not_block():
    """背景过曝成白是高调风格的刻意手法，不是缺陷。"""
    stats = [
        FrameStat(time=i * 0.5, luma=190, low=40, high=255, sharp=3.0, motion=1.0)
        for i in range(20)
    ]
    shot = evaluate(make(), stats)
    lib = Library(root="", entries=[LibraryEntry(
        path="a.mov", size=1, mtime=1, duration=10, width=1920, height=1080,
        fps=25, has_audio=True, shots=[shot],
    )])
    assert shot in lib.usable_shots()


def test_crushed_shadows_do_not_block():
    """暗调场景压黑也是风格选择。"""
    stats = [
        FrameStat(time=i * 0.5, luma=45, low=0, high=200, sharp=3.0, motion=1.0)
        for i in range(20)
    ]
    shot = evaluate(make(), stats)
    lib = Library(root="", entries=[LibraryEntry(
        path="a.mov", size=1, mtime=1, duration=10, width=1920, height=1080,
        fps=25, has_audio=True, shots=[shot],
    )])
    assert shot in lib.usable_shots()


def test_blurry_shot_is_excluded_from_usable():
    shot = evaluate(make(sharp=0.5), flat_stats(sharp=0.5))
    lib = Library(root="", entries=[LibraryEntry(
        path="a.mov", size=1, mtime=1, duration=10, width=1920, height=1080,
        fps=25, has_audio=True, shots=[shot],
    )])
    assert lib.usable_shots() == []


# ---- 入点推荐 ------------------------------------------------------

def test_best_window_prefers_sharper_section():
    """前半段糊、后半段实，推荐入点应落在后半段。"""
    stats = (
        [FrameStat(time=i * 0.5, sharp=1.0, motion=2.0, luma=128) for i in range(20)]
        + [FrameStat(time=10 + i * 0.5, sharp=6.0, motion=2.0, luma=128) for i in range(20)]
    )
    start, length, sharp, _ = pick_best_window(stats, 0.0, 20.0, length=5.0)
    assert start >= 9.0
    assert sharp > 4.0


def test_best_window_handles_short_shot():
    """镜头比要求的窗口还短时，整段拿走。"""
    stats = flat_stats(count=6)   # 3 秒
    start, length, _, _ = pick_best_window(stats, 0.0, 3.0, length=5.0)
    assert start == 0.0
    assert length == pytest.approx(3.0)


def test_best_window_without_stats_is_safe():
    start, length, sharp, motion = pick_best_window([], 2.0, 8.0, length=5.0)
    assert (start, length, sharp, motion) == (2.0, 5.0, 0.0, 0.0)


def test_stats_in_is_half_open():
    stats = flat_stats(count=10)          # t = 0.0 .. 4.5
    assert all(2.0 <= s.time < 4.0 for s in stats_in(stats, 2.0, 4.0))


# ---- 排序与持久化 --------------------------------------------------

def test_usable_shots_sorted_by_score_descending():
    shots = [evaluate(make(sharp=s), flat_stats(sharp=s)) for s in (2.0, 5.0, 3.0)]
    lib = Library(root="", entries=[LibraryEntry(
        path="a.mov", size=1, mtime=1, duration=10, width=1920, height=1080,
        fps=25, has_audio=True, shots=shots,
    )])
    scores = [s.score for s in lib.usable_shots()]
    assert scores == sorted(scores, reverse=True)


def test_library_round_trip(tmp_path):
    entry = LibraryEntry(
        path="a.mov", size=123, mtime=456.0, duration=10.0,
        width=1920, height=1080, fps=25.0, has_audio=True,
        shots=[evaluate(make(), flat_stats())],
    )
    lib = Library(root="/x", entries=[entry])
    loaded = Library.load(lib.save(tmp_path / "index.json"))
    assert loaded.root == "/x"
    assert len(loaded.all_shots) == 1
    assert loaded.all_shots[0].score == entry.shots[0].score


def test_fingerprint_changes_with_file():
    a = LibraryEntry(path="a", size=1, mtime=100.0, duration=1, width=1,
                     height=1, fps=1, has_audio=False)
    b = LibraryEntry(path="a", size=2, mtime=100.0, duration=1, width=1,
                     height=1, fps=1, has_audio=False)
    assert a.fingerprint != b.fingerprint
