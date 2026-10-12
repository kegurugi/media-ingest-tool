from datetime import date

import pytest

from media_ingest.errors import NamingError
from media_ingest.naming import CardName, date_folder_name, parse_date_folder

OCT12 = date(2026, 10, 12)


def test_format_single_letter_camera():
    assert str(CardName("ABC", OCT12, "A", 1)) == "ABC261012A01"


def test_format_three_char_camera():
    assert str(CardName("ABC", OCT12, "DR1", 2)) == "ABC261012DR102"


def test_constructor_normalises_case():
    assert str(CardName("abc", OCT12, "dr1", 2)) == "ABC261012DR102"


@pytest.mark.parametrize(
    "name", ["ABC261012A01", "ABC261012DR102", "ABC261012S01", "XYZ270101T99", "ABC261012105"]
)
def test_round_trip(name):
    assert str(CardName.parse(name)) == name


def test_parse_splits_fields_by_position():
    card = CardName.parse("ABC261012DR102")
    assert (card.show, card.shoot_date, card.cam, card.reel) == ("ABC", OCT12, "DR1", 2)


def test_parse_numeric_camera():
    card = CardName.parse("ABC261012105")
    assert (card.cam, card.reel) == ("1", 5)


def test_parse_is_case_insensitive():
    assert CardName.parse("abc261012dr102") == CardName("ABC", OCT12, "DR1", 2)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "ABC261012A00",  # reel 00
        "ABC261332A01",  # month 13
        "ABC260230A01",  # Feb 30
        "AB261012A01",  # 2-letter show
        "AB1261012A01",  # digit in show
        "ABC261012ABCD01",  # 4-char camera
        "ABC26101201",  # no camera
        "ABC261012A1",  # 1-digit reel
        "ABC261012A-01",
        "ABC261012A01_old",
    ],
)
def test_parse_rejects(name):
    with pytest.raises(NamingError):
        CardName.parse(name)


@pytest.mark.parametrize("reel", [0, 100, -1])
def test_reel_out_of_range(reel):
    with pytest.raises(NamingError):
        CardName("ABC", OCT12, "A", reel)


@pytest.mark.parametrize("cam", ["", "A_", "ABCD"])
def test_bad_camera(cam):
    with pytest.raises(NamingError):
        CardName("ABC", OCT12, cam, 1)


def test_date_outside_century():
    with pytest.raises(NamingError):
        CardName("ABC", date(1999, 12, 31), "A", 1)


def test_date_folder_name():
    assert date_folder_name(OCT12) == "10122026"
    assert date_folder_name(date(2027, 1, 5)) == "01052027"


def test_parse_date_folder():
    assert parse_date_folder("10122026") == OCT12


@pytest.mark.parametrize("name", ["13012026", "02302026", "1012026", "2026-10-12", "261012"])
def test_parse_date_folder_rejects(name):
    with pytest.raises(NamingError):
        parse_date_folder(name)
