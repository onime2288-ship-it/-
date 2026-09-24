"""时间码解析的测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vedit.errors import ConfigError
from vedit.timecode import format_time, parse_span, parse_time


@pytest.mark.parametrize(
    "value,expected",
    [
        (83.5, 83.5),
        ("83.5", 83.5),
        ("1:23.5", 83.5),
        ("00:01:23.5", 83.5),
        ("1:00:00", 3600.0),
        (0, 0.0),
    ],
)
def test_parse_time(value, expected):
    assert parse_time(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["", "abc", "1:2:3:4", -5, "-1:00"])
def test_parse_time_rejects_garbage(value):
    with pytest.raises(ConfigError):
        parse_time(value)


@pytest.mark.parametrize(
    "value",
    ["1:00-1:30", [60, 90], (60, 90), {"start": "1:00", "end": "1:30"}],
)
def test_parse_span_accepts_all_forms(value):
    assert parse_span(value) == (60.0, 90.0)


@pytest.mark.parametrize("value", ["1:30-1:00", [90, 60], [1, 2, 3], {"start": 1}])
def test_parse_span_rejects_invalid(value):
    with pytest.raises(ConfigError):
        parse_span(value)


def test_format_time():
    assert format_time(3723.456) == "01:02:03.456"
    assert format_time(0) == "00:00:00.000"


def test_format_time_rounds_millis_without_overflow():
    # .9999 秒不能格式化成 :000 之外的非法值
    assert format_time(1.9999) == "00:00:02.000"
