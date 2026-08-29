import json
import os
from datetime import UTC, datetime
from typing import Any

from daily_holdings.config import DATA_DIR


def STATE_FILE() -> str:  # noqa: N802
    return os.path.join(DATA_DIR, "state.json")


def read_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def write_state(**updates: Any) -> None:
    st = read_state()
    st.update(updates)
    with open(STATE_FILE(), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def already_sent(as_of: object | None) -> bool:
    if as_of is None:
        return False
    return read_state().get("last_as_of") == as_of.isoformat()


def mark_sent(as_of: object | None, complete: bool = True) -> None:
    if as_of is None:
        return
    write_state(
        last_as_of=as_of.isoformat(),
        sent_at=datetime.now().isoformat(),
        last_complete=bool(complete),
        pending_as_of=None,
        pending_since=None,
    )


def check_readiness(
    data: dict[str, Any], pos: dict[str, Any], diff: dict[str, Any]
) -> tuple[list[str], list[str], bool]:
    blocking: list[str] = []
    warnings: list[str] = []
    waitable: list[bool] = []

    def block(msg: str, wait: bool = True) -> None:
        blocking.append(msg)
        waitable.append(wait)

    o_legs = data.get("option_legs") or []
    f_legs = data.get("futures_legs") or []

    if diff and diff["options"].get("rows_prev") and not o_legs:
        block(
            f"较前一日:期权合约行 {diff['options']['rows_prev']}→0 条;"
            f"若为官网分阶段发布,稍后重试即可"
        )
    if diff and diff["futures"].get("rows_prev") and not f_legs:
        block(
            f"较前一日:期货合约行 {diff['futures']['rows_prev']}→0 条;"
            f"若为官网分阶段发布,稍后重试即可"
        )
    if o_legs and data.get("index_close") is None:
        block(f"未取到 HSCEI 收盘点位,{len(o_legs)} 条期权腿的敞口无法计算")

    for d in pos.get("dropped_legs") or []:
        block(f"期权腿未计入敞口计算({d['reason']}):{d['name']}", wait=d.get("wait_helps", True))

    if o_legs and data["options"].empty:
        warnings.append("官网「期权敞口表」缺失,敞口已由完整持仓表计算")
    if data.get("nav_page") is None:
        warnings.append("页面未公布基金总净值,已改用「市值 ÷ 权重」反推,数值可能略有偏差")
    for nm in data.get("unknown_instruments") or []:
        warnings.append(f"发现既非期货也非期权的未识别工具,未纳入敞口计算:{nm}")

    return blocking, warnings, any(waitable)


def note_not_ready(as_of: object | None) -> float:
    key = as_of.isoformat() if as_of else "unknown"
    st = read_state()
    since = st.get("pending_since") if st.get("pending_as_of") == key else None
    if not since:
        since = datetime.now(UTC).isoformat()
        write_state(pending_as_of=key, pending_since=since)
    try:
        t0 = datetime.fromisoformat(since)
        if t0.tzinfo is None:
            t0 = t0.astimezone()
        waited = (datetime.now(UTC) - t0).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return 0.0
    return max(waited, 0.0)
