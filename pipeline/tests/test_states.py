"""Tests for pipeline.states: which state or territory a point is in."""

import pytest

from pipeline.states import StateLookup


@pytest.fixture(scope="module")
def lookup():
    return StateLookup()


@pytest.mark.parametrize(
    ("lon", "lat", "state"),
    [
        (151.2153, -33.8568, "NSW"),  # Sydney Harbour
        (144.9631, -37.8136, "VIC"),  # Melbourne
        (149.1244, -35.3081, "ACT"),  # Canberra
        (153.0251, -27.4698, "QLD"),  # Brisbane
        (138.6007, -34.9285, "SA"),  # Adelaide
        (115.8605, -31.9505, "WA"),  # Perth
        (147.3272, -42.8821, "TAS"),  # Hobart
        (130.8456, -12.4634, "NT"),  # Darwin
        (133.8807, -23.6980, "NT"),  # Alice Springs
        (159.0800, -31.5500, "NSW"),  # Lord Howe Island
        (150.7500, -35.1500, "OT"),  # Jervis Bay Territory
        (167.9500, -29.0300, "OT"),  # Norfolk Island
    ],
)
def test_known_points(lookup, lon, lat, state):
    assert lookup.state_of(lon, lat) == state


def test_just_off_the_generalised_coast_takes_the_nearest_state(lookup):
    # About 2 km east of the cliffs at the entrance to Sydney Harbour.
    assert lookup.state_of(151.31, -33.835) == "NSW"


@pytest.mark.parametrize(
    ("lon", "lat"),
    [
        (155.0, -35.0),  # the Tasman Sea
        (151.5, -33.85),  # about 20 km off Sydney
        (174.7633, -36.8485),  # Auckland, not Australia
    ],
)
def test_offshore_is_in_no_state(lookup, lon, lat):
    assert lookup.state_of(lon, lat) is None
