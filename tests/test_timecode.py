from fractions import Fraction

import pytest

from media_ingest.timecode import format_rate, frames_to_timecode, parse_rate, samples_to_timecode


@pytest.mark.parametrize("text, rate", [("25", Fraction(25)), ("30000/1001", Fraction(30000, 1001)),
                                        ("23.976", Fraction(23976, 1000)), (24, Fraction(24))])
def test_parse_rate(text, rate):
    assert parse_rate(text) == rate


@pytest.mark.parametrize("text", [None, "", "0/0", "abc", "-25", "0"])
def test_parse_rate_rejects(text):
    assert parse_rate(text) is None


@pytest.mark.parametrize("rate, shown", [("25", "25"), ("24000/1001", "23.976"), ("30000/1001", "29.97"),
                                         ("60000/1001", "59.94"), ("50", "50")])
def test_format_rate(rate, shown):
    assert format_rate(parse_rate(rate)) == shown


def test_non_drop():
    assert frames_to_timecode(0, 25) == "00:00:00:00"
    assert frames_to_timecode(25 * 3600 + 24, 25) == "01:00:00:24"
    assert frames_to_timecode(24 * 3600 * 25, 25) == "00:00:00:00"  # wraps at 24h


@pytest.mark.parametrize("frames, expected", [
    (1799, "00:00:59;29"),
    (1800, "00:01:00;02"),  # frames ;00 and ;01 are dropped
    (17982, "00:10:00;00"),  # but not at ten-minute marks
    (107892, "01:00:00;00"),  # one real hour
])
def test_drop_frame_29_97(frames, expected):
    assert frames_to_timecode(frames, 30, drop_frame=True) == expected


def test_drop_frame_59_94():
    assert frames_to_timecode(3600, 60, drop_frame=True) == "00:01:00;04"


def test_drop_frame_needs_30_or_60():
    with pytest.raises(ValueError):
        frames_to_timecode(10, 25, drop_frame=True)


def test_bwf_samples():
    one_hour = 3600 * 48000
    assert samples_to_timecode(one_hour, 48000, Fraction(25)) == "01:00:00:00"
    assert samples_to_timecode(one_hour, 48000, Fraction(30000, 1001), drop_frame=True) == "01:00:00;00"
    assert samples_to_timecode(one_hour, 48000, Fraction(30000, 1001)) == "00:59:56:12"
    assert samples_to_timecode(one_hour + 1919, 48000, Fraction(25)) == "01:00:00:00"  # rounds down
    assert samples_to_timecode(one_hour + 1920, 48000, Fraction(25)) == "01:00:00:01"
