import math

import pytest

from daily_holdings.positions import (
    _norm_cdf,
    bs_call_delta,
    bs_call_price,
    implied_vol,
)


class TestNormCdf:
    def test_basic(self):
        result = _norm_cdf(0)
        assert abs(result - 0.5) < 1e-10

    def test_large_positive(self):
        assert _norm_cdf(10) > 0.9999999

    def test_large_negative(self):
        assert _norm_cdf(-10) < 0.0000001


class TestBsCallPrice:
    def test_at_expiry(self):
        # T=0 should return intrinsic value
        assert bs_call_price(100, 90, 0, 0.3) == 10.0
        assert bs_call_price(100, 110, 0, 0.3) == 0.0

    def test_positive_price(self):
        price = bs_call_price(100, 90, 1.0, 0.3)
        assert price > 10.0  # should be above intrinsic value

    def test_zero_vol(self):
        price = bs_call_price(100, 90, 1.0, 0.0)
        assert price == 10.0


class TestBsCallDelta:
    def test_deep_itm(self):
        delta = bs_call_delta(100, 50, 1.0, 0.3)
        assert delta > 0.99

    def test_deep_otm(self):
        # Need more extreme OTM for delta < 0.01
        delta = bs_call_delta(100, 200, 0.1, 0.2)
        assert delta < 0.01

    def test_atm(self):
        delta = bs_call_delta(100, 100, 1.0, 0.3)
        assert 0.4 < delta < 0.6

    def test_zero_vol(self):
        assert bs_call_delta(100, 90, 1.0, 0.0) == 1.0
        assert bs_call_delta(100, 110, 1.0, 0.0) == 0.0

    def test_invalid_inputs(self):
        assert bs_call_delta(-1, 90, 1.0, 0.3) is None
        assert bs_call_delta(100, -1, 1.0, 0.3) is None


class TestImpliedVol:
    def test_normal_case(self):
        s, k, t = 100.0, 100.0, 1.0
        sigma = 0.3
        price = bs_call_price(s, k, t, sigma)
        iv = implied_vol(price, s, k, t)
        assert iv is not None
        assert abs(iv - sigma) < 0.01

    def test_none_inputs(self):
        assert implied_vol(None, 100, 100, 1) is None
        assert implied_vol(10, None, 100, 1) is None

    def test_out_of_bounds(self):
        # price below intrinsic value
        iv = implied_vol(5.0, 100, 90, 1.0)
        assert iv is None

    def test_zero_days(self):
        iv = implied_vol(10.0, 100, 90, 0.0)
        assert iv is None


def test_bs_consistency():
    """BS price and delta should be internally consistent."""
    s, k, t, sigma = 100.0, 105.0, 0.5, 0.25
    price = bs_call_price(s, k, t, sigma)
    delta = bs_call_delta(s, k, t, sigma)
    assert 0 < delta < 1
    assert price > 0
    # Verify: price should increase with sigma
    price_higher = bs_call_price(s, k, t, sigma + 0.05)
    assert price_higher > price
