from unittest.mock import patch

import pytest

from daily_holdings.scraper import fetch_page


class TestFetchPage:
    def test_success(self):
        html = "<html>holdingsList content here" * 1000
        with patch("daily_holdings.scraper.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.text = html
            result = fetch_page("http://example.com")
            assert "holdingsList" in result

    def test_too_short(self):
        html = "short"
        with patch("daily_holdings.scraper.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.text = html
            with pytest.raises(RuntimeError, match="页面内容异常"):
                fetch_page("http://example.com")

    def test_retries_on_failure(self):
        with patch("daily_holdings.scraper.requests.get") as mock_get:
            mock_get.side_effect = Exception("connection error")
            with pytest.raises(RuntimeError, match="页面抓取最终失败"):
                fetch_page("http://example.com")

    def test_raises_on_404(self):
        with patch("daily_holdings.scraper.requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = Exception("404")
            with pytest.raises(RuntimeError, match="页面抓取最终失败"):
                fetch_page("http://example.com")
