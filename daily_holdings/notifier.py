import smtplib
import ssl
import time
from datetime import date
from email.message import EmailMessage

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


def send_email(subject: str, html_body: str, attachments: list[str] | None = None) -> None:
    if not (MAIL_FROM and SMTP_PASS and MAIL_TO):
        raise RuntimeError("邮件配置不全(SMTP_USER / SMTP_PASS / MAIL_TO)")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(MAIL_TO)
    msg.set_content("本邮件为 HTML 格式,请使用支持 HTML 的客户端查看。")
    msg.add_alternative(html_body, subtype="html")

    for path in attachments or []:
        try:
            with open(path, "rb") as f:
                data = f.read()
            msg.add_attachment(data, maintype="text", subtype="csv", filename=path.split("/")[-1])
        except Exception as e:
            logger.warning("附件添加失败(跳过)%s: %s", path, e)

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
    today = date.today().isoformat()
    if not FORCE_SEND and read_state().get("last_alert_date") == today:
        logger.info("今日已发过失败告警,本次不重复发(避免高频触发刷屏)")
        return
    try:
        send_email(
            subject=f"[ETF日报-失败] {FUND_NAME} {today}",
            html_body=f"<p>每日持仓任务执行失败:</p><pre>{error_text}</pre>",
        )
        write_state(last_alert_date=today)
    except Exception as e:
        logger.error("连失败告警都发不出去: %s", e)
