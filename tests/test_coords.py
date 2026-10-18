"""Coordinate mapping: technician viewport -> customer desktop pixels."""

from __future__ import annotations

import pytest

from app.customer import CustomerSession

GEOM = {"left": 0, "top": 0, "width": 1280, "height": 800}


def test_corners():
    assert CustomerSession._map_point(0, 0, GEOM) == (0, 0)
    assert CustomerSession._map_point(1, 1, GEOM) == (1279, 799)
    assert CustomerSession._map_point(0.5, 0.5, GEOM) == (640, 400)


def test_scaled_video_maps_to_full_desktop():
    # Technician window resizes the video arbitrarily; normalized coords must
    # always land on the same desktop pixel.
    for x, y in [(0.25, 0.75), (0.1, 0.9), (0.99, 0.01)]:
        px, py = CustomerSession._map_point(x, y, GEOM)
        assert 0 <= px < 1280 and 0 <= py < 800
        assert px == round(x * 1279)
        assert py == round(y * 799)


def test_offset_root_origin():
    geom = {"left": 100, "top": 50, "width": 1280, "height": 800}
    assert CustomerSession._map_point(0, 0, geom) == (100, 50)
    assert CustomerSession._map_point(1, 1, geom) == (100 + 1279, 50 + 799)


def test_clamping():
    assert CustomerSession._map_point(-0.5, 1.7, GEOM) == (0, 799)
    assert CustomerSession._map_point(2.0, -3.0, GEOM) == (1279, 0)


def test_invalid_coordinates():
    with pytest.raises(ValueError):
        CustomerSession._map_point("x", 0.5, GEOM)
    with pytest.raises(ValueError):
        CustomerSession._map_point(None, None, GEOM)
