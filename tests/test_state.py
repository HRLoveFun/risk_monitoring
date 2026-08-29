import json
import os
from datetime import date, datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from daily_holdings.state import (
    already_sent,
    check_readiness,
    mark_sent,
    note_not_ready,
    read_state,
    write_state,
)


class TestReadWriteState:
    def test_read_empty(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            # No state file yet
            st = read_state()
            assert st == {}

    def test_write_and_read(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            write_state(last_as_of="2026-08-24", last_complete=True)
            st = read_state()
            assert st["last_as_of"] == "2026-08-24"
            assert st["last_complete"] is True

    def test_write_preserves_keys(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            write_state(last_as_of="2026-08-24")
            write_state(last_alert_date="2026-08-24")
            st = read_state()
            assert st["last_as_of"] == "2026-08-24"
            assert st["last_alert_date"] == "2026-08-24"


class TestAlreadySent:
    def test_not_sent_yet(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            write_state(last_as_of="2026-08-23")
            assert already_sent(date(2026, 8, 24)) is False

    def test_already_sent(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            write_state(last_as_of="2026-08-24")
            assert already_sent(date(2026, 8, 24)) is True

    def test_none_as_of(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            assert already_sent(None) is False


class TestMarkSent:
    def test_marks_as_sent(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            mark_sent(date(2026, 8, 24), complete=True)
            st = read_state()
            assert st["last_as_of"] == "2026-08-24"
            assert st["last_complete"] is True

    def test_clears_pending(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            write_state(pending_as_of="2026-08-24", pending_since="2026-08-24T00:00:00")
            mark_sent(date(2026, 8, 24))
            st = read_state()
            assert st["pending_as_of"] is None
            assert st["pending_since"] is None


class TestCheckReadiness:
    def test_ready(self):
        data = {"option_legs": [], "futures_legs": [], "options": pd.DataFrame()}
        pos = {"dropped_legs": []}
        diff = {"options": {"rows_prev": 0}, "futures": {"rows_prev": 0}}
        blocking, warnings, wait_worth = check_readiness(data, pos, diff)
        assert blocking == []
        assert wait_worth is False

    def test_dropped_leg_blocks(self):
        data = {
            "option_legs": [],
            "futures_legs": [],
            "options": pd.DataFrame(),
            "index_close": 100.0,
        }
        pos = {"dropped_legs": [{"name": "CALL XYZ", "reason": "test", "wait_helps": True}]}
        diff = {"options": {"rows_prev": 0}, "futures": {"rows_prev": 0}}
        blocking, warnings, wait_worth = check_readiness(data, pos, diff)
        assert len(blocking) == 1
        assert "test" in blocking[0]
        assert wait_worth is True

    def test_no_options_warnings(self):
        data = {
            "option_legs": [{"name": "x"}],
            "futures_legs": [],
            "options": pd.DataFrame(),
            "index_close": 100.0,
        }
        pos = {"dropped_legs": []}
        diff = {"options": {"rows_prev": 0}, "futures": {"rows_prev": 0}}
        _, warnings, _ = check_readiness(data, pos, diff)
        assert any("期权敞口表" in w for w in warnings)

    def test_no_nav_page_warning(self):
        data = {
            "option_legs": [],
            "futures_legs": [],
            "options": pd.DataFrame(),
            "index_close": 100.0,
            "nav_page": None,
        }
        pos = {"dropped_legs": []}
        diff = {"options": {"rows_prev": 0}, "futures": {"rows_prev": 0}}
        _, warnings, _ = check_readiness(data, pos, diff)
        assert any("反推" in w for w in warnings)


class TestNoteNotReady:
    def test_records_and_returns_zero_initially(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            waited = note_not_ready(date(2026, 8, 24))
            assert waited >= 0.0

    def test_subsequent_call_returns_elapsed(self, tmp_path):
        with patch("daily_holdings.state.DATA_DIR", tmp_path):
            note_not_ready(date(2026, 8, 24))
            waited = note_not_ready(date(2026, 8, 24))
            assert waited >= 0.0
