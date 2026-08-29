from datetime import date
from typing import Any

import pandas as pd
import pytest


@pytest.fixture
def sample_equities() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Name of Securities": ["CHINA CONSTRUCTION BANK", "TENCENT HOLDINGS"],
            "Exchange Ticker": ["939 HK", "700 HK"],
            "Exchange": ["Hong Kong", "Hong Kong"],
            "Market Price (in HKD)": [9.175, 440.0],
            "Number of Shares Held": [157479507, 3066592],
            "Market Value (in HKD)": [1444874476.73, 1349300480.0],
            "Net Assets (%)": [6.49, 6.06],
        }
    )


@pytest.fixture
def sample_derivatives() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Name of Securities": [
                "CALL HSCEI 08/28/26 C8700 OTC",
                "HSCEI FUTURES 08/28/26",
            ],
            "Exchange Ticker": ["", "HCQ6"],
            "Exchange": ["Hong Kong", "Hong Kong"],
            "Market Price (in HKD)": [8.0, 8444.0],
            "Number of Shares Held": [-8500, 9665],
            "Market Value (in HKD)": [-3400000.0, -4729500.0],
            "Net Assets (%)": [-0.02, -0.02],
        }
    )


@pytest.fixture
def full_holdings_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Name of Securities": [
                "CHINA CONSTRUCTION BANK",
                "TENCENT HOLDINGS",
                "CALL HSCEI 08/28/26 C8700 OTC",
                "HSCEI FUTURES 08/28/26",
            ],
            "Exchange Ticker": ["939 HK", "700 HK", "", "HCQ6"],
            "Exchange": ["Hong Kong", "Hong Kong", "Hong Kong", "Hong Kong"],
            "Market Price (in HKD)": [9.175, 440.0, 8.0, 8444.0],
            "Number of Shares Held": [157479507, 3066592, -8500, 9665],
            "Market Value (in HKD)": [1444874476.73, 1349300480.0, -3400000.0, -4729500.0],
            "Net Assets (%)": [6.49, 6.06, -0.02, -0.02],
        }
    )


@pytest.fixture
def sample_html() -> str:
    return """
    <html><body>
    <p>As of 24 August 2026</p>
    <table class="holdings">
    <tr><th>Name of Securities</th><th>Exchange Ticker</th>
    <th>Market Price (in HKD)</th><th>Number of Shares Held</th>
    <th>Market Value (in HKD)</th><th>Net Assets (%)</th></tr>
    <tr><td>CHINA CONSTRUCTION BANK</td><td>939 HK</td><td>9.175</td>
    <td>157479507</td><td>1444874476.73</td><td>6.49</td></tr>
    <tr><td>TENCENT HOLDINGS</td><td>700 HK</td><td>440.0</td>
    <td>3066592</td><td>1349300480.0</td><td>6.06</td></tr>
    </table>
    <table class="holdings">
    <tr><th>Option Position</th><th>Notional Exposure to NAV (%)</th></tr>
    <tr><td>Short HSCEI WEEKLY OPTION 8,750 Call Option</td><td>-98.08</td></tr>
    </table>
    <tr><th>Total Net Asset Value of the Fund</th><td>HKD 22,379,711,265</td></tr>
    <tr><th>Closing level of Hang Seng China Enterprises</th><td>8,490.31</td></tr>
    </body></html>
    """


@pytest.fixture
def sample_meta() -> dict[str, Any]:
    return {
        "as_of": "2026-08-24",
        "nav": 22379711265.0,
        "nav_page": 22379711265.0,
        "index_close": 8490.31,
    }
