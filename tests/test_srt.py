"""字幕解析与对轴的测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vedit import srt
from vedit.errors import ConfigError
from vedit.timeline import Timeline

SAMPLE = """1
00:00:00,000 --> 00:00:05,000
第一句

2
00:00:12,000 --> 00:00:18,000
第二句

3
00:00:25,000 --> 00:00:30,000
第三句
"""


@pytest.fixture
def sample_srt(tmp_path):
    path = tmp_path / "raw.srt"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_parse_reads_all_cues(sample_srt):
    cues = srt.parse(sample_srt)
    assert len(cues) == 3
    assert cues[0].start == 0.0
    assert cues[0].end == 5.0
    assert cues[1].text == "第二句"


def test_parse_handles_bom_and_dot_separator(tmp_path):
    path = tmp_path / "bom.srt"
    path.write_text(
        "﻿1\n00:00:01.500 --> 00:00:02.500\nhi\n", encoding="utf-8"
    )
    cues = srt.parse(path)
    assert cues[0].start == 1.5


def test_parse_handles_multiline_text(tmp_path):
    path = tmp_path / "multi.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:02,000\n上行\n下行\n", encoding="utf-8")
    assert srt.parse(path)[0].text == "上行\n下行"


def test_parse_rejects_file_without_cues(tmp_path):
    path = tmp_path / "junk.srt"
    path.write_text("这不是字幕\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        srt.parse(path)


def test_retime_shifts_later_cues(sample_srt):
    cues = srt.parse(sample_srt)
    timeline = Timeline.full("a.mp4", 40).remove([(6, 10)])   # 剪掉 4 秒
    out = srt.retime(cues, timeline)
    assert len(out) == 3
    assert out[0].start == 0.0          # 剪切点之前，不受影响
    assert out[1].start == 8.0          # 12 - 4
    assert out[2].start == 21.0         # 25 - 4


def test_retime_drops_cue_inside_cut(sample_srt):
    cues = srt.parse(sample_srt)
    timeline = Timeline.full("a.mp4", 40).remove([(11, 19)])  # 第二句整条被剪
    out = srt.retime(cues, timeline)
    assert [c.text for c in out] == ["第一句", "第三句"]


def test_retime_accounts_for_speed(sample_srt):
    cues = srt.parse(sample_srt)
    timeline = Timeline.full("a.mp4", 40).with_speed(2.0)
    out = srt.retime(cues, timeline)
    assert out[1].start == 6.0          # 12 / 2


def test_retime_snaps_partially_cut_cue(sample_srt):
    cues = srt.parse(sample_srt)
    # 剪掉 14-16，正好落在第二句 (12-18) 中间，这条应该保留并压缩
    timeline = Timeline.full("a.mp4", 40).remove([(14, 16)])
    out = srt.retime(cues, timeline)
    assert len(out) == 3
    assert out[1].end - out[1].start == pytest.approx(4.0)


def test_write_round_trip(tmp_path, sample_srt):
    cues = srt.parse(sample_srt)
    out_path = srt.write(cues, tmp_path / "out.srt")
    assert [c.text for c in srt.parse(out_path)] == [c.text for c in cues]


def test_write_renumbers_sequentially(tmp_path):
    cues = [srt.Cue(99, 0, 1, "a"), srt.Cue(42, 2, 3, "b")]
    content = srt.write(cues, tmp_path / "o.srt").read_text(encoding="utf-8")
    assert content.startswith("1\n")
    assert "\n2\n" in content
