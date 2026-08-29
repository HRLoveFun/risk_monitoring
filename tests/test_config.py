import pytest

from daily_holdings.config import env


def test_env_with_default(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    assert env("TEST_KEY", default="fallback") == "fallback"


def test_env_with_value(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "hello")
    assert env("TEST_KEY") == "hello"


def test_env_required(monkeypatch):
    monkeypatch.delenv("REQUIRED_KEY", raising=False)
    with pytest.raises(RuntimeError, match="缺少必填环境变量: REQUIRED_KEY"):
        env("REQUIRED_KEY", required=True)
