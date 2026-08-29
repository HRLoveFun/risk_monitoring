import numpy as np
import pandas as pd
import pytest

from daily_holdings.parser import (
    _clean_num,
    _num1,
    _table_to_df,
    derive_nav,
    is_future,
    is_option,
    parse_as_of_date,
    parse_option_name,
    split_equities,
)


class TestCleanNum:
    def test_basic_number(self):
        s = pd.Series(["1,438,766,521.20", "6.49", "-20.14%"])
        result = _clean_num(s)
        assert result.tolist() == [1438766521.20, 6.49, -20.14]

    def test_na_values(self):
        s = pd.Series(["N/A", "n/a", "-", ""])
        result = _clean_num(s)
        assert result.isna().all()

    def test_hkd_prefix(self):
        s = pd.Series(["HKD$1,000.50"])
        result = _clean_num(s)
        assert result.tolist() == [1000.5]


class TestNum1:
    def test_extract_number(self):
        assert _num1("HKD 22,326,482,899.12") == 22326482899.12

    def test_none_input(self):
        assert _num1(None) is None

    def test_no_number(self):
        assert _num1("no numbers here") is None


class TestParseAsOfDate:
    def test_standard_format(self):
        html = '<p>As of 24 August 2026</p><div id="holdingsList">...'
        assert parse_as_of_date(html) is not None
        d = parse_as_of_date(html)
        assert d.year == 2026
        assert d.month == 8
        assert d.day == 24

    def test_abbreviated_month(self):
        html = '<p>As of 24 Aug 2026</p><div id="holdingsList">...'
        assert parse_as_of_date(html) is not None

    def test_no_date(self):
        assert parse_as_of_date("<p>No date here</p>") is None


class TestIsFuture:
    def test_future(self):
        assert is_future("HSCEI FUTURES 08/28/26") is True

    def test_not_future(self):
        assert is_future("CHINA CONSTRUCTION BANK") is False


class TestIsOption:
    def test_call(self):
        assert is_option("CALL HSCEI 08/28/26 C8700 OTC") is True

    def test_weekly_option(self):
        assert is_option("CALL HSCEI WEEKLY OPTION 08/07/26 8750") is True

    def test_put(self):
        assert is_option("PUT HSCEI 08/28/26 C8700") is True

    def test_not_option(self):
        assert is_option("CHINA CONSTRUCTION BANK") is False


class TestParseOptionName:
    def test_weekly_option(self):
        strike, expiry = parse_option_name("CALL HSCEI WEEKLY OPTION 08/07/26 8750")
        assert strike == 8750.0
        assert expiry is not None
        assert expiry.year == 2026
        assert expiry.month == 8
        assert expiry.day == 7

    def test_otc_option(self):
        strike, expiry = parse_option_name("CALL HSCEI 08/28/26 C8700 OTC")
        assert strike == 8700.0
        assert expiry is not None
        assert expiry.year == 2026
        assert expiry.month == 8
        assert expiry.day == 28

    def test_broken_c(self):
        strike, expiry = parse_option_name("CALL HSCEI 07/30/26 C770 0 OTC")
        assert strike == 7700.0

    def test_no_expiry(self):
        strike, expiry = parse_option_name("Short HSCEI WEEKLY OPTION 8,750 Call Option")
        assert strike == 8750.0
        assert expiry is None

    def test_none_input(self):
        strike, expiry = parse_option_name(None)
        assert strike is None
        assert expiry is None


class TestSplitEquities:
    def test_splits_correctly(self, full_holdings_df):
        equities, derivatives, unknown = split_equities(full_holdings_df)
        assert len(equities) == 2  # two equity rows
        assert len(derivatives) == 2  # two derivative rows
        assert unknown == []

    def test_mm_fund_as_equity(self, full_holdings_df):
        df = pd.concat(
            [
                full_holdings_df,
                pd.DataFrame(
                    [
                        {
                            "Name of Securities": "BNYM-USD LIQ-ADVANT",
                            "Exchange Ticker": "DRELIQP ID",
                            "Exchange": "",
                            "Market Price (in HKD)": 1.0,
                            "Number of Shares Held": 10000000,
                            "Market Value (in HKD)": 78393866.46,
                            "Net Assets (%)": 0.35,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
        equities, derivatives, unknown = split_equities(df)
        assert len(equities) == 3  # original 2 + BNYM money market fund
        bnym = equities[equities["Name of Securities"].astype(str).str.contains("BNYM")]
        assert len(bnym) == 1
        assert unknown == []


class TestDeriveNav:
    def test_derive(self, sample_equities):
        nav = derive_nav(sample_equities)
        assert nav is not None
        assert nav > 0

    def test_empty(self):
        df = pd.DataFrame(columns=["Market Value (in HKD)", "Net Assets (%)"])
        assert derive_nav(df) is None


class TestTableToDf:
    def test_basic_table(self):
        from bs4 import BeautifulSoup

        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        soup = BeautifulSoup(html, "html.parser")
        df = _table_to_df(soup.find("table"))
        assert list(df.columns) == ["A", "B"]
        assert df.iloc[0, 0] == "1"
        assert df.iloc[0, 1] == "2"
