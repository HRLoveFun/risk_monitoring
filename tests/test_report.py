import datetime

import pandas as pd
import pytest

from daily_holdings.report import build_report


def _empty_diff() -> dict:
    return {
        "has_prev": False,
        "base_date": None,
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


@pytest.fixture
def minimal_data():
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


@pytest.fixture
def minimal_positions():
    return {
        "nav": 22379711265.0,
        "index_close": 8490.31,
        "equity_pct": 50.0,
        "futures_pct": 10.0,
        "option_pressure_pct": -5.0,
        "option_notional_pct": -8.0,
        "option_notional_pct_page": None,
        "cross_check_gap": None,
        "iv_avg": 0.2,
        "iv_all_from_market": True,
        "strike_distances": [],
        "option_rows": [],
        "futures_rows": [],
        "dropped_legs": [],
    }


class TestBuildSummary:
    def test_subject_no_blocking(self, minimal_data, minimal_positions):
        subject, body, _text = build_report(minimal_data, _empty_diff(), positions=minimal_positions)
        assert "2026-08-24" in subject
        assert "持仓" in subject
        assert "【待补全】" not in subject
        assert "【更新】" not in subject
        assert "前五大重仓占比 18.00%" in body
        # section subtitle carries index level and as-of date
        assert "指数现价 8,490.31, 2026-08-24" in body
        # section headings are underlined
        assert "text-decoration:underline" in body

    def test_subject_with_blocking(self, minimal_data, minimal_positions):
        subject, body, _text = build_report(
            minimal_data, _empty_diff(), positions=minimal_positions, blocking=["期权腿缺失"]
        )
        assert "【待补全】" in subject
        assert "期权腿缺失" in body
        assert "【缺失】" in body

    def test_subject_update_mode(self, minimal_data, minimal_positions):
        subject, body, _text = build_report(
            minimal_data, _empty_diff(), positions=minimal_positions, update_mode=True
        )
        assert "【更新】" in subject
        assert "替代版" in body

    def test_top5(self, minimal_data, minimal_positions):
        _, body, _text = build_report(minimal_data, _empty_diff(), positions=minimal_positions)
        assert "STOCK A" in body
        assert "STOCK B" in body
        assert "STOCK C" in body

    def test_first_run_without_previous_snapshot(self, minimal_data, minimal_positions):
        _, body, _text = build_report(minimal_data, _empty_diff(), positions=minimal_positions)
        assert "首次运行" in body


class TestEscaping:
    def test_scraped_names_are_escaped(self, minimal_data, minimal_positions):
        data = dict(minimal_data)
        eq = minimal_data["equities"].copy()
        eq.loc[0, "Name of Securities"] = "A&B <script>alert(1)</script>"
        data["equities"] = eq

        _, body, _text = build_report(data, _empty_diff(), positions=minimal_positions)

        assert "<script>" not in body
        assert "A&amp;B" in body

    def test_blocking_messages_are_escaped(self, minimal_data, minimal_positions):
        _, body, _text = build_report(
            minimal_data,
            _empty_diff(),
            positions=minimal_positions,
            blocking=["腿 <b>异常</b>"],
        )
        assert "<b>异常</b>" not in body
        assert "&lt;b&gt;异常&lt;/b&gt;" in body


class TestPlainTextFallback:
    def test_text_body_mirrors_key_content(self, minimal_data, minimal_positions):
        subject, html, text = build_report(
            minimal_data, _empty_diff(), positions=minimal_positions
        )
        assert subject
        assert "持仓截止日期:2026-08-24" in text
        assert "STOCK A" in text
        # plain text carries no markup, HTML counterpart does
        assert "<table" not in text
        assert "<table" in html

    def test_text_table_is_aligned(self, minimal_data, minimal_positions):
        _, _, text = build_report(minimal_data, _empty_diff(), positions=minimal_positions)
        header_line = next(line for line in text.splitlines() if line.startswith("#"))
        assert "|" in header_line


class TestMobileLayout:
    def test_mobile_css_emitted(self, minimal_data, minimal_positions):
        _, html, _text = build_report(minimal_data, _empty_diff(), positions=minimal_positions)
        assert "<style>" in html
        assert "@media (max-width:480px)" in html

    def test_cells_carry_data_label(self, minimal_data, minimal_positions):
        _, html, _text = build_report(minimal_data, _empty_diff(), positions=minimal_positions)
        assert 'data-label="权重"' in html
        assert 'data-label="名称"' in html

    def test_subtitle_renders_as_gray_note(self, minimal_data, minimal_positions):
        _, html, text = build_report(minimal_data, _empty_diff(), positions=minimal_positions)
        assert "color:#667085" in html
        assert "指数现价 8,490.31, 2026-08-24" in html
        assert "指数现价 8,490.31, 2026-08-24" in text


class TestDiffRendering:
    def test_base_date_shown_in_section_title(self, minimal_data, minimal_positions):
        diff = _empty_diff()
        diff["has_prev"] = True
        diff["base_date"] = "2026-08-21"
        diff["metrics"] = [
            {"label": "基金净值 NAV(HKD)", "prev": 100.0, "cur": 110.0, "fmt": ",.2f"}
        ]
        _, body, _text = build_report(minimal_data, diff, positions=minimal_positions)
        assert "d. 快照差异" in body
        assert "2026-08-21 vs 2026-08-24" in body
        assert "+10.00" in body

    def test_no_change_sections_collapse(self, minimal_data, minimal_positions):
        diff = _empty_diff()
        diff["has_prev"] = True
        diff["base_date"] = "2026-08-21"
        _, body, _text = build_report(minimal_data, diff, positions=minimal_positions)
        assert "正股成分无变动" in body
        assert "期货合约无变动" in body
        assert "期权合约无变动" in body
