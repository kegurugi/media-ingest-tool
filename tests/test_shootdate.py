from datetime import date, datetime

from media_ingest.shootdate import propose_shoot_date


def test_single_day():
    p = propose_shoot_date([datetime(2026, 10, 12, 9, 0), datetime(2026, 10, 12, 17, 30)])
    assert p.proposed == date(2026, 10, 12)
    assert not p.spans_midnight


def test_card_spanning_midnight_proposes_earliest_and_flags():
    p = propose_shoot_date([datetime(2026, 10, 13, 0, 40), datetime(2026, 10, 12, 23, 10)])
    assert p.proposed == date(2026, 10, 12)
    assert p.dates == [date(2026, 10, 12), date(2026, 10, 13)]
    assert p.spans_midnight


def test_no_dates_means_no_proposal():
    p = propose_shoot_date([])
    assert p.proposed is None
    assert not p.spans_midnight
