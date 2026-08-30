import os
import smtplib
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from html import escape

from daily_holdings.config import (
    FORCE_SEND,
    FUND_NAME,
    MAIL_FROM,
    MAIL_TO,
    SMTP_HOST,
    SMTP_PASS,
    SMTP_PORT,
    SMTP_RETRIES,
    SMTP_TIMEOUT,
    SMTP_USER,
    logger,
)
from daily_holdings.state import read_state, write_state

FALLBACK_TEXT = "本邮件为 HTML 格式,请使用支持 HTML 的客户端查看完整内容。"


def _build_message(
    subject: str, html_body: str, text_body: str | None, attachments: list[str] | None
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    if len(MAIL_TO) > 1:
        # Do not expose the recipient list to every recipient.
        msg["To"] = MAIL_FROM
        msg["Bcc"] = ", ".join(MAIL_TO)
    else:
        msg["To"] = ", ".join(MAIL_TO)
    # Keep auto-responders (out-of-office, vacation) out of the loop.
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"

    msg.set_content(text_body or FALLBACK_TEXT)
    msg.add_alternative(html_body, subtype="html")

    for path in attachments or []:
        try:
            with open(path, "rb") as f:
                data = f.read()
            # Keep the UTF-8 BOM written by the snapshot so Excel opens the
            # Chinese columns correctly; declare the charset explicitly.
            msg.add_attachment(
                data,
                maintype="text",
                subtype="csv",
                filename=os.path.basename(path),
                params={"charset": "utf-8"},
            )
        except Exception as e:
            logger.warning("附件添加失败(跳过)%s: %s", path, e)
    return msg


def send_email(
    subject: str,
    html_body: str,
    text_body: str | None = None,
    attachments: list[str] | None = None,
) -> None:
    if not (MAIL_FROM and SMTP_PASS and MAIL_TO):
        raise RuntimeError("邮件配置不全(SMTP_USER / SMTP_PASS / MAIL_TO)")

    msg = _build_message(subject, html_body, text_body, attachments)

    last_err: Exception | None = None
    for attempt in range(1, SMTP_RETRIES + 1):
        try:
            logger.info("发送邮件(第 %d 次)-> %s", attempt, MAIL_TO)
            if SMTP_PORT == 465:
                with smtplib.SMTP_SSL(
                    SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=SMTP_TIMEOUT
                ) as s:
                    s.login(SMTP_USER, SMTP_PASS)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(SMTP_USER, SMTP_PASS)
                    s.send_message(msg)
            logger.info("邮件发送成功")
            return
        except Exception as e:
            last_err = e
            logger.warning("发送失败: %s", e)
            if attempt < SMTP_RETRIES:
                time.sleep(2**attempt)
    raise RuntimeError(f"邮件最终发送失败: {last_err}")


def send_failure_alert(error_text: str) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    if not FORCE_SEND and read_state().get("last_alert_date") == today:
        logger.info("今日已发过失败告警,本次不重复发(避免高频触发刷屏)")
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"【失败告警】{FUND_NAME} {today}"
    safe = escape(error_text)
    html_body = (
        "<html><head><meta charset='utf-8'></head>"
        "<body style=\"font-family:Arial,'Microsoft YaHei',sans-serif;font-size:14px\">"
        f"<p><b>每日持仓任务执行失败。</b></p>"
        f"<p>发生时间:{escape(now)}(本地时区)</p>"
        f"<pre style='background:#f6f6f6;padding:8px;white-space:pre-wrap'>{safe}</pre>"
        "</body></html>"
    )
    text_body = f"每日持仓任务执行失败。\n发生时间:{now}(本地时区)\n\n{error_text}"
    try:
        send_email(subject, html_body, text_body=text_body)
        write_state(last_alert_date=today)
    except Exception as e:
        logger.error("连失败告警都发不出去: %s", e)
