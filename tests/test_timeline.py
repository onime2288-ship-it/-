"""时间线运算的测试 —— 这是整个工作流最容易出错的地方。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vedit.errors import EmptyTimeline
from vedit.timeline import Segment, Timeline, invert_spans, normalize_spans


def test_remove_splits_segment_in_middle():
    t = Timeline.full("a.mp4", 100).remove([(40, 60)])
    assert [(s.start, s.end) for s in t.segments] == [(0, 40), (60, 100)]


def test_remove_multiple_spans():
    t = Timeline.full("a.mp4", 100).remove([(10, 20), (50, 60)])
    assert [(s.start, s.end) for s in t.segments] == [(0, 10), (20, 50), (60, 100)]


def test_remove_overlapping_spans():
    t = Timeline.full("a.mp4", 100).remove([(10, 30), (20, 40)])
    assert [(s.start, s.end) for s in t.segments] == [(0, 10), (40, 100)]


def test_remove_at_boundaries():
    t = Timeline.full("a.mp4", 100).remove([(0, 10), (90, 100)])
    assert [(s.start, s.end) for s in t.segments] == [(10, 90)]


def test_remove_everything_yields_empty():
    t = Timeline.full("a.mp4", 100).remove([(0, 100)])
    assert t.is_empty
    with pytest.raises(EmptyTimeline):
        t.require_content("测试")


def test_remove_discards_subframe_slivers():
    # 挖掉 0-99.99 之后只剩 0.01 秒，短于 MIN_SEGMENT，应该被丢掉
    t = Timeline.full("a.mp4", 100).remove([(0, 99.99)])
    assert t.is_empty


def test_keep_only_is_inverse_of_remove():
    t = Timeline.full("a.mp4", 100).keep_only([(20, 30), (70, 80)])
    assert [(s.start, s.end) for s in t.segments] == [(20, 30), (70, 80)]


def test_pad_widens_and_merges():
    t = Timeline.full("a.mp4", 100).remove([(10, 11)]).pad(1.0)
    # 1 秒的缝隙被 1 秒的 padding 从两侧填满，两段应该合回一段
    assert len(t.segments) == 1
    assert (t.segments[0].start, t.segments[0].end) == (0, 100)


def test_pad_clamps_to_bounds():
    t = Timeline.full("a.mp4", 100).remove([(50, 60)]).pad(5.0)
    assert t.segments[0].start == 0.0
    assert t.segments[-1].end == 100.0


def test_speed_changes_output_duration_not_source():
    t = Timeline.full("a.mp4", 100).with_speed(2.0)
    assert t.source_duration == 100
    assert t.output_duration == 50


def test_speed_compounds():
    t = Timeline.full("a.mp4", 100).with_speed(2.0).with_speed(2.0)
    assert t.segments[0].speed == 4.0


def test_drop_shorter_than():
    t = Timeline("a.mp4", 100, [Segment(0, 1), Segment(10, 30)])
    assert len(t.drop_shorter_than(5).segments) == 1


def test_map_time_within_kept_segment():
    t = Timeline.full("a.mp4", 100).remove([(10, 20)])
    assert t.map_time(5) == 5
    assert t.map_time(25) == 15   # 前面剪掉了 10 秒


def test_map_time_in_gap_returns_none():
    t = Timeline.full("a.mp4", 100).remove([(10, 20)])
    assert t.map_time(15) is None


def test_map_time_accounts_for_speed():
    t = Timeline.full("a.mp4", 100).with_speed(2.0)
    assert t.map_time(50) == 25


def test_snap_time_never_returns_none():
    t = Timeline.full("a.mp4", 100).remove([(10, 20)])
    assert t.snap_time(15) == 10.0    # 吸附到缝隙前的边界
    assert t.snap_time(500) == 90.0   # 超出末尾则吸附到片尾


def test_serialization_round_trip():
    t = Timeline.full("a.mp4", 100).remove([(10, 20)]).with_speed(1.5)
    assert Timeline.from_dict(t.to_dict()).segments == t.segments


def test_segment_rejects_reversed_range():
    with pytest.raises(ValueError):
        Segment(10, 5)


def test_segment_rejects_nonpositive_speed():
    with pytest.raises(ValueError):
        Segment(0, 10, speed=0)


def test_normalize_merges_adjacent():
    assert normalize_spans([(0, 10), (10, 20)]) == [(0, 20)]


def test_normalize_drops_empty_spans():
    assert normalize_spans([(5, 5), (0, 10)]) == [(0, 10)]


def test_invert_spans():
    assert invert_spans([(10, 20)], 100) == [(0, 10), (20, 100)]
    assert invert_spans([], 100) == [(0, 100)]
    assert invert_spans([(0, 100)], 100) == []
