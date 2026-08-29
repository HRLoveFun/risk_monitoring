import pytest

from daily_holdings.report import build_summary


@pytest.fixture
def minimal_data():
    import datetime

    import pandas as pd

    return {
        "nav": 22379711265.0,
        "index_close": 8490.31,
        "equities": pd.DataFrame(
            {
                "Name of Securities": ["STOCK A", "STOCK B", "STOCK C"],
                "Exchange Ticker": ["1 HK", "2 HK", "3 HK"],
                "Net Assets (%)": [10.0, 5.0, 3.0],
                "Market Value (in HKD)": [100.0, 50.0, 30.0],
            }
        ),
        "options": pd.DataFrame(),
        "futures_legs": [],
        "option_legs": [],
        "dropped_legs": [],
        "as_of": datetime.date(2026, 8, 24),
    }


class TestBuildSummary:
    def test_subject_no_blocking(self, minimal_data):
        diff = {
            "has_prev": False,
            "equities": {"added": [], "removed": [], "weight_changes": []},
            "futures": {"added": [], "removed": [], "changed": [], "rows_prev": 0, "rows_cur": 0},
            "options": {
                "added": [],
                "removed": [],
                "changed": [],
                "rows_prev": 0,
                "rows_cur": 0,
                "contracts_abs_prev": None,
                "contracts_abs_cur": None,
            },
            "metrics": [],
        }
        pos = {
            "equity_pct": 50.0,
            "futures_pct": 10.0,
            "option_pressure_pct": -5.0,
            "option_notional_pct": -8.0,
            "option_notional_pct_page": None,
            "cross_check_gap": None,
            "iv_avg": 0.2,
            "iv_all_from_market": True,
            "strike_distances": [],
            "dropped_legs": [],
        }
        subject, body = build_summary(minimal_data, diff, positions=pos)
        assert "20260824" in subject
        assert "持仓" in subject
        assert "【待补全】" not in subject
        assert "【更新】" not in subject
        assert "前五大重仓占比 18.00%" in body

    def test_subject_with_blocking(self, minimal_data):
        diff = {
            "has_prev": False,
            "equities": {"added": [], "removed": [], "weight_changes": []},
            "futures": {"added": [], "removed": [], "changed": [], "rows_prev": 0, "rows_cur": 0},
            "options": {
                "added": [],
                "removed": [],
                "changed": [],
                "rows_prev": 0,
                "rows_cur": 0,
                "contracts_abs_prev": None,
                "contracts_abs_cur": None,
            },
            "metrics": [],
        }
        subject, body = build_summary(minimal_data, diff, blocking=["期权腿缺失"])
        assert "【待补全】" in subject
        assert "期权腿缺失" in body

    def test_subject_update_mode(self, minimal_data):
        diff = {
            "has_prev": False,
            "equities": {"added": [], "removed": [], "weight_changes": []},
            "futures": {"added": [], "removed": [], "changed": [], "rows_prev": 0, "rows_cur": 0},
            "options": {
                "added": [],
                "removed": [],
                "changed": [],
                "rows_prev": 0,
                "rows_cur": 0,
                "contracts_abs_prev": None,
                "contracts_abs_cur": None,
            },
            "metrics": [],
        }
        subject, body = build_summary(minimal_data, diff, update_mode=True)
        assert "【更新】" in subject

    def test_top5(self, minimal_data):
        diff = {
            "has_prev": False,
            "equities": {"added": [], "removed": [], "weight_changes": []},
            "futures": {"added": [], "removed": [], "changed": [], "rows_prev": 0, "rows_cur": 0},
            "options": {
                "added": [],
                "removed": [],
                "changed": [],
                "rows_prev": 0,
                "rows_cur": 0,
                "contracts_abs_prev": None,
                "contracts_abs_cur": None,
            },
            "metrics": [],
        }
        pos = {
            "equity_pct": 50.0,
            "futures_pct": 10.0,
            "option_pressure_pct": -5.0,
            "option_notional_pct": -8.0,
            "option_notional_pct_page": None,
            "cross_check_gap": None,
            "iv_avg": 0.2,
            "iv_all_from_market": True,
            "strike_distances": [],
            "dropped_legs": [],
        }
        _, body = build_summary(minimal_data, diff, positions=pos)
        assert "STOCK A" in body
        assert "STOCK B" in body
        assert "STOCK C" in body
