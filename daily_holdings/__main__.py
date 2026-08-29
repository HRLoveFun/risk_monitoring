import logging
from datetime import date
from typing import Any

from daily_holdings.config import (
    FORCE_SEND,
    FUND_URL,
    INCOMPLETE_GRACE_HOURS,
    setup_logging,
)
from daily_holdings.notifier import send_email, send_failure_alert
from daily_holdings.parser import parse_holdings
from daily_holdings.positions import compute_positions
from daily_holdings.report import build_summary
from daily_holdings.scraper import fetch_page
from daily_holdings.snapshot import diff_vs_previous, load_previous, save_snapshot
from daily_holdings.state import (
    already_sent,
    check_readiness,
    mark_sent,
    note_not_ready,
    read_state,
)

logger = logging.getLogger("daily_holdings")


def main() -> int:
    setup_logging()
    logger.info("===== 任务开始 =====")
    try:
        html = fetch_page(FUND_URL)
        data = parse_holdings(html)
        as_of = data["as_of"]
        if as_of is None:
            raise ValueError("无法从页面解析出持仓截止日期(As of ...),疑似官网改版")

        st = read_state()
        last = st.get("last_as_of")
        if last and as_of < date.fromisoformat(last) and not FORCE_SEND:
            logger.warning("页面截止日期 %s 早于已发送的 %s,判为页面异常,跳过本次", as_of, last)
            return 0

        prev_full, prev_meta = load_previous(as_of)
        diff = diff_vs_previous(
            prev_full,
            prev_meta,
            data["full"],
            {"nav": data["nav"], "index_close": data["index_close"]},
        )
        pos = compute_positions(data)
        blocking, warnings, wait_worth = check_readiness(data, pos, diff)
        ready = not blocking
        for w in warnings:
            logger.warning("数据告警(不阻断发信):%s", w)

        sent_before = already_sent(as_of)
        sent_incomplete = sent_before and not st.get("last_complete", False)

        update_mode = sent_incomplete and ready
        if sent_before and not update_mode and not FORCE_SEND:
            logger.info(
                "截止日期 %s 已发送过(可完整计算=%s),跳过(可设 FORCE_SEND=1 强制发送)",
                as_of,
                st.get("last_complete", False),
            )
            return 0

        if not ready and not update_mode:
            if not wait_worth:
                logger.warning(
                    "存在无法等待解决的拦截项(%s);直接发一封带提示的日报,"
                    "并留下 last_complete=False 以便修好后补发",
                    ";".join(blocking),
                )
            else:
                waited = note_not_ready(as_of)
                if waited < INCOMPLETE_GRACE_HOURS and not FORCE_SEND:
                    logger.warning(
                        "存在拦截项(%s);已等待 %.1fh < 宽限 %.1fh,本次不发信、"
                        "不写幂等标记,等下次触发",
                        ";".join(blocking),
                        waited,
                        INCOMPLETE_GRACE_HOURS,
                    )
                    return 0
                logger.warning(
                    "拦截项仍未消除(%s);已超过 %.1fh 宽限,先发一封【待补全】日报兜底,"
                    "补齐后会自动补发更新版",
                    ";".join(blocking),
                    INCOMPLETE_GRACE_HOURS,
                )

        meta: dict[str, Any] = {
            "as_of": as_of.isoformat(),
            "nav": data["nav"],
            "nav_page": data["nav_page"],
            "index_close": data["index_close"],
        }
        path = save_snapshot(data["full"], as_of, meta=meta)
        subject, body = build_summary(
            data, diff, positions=pos, blocking=blocking, warnings=warnings, update_mode=update_mode
        )
        send_email(subject, body, attachments=[path])
        mark_sent(as_of, complete=ready)
        logger.info(
            "===== 任务成功(%s)=====",
            "更新版补发" if update_mode else ("可完整计算" if ready else "有拦截项,已带提示发送"),
        )
        return 0
    except Exception as e:
        logger.exception("任务失败: %s", e)
        send_failure_alert(str(e))
        logger.info("===== 任务失败 =====")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
