import pytest

from daily_holdings import notifier


@pytest.fixture
def attachment_path(tmp_path):
    path = tmp_path / "holdings_20260827.csv"
    path.write_text("Name of Securities\n测试\n", encoding="utf-8-sig")
    return str(path)


@pytest.fixture
def mail_config(monkeypatch):
    monkeypatch.setattr(notifier, "MAIL_FROM", "sender@example.com")
    monkeypatch.setattr(notifier, "MAIL_TO", ["risk1@example.com", "risk2@example.com"])


def _alternatives(msg):
    """Return ``(text_part, html_part)`` of a message with or without attachments."""
    root = msg.get_payload()
    return root[0].get_payload() if root[0].is_multipart() else root


class TestBuildMessage:
    def test_multiple_recipients_move_to_bcc(self, mail_config):
        msg = notifier._build_message("主题", "<p>hi</p>", "纯文本", [])
        assert msg["To"] == "sender@example.com"
        assert msg["Bcc"] == "risk1@example.com, risk2@example.com"

    def test_single_recipient_uses_to(self, mail_config, monkeypatch):
        monkeypatch.setattr(notifier, "MAIL_TO", ["risk1@example.com"])
        msg = notifier._build_message("主题", "<p>hi</p>", "纯文本", [])
        assert msg["To"] == "risk1@example.com"
        assert msg["Bcc"] is None

    def test_plain_text_precedes_html(self, mail_config):
        msg = notifier._build_message("主题", "<p>hi</p>", "纯文本", [])
        text, html = _alternatives(msg)
        assert text.get_content_type() == "text/plain"
        assert text.get_payload(decode=True).decode("utf-8").strip() == "纯文本"
        assert "<p>hi</p>" in html.get_payload(decode=True).decode("utf-8")

    def test_missing_text_falls_back_to_notice(self, mail_config):
        msg = notifier._build_message("主题", "<p>hi</p>", None, [])
        text, _html = _alternatives(msg)
        assert "HTML" in text.get_payload(decode=True).decode("utf-8")

    def test_attachment_basename_and_charset(self, mail_config, attachment_path):
        msg = notifier._build_message("主题", "<p>hi</p>", "纯文本", [attachment_path])
        attachment = msg.get_payload()[1]
        assert attachment.get_filename() == "holdings_20260827.csv"
        assert attachment.get_param("charset") == "utf-8"

    def test_attachment_keeps_utf8_bom(self, mail_config, attachment_path):
        msg = notifier._build_message("主题", "<p>hi</p>", "纯文本", [attachment_path])
        raw = msg.get_payload()[1].get_payload(decode=True)
        assert raw.decode("utf-8-sig").startswith("Name of Securities")

    def test_broken_attachment_is_skipped(self, mail_config):
        msg = notifier._build_message("主题", "<p>hi</p>", "纯文本", ["/no/such/file.csv"])
        assert "text/csv" not in [part.get_content_type() for part in msg.walk()]

    def test_auto_submitted_headers(self, mail_config):
        msg = notifier._build_message("主题", "<p>hi</p>", "纯文本", [])
        assert msg["Auto-Submitted"] == "auto-generated"
        assert msg["X-Auto-Response-Suppress"] == "All"
