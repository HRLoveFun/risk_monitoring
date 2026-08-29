import json
import os
import tempfile
from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from daily_holdings.snapshot import (
    _norm_label,
    _num_series,
    diff_vs_previous,
    load_previous,
    save_snapshot,
)


class TestDiffVsPrevious:
    def test_no_previous(self, full_holdings_df):
        diff = diff_vs_previous(None, None, full_holdings_df, {"nav": 1e9, "index_close": 100.0})
        assert diff["has_prev"] is False
        # No previous snapshot means all current equities are "added"
        assert len(diff["equities"]["added"]) == 2
        # Metrics still computed; prev is None when no prior meta
        assert len(diff["metrics"]) == 5
        assert diff["metrics"][0]["prev"] is None

    def test_empty_current(self):
        diff = diff_vs_previous(pd.DataFrame(), None, pd.DataFrame(), {"nav": 1e9})
        assert diff["has_prev"] is False

    def test_equity_changes(self, full_holdings_df):
        # Create a previous snapshot with different weights
        prev = full_holdings_df.copy()
        prev.loc[prev["Exchange Ticker"] == "939 HK", "Net Assets (%)"] = 5.0
        cur = full_holdings_df.copy()
        diff = diff_vs_previous(prev, None, cur, {"nav": 1e9, "index_close": 100.0})
        assert len(diff["equities"]["weight_changes"]) == 1
        assert diff["equities"]["weight_changes"][0]["delta"] > 0

    def test_futures_diff(self):
        prev_data = {
            "Name of Securities": ["HSCEI FUTURES 08/28/26"],
            "Exchange Ticker": ["HCQ6"],
            "Market Price (in HKD)": [8000.0],
            "Number of Shares Held": [10000],
            "Market Value (in HKD)": [-4000000.0],
            "Net Assets (%)": [-0.02],
        }
        cur_data = {
            "Name of Securities": ["HSCEI FUTURES 08/28/26"],
            "Exchange Ticker": ["HCQ6"],
            "Market Price (in HKD)": [8444.0],
            "Number of Shares Held": [9665],
            "Market Value (in HKD)": [-4729500.0],
            "Net Assets (%)": [-0.02],
        }
        prev = pd.DataFrame(prev_data)
        cur = pd.DataFrame(cur_data)
        diff = diff_vs_previous(prev, None, cur, {"nav": 1e9, "index_close": 8490.31})
        assert diff["futures"]["rows_prev"] == 1
        assert diff["futures"]["rows_cur"] == 1
        assert len(diff["futures"]["changed"]) == 1
        assert diff["futures"]["changed"][0]["prev"] == 10000.0
        assert diff["futures"]["changed"][0]["cur"] == 9665.0

    def test_options_contracts_abs(self):
        prev_data = {
            "Name of Securities": ["CALL HSCEI 08/28/26 C8700 OTC"],
            "Exchange Ticker": [""],
            "Market Price (in HKD)": [8.0],
            "Number of Shares Held": [-8500],
            "Market Value (in HKD)": [-3400000.0],
            "Net Assets (%)": [-0.02],
        }
        cur_data = {
            "Name of Securities": ["CALL HSCEI 08/28/26 C8700 OTC"],
            "Exchange Ticker": [""],
            "Market Price (in HKD)": [8.0],
            "Number of Shares Held": [-11500],
            "Market Value (in HKD)": [-4600000.0],
            "Net Assets (%)": [-0.02],
        }
        prev = pd.DataFrame(prev_data)
        cur = pd.DataFrame(cur_data)
        diff = diff_vs_previous(prev, None, cur, {"nav": 1e9, "index_close": 8490.31})
        assert diff["options"]["rows_prev"] == 1
        assert diff["options"]["rows_cur"] == 1
        assert diff["options"]["changed"][0]["prev"] == -8500.0
        assert diff["options"]["changed"][0]["cur"] == -11500.0

    def test_metrics(self, full_holdings_df):
        prev = full_holdings_df.copy()
        meta = {"nav": 22379711265.0, "index_close": 8400.0}
        diff = diff_vs_previous(
            prev, meta, full_holdings_df, {"nav": 22379711265.0, "index_close": 8490.31}
        )
        assert len(diff["metrics"]) == 5
        assert diff["metrics"][0]["label"] == "基金净值 NAV(HKD)"
        assert diff["metrics"][0]["prev"] == 22379711265.0


class TestNormLabel:
    def test_normalize(self):
        assert _norm_label("hello  world") == "hello world"
        assert _norm_label("  spaces  ") == "spaces"
        assert _norm_label(None) == ""


class TestNumSeries:
    def test_existing_column(self):
        df = pd.DataFrame({"col": ["1", "2", "3"]})
        result = _num_series(df, "col")
        assert result.tolist() == [1.0, 2.0, 3.0]

    def test_missing_column(self):
        df = pd.DataFrame({"a": [1]})
        result = _num_series(df, "col")
        assert len(result) == 1
        assert result.iloc[0] is None


class TestSaveSnapshot:
    def test_save_and_load(self, tmp_path):
        df = pd.DataFrame({"a": [1, 2, 3]})
        as_of = date(2026, 8, 24)
        with patch("daily_holdings.snapshot.DATA_DIR", tmp_path):
            path = save_snapshot(df, as_of, meta={"nav": 1e9})
            assert path.endswith("holdings_20260824.csv")
            assert os.path.exists(path)
            meta_path = path.replace(".csv", ".meta.json")
            assert os.path.exists(meta_path)
            with open(meta_path) as f:
                meta = json.load(f)
            assert meta["nav"] == 1e9

            loaded = pd.read_csv(path)
            assert len(loaded) == 3

    def test_load_previous_finds_prev(self, tmp_path):
        df1 = pd.DataFrame({"a": [1]})
        df2 = pd.DataFrame({"a": [2]})
        as_of = date(2026, 8, 24)
        prev_as_of = date(2026, 8, 23)
        with patch("daily_holdings.snapshot.DATA_DIR", tmp_path):
            save_snapshot(df1, prev_as_of)
            save_snapshot(df2, as_of)
            prev, prev_meta = load_previous(as_of)
            assert prev is not None
            assert len(prev) == 1
            assert prev.iloc[0, 0] == 1
