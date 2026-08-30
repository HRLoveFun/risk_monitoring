"""Compose the daily holdings email.

Both an HTML body (primary) and a plain-text body (fallback) are rendered from
one intermediate ``model`` built by :func:`_build_model`, so the two
representations can never drift apart.

Every value that originates from the scraped page is escaped exactly once, at
render time, by :func:`render_html`; the plain-text renderer uses the raw text.
"""

from datetime import date, datetime
from html import escape
from typing import Any
from unicodedata import east_asian_width

from daily_holdings.config import (
    FUND_NAME,
    FUND_URL,
    IMPLIED_VOL,
    INDEX_MULTIPLIER,
    WEIGHT_DIFF_TOL_PP,
)
from daily_holdings.positions import compute_positions as _compute_positions

# Email clients (Outlook in particular) drop <style> blocks, so the critical
# bits are inlined on every element.
TABLE_STYLE = "border-collapse:collapse;width:100%;font-size:13px"
CELL = "border:1px solid #d0d5dd;padding:4px 6px"
CELL_NUM = CELL + ";text-align:right;font-variant-numeric:tabular-nums"
HEAD_CELL = CELL + ";background:#f2f4f7;text-align:left;font-weight:bold"
HEAD_CELL_NUM = CELL + ";background:#f2f4f7;text-align:right;font-weight:bold"

# Increase / decrease of a quantity (not price up/down), hence the neutral
# green-for-more, red-for-less convention.
COLOR_POS = "#1a7f37"
COLOR_NEG = "#b42318"

LINE = "line"
TABLE = "table"
HEADERS = "headers"
ROWS = "rows"
EMPTY = "empty"
SUBTITLE = "subtitle"
MOBILE_CSS = """\
@media (max-width:480px){
  body{font-size:13px}
  table{display:block;border:0;width:100%}
  thead{display:none}
  tbody{display:block}
  tr{display:block;border:1px solid #d0d5dd;border-radius:8px;margin:0 0 10px;padding:6px 10px;background:#fff}
  td{display:block;border:0;padding:3px 0;white-space:normal;text-align:left;word-break:break-word;overflow-wrap:anywhere}
  td::before{content:attr(data-label) " : ";font-weight:bold;color:#555}
}
"""


WEIGHT_COL = "Net Assets (%)"
NAME_COL = "Name of Securities"
TICKER_COL = "Exchange Ticker"


def _pct(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "N/A"
    return format(v, f"{'+' if sign else ''}.2f") + "%"


def _num(v: float | None, fmt: str = ",.2f") -> str:
    return "N/A" if v is None else format(v, fmt)


def _delta(v: float | None, fmt: str) -> str:
    if v is None:
        return "—"
    text = format(v, "+" + fmt)
    return text.lstrip("+") if v == 0 else text


def _contracts(v: Any) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def _cell(text: str, tone: str | None = None) -> tuple[str, str | None]:
    return (text, tone)


def _display_width(text: str) -> int:
    """Terminal width of ``text``; CJK glyphs occupy two cells."""
    return sum(2 if east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    gap = max(width - _display_width(text), 0)
    return " " * gap + text if right else text + " " * gap


def _delta_tone(v: float | None) -> str | None:
    if v is None or v == 0:
        return None
    return "pos" if v > 0 else "neg"


def _line_item(text: str, bold: bool = False) -> tuple[str, tuple[str, bool]]:
    return (LINE, (text, bold))


def _table_item(
    headers: list[tuple[str, bool]],
    rows: list[list[tuple[str, str | None]]],
    empty: str,
) -> tuple[str, dict[str, Any]]:
    return (TABLE, {HEADERS: headers, ROWS: rows, EMPTY: empty})


def _subtitle_item(text: str) -> tuple[str, str]:
    return (SUBTITLE, text)


def _html_table(block: dict[str, Any]) -> str:
    rows: list[list[tuple[str, str | None]]] = block[ROWS]
    if not rows:
        return f"<p style='color:#666;font-size:13px'>{escape(block[EMPTY])}</p>"
    flags = [numeric for _, numeric in block[HEADERS]]
    head = "".join(
        f"<th style=\"{HEAD_CELL_NUM if n else HEAD_CELL}\">{escape(h)}</th>"
        for (h, n) in block[HEADERS]
    )
    body = []
    for row in rows:
        cells = []
        for (text, tone), (h, numeric) in zip(row, block[HEADERS], strict=True):
            style = CELL_NUM if numeric else CELL
            if tone == "pos":
                style += f";color:{COLOR_POS}"
            elif tone == "neg":
                style += f";color:{COLOR_NEG}"
            cells.append(
                f"<td style=\"{style}\" data-label=\"{escape(h)}\">{escape(text)}</td>"
            )
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f"<div style=\"overflow-x:auto\"><table style=\"{TABLE_STYLE}\">"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


def _text_table(block: dict[str, Any]) -> str:
    rows: list[list[tuple[str, str | None]]] = block[ROWS]
    if not rows:
        return block[EMPTY]
    headers = [h for h, _ in block[HEADERS]]
    flags = [n for _, n in block[HEADERS]]
    data = [[c[0] for c in row] for row in rows]
    widths = [_display_width(h) for h in headers]
    for row in data:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], _display_width(c))

    def line(cells: list[str]) -> str:
        parts = [
            _pad(c, widths[i], right=flags[i]) for i, c in enumerate(cells)
        ]
        return " | ".join(parts).rstrip()

    lines = [line(headers), "-+-".join("-" * w for w in widths)]
    lines += [line(r) for r in data]
    return "\n".join(lines)


def _build_model(
    data: dict[str, Any],
    diff: dict[str, Any],
    positions: dict[str, Any],
    blocking: list[str],
    warnings: list[str],
    update_mode: bool,
) -> dict[str, Any]:
    eq = data["equities"]
    as_of = data["as_of"]
    as_of_label = as_of.strftime("%Y-%m-%d") if as_of else "未知"
    as_of_compact = (as_of or date.today()).strftime("%Y-%m-%d")
    p = positions

    nav_note = (
        "NAV 取自页面公布的基金总净值"
        if data.get("nav_page")
        else "NAV 由「市值/权重」反推(页面未公布)"
    )
    iv_desc = (
        f"IV {p.get('iv_avg', 0):.1%} 由期权市价反解"
        if p.get("iv_all_from_market")
        else (
            f"IV≈{p.get('iv_avg', 0):.1%},部分腿反解失败退回假设 {IMPLIED_VOL:.0%}"
            if p.get("iv_avg")
            else f"IV={IMPLIED_VOL:.0%}(假设)"
        )
    )

    # --- banners -------------------------------------------------------
    bars: list[dict[str, Any]] = []
    if blocking:
        bars.append(
            {
                "tone": "error",
                "title": "【缺失】以下项目缺失或无法计算,相关口径未纳入本报告",
                "items": blocking,
                "tail": "若后续数据补齐,将自动补发一封「【更新】」版。",
            }
        )
    elif update_mode:
        bars.append(
            {
                "tone": "ok",
                "title": "【更新】此前一封因部分项目缺失而先行兜底发出",
                "items": [],
                "tail": "现相关数据已可完整计算,本封为其替代版。",
            }
        )
    if warnings:
        bars.append({"tone": "warn", "title": "【注意】以下情况请留意", "items": warnings, "tail": ""})

    # --- a. option legs ------------------------------------------------
    index_close = p.get("index_close")
    opt_rows: list[list[tuple[str, str | None]]] = []
    for r in p.get("option_rows", []):
        strike, index = r["strike"], index_close
        dist = (strike - index) / index * 100 if (index and strike) else None
        iv = "N/A" if r["iv"] is None else f"{r['iv']:.1%}" + ("" if r["iv_src"] == "市价反解" else "*")
        opt_rows.append(
            [
                _cell(r["pos"]),
                _cell(_num(strike, ",.0f")),
                _cell(_pct(dist, sign=True)),
                _cell(r["expiry"].isoformat() if r["expiry"] else "N/A"),
                _cell("N/A" if r["days"] is None else str(r["days"])),
                _cell(_contracts(r["contracts"])),
                _cell(iv),
                _cell("N/A" if r["delta"] is None else f"{r['delta']:.3f}"),
                _cell(_pct(r["notional_pct"])),
            ]
        )
    opt_headers = [
        ("合约", False),
        ("行权价", True),
        ("距离", True),
        ("到期日", False),
        ("剩余天数", True),
        ("张数", True),
        ("IV", True),
        ("Delta", True),
        ("名义占净值", True),
    ]
    index_label = f"{index_close:,.2f}" if index_close else "未知"
    section_a = {
        "title": "a. 期权腿明细",
        "items": [
            _subtitle_item(f"指数现价 {index_label}, {as_of_label}"),
            _table_item(
                opt_headers, opt_rows, "当前无期权腿数据(未解析到期权合约行)。"
            ),
        ],
    }

    # --- b. exposure ---------------------------------------------------
    net_directional: float | None = None
    equity_pct: float | None = p.get("equity_pct")
    futures_pct: float | None = p.get("futures_pct")
    if equity_pct is not None and futures_pct is not None:
        net_directional = equity_pct + futures_pct
        option_pressure: float | None = p.get("option_pressure_pct")
        if option_pressure is not None:
            net_directional += option_pressure

    expo_rows = [
        [
            _cell("正股敞口"),
            _cell(_pct(p.get("equity_pct"))),
            _cell("—"),
        ],
        [
            _cell("期货多头敞口"),
            _cell(_pct(p.get("futures_pct"))),
            _cell("—"),
        ],
        [
            _cell("期权空头敞口"),
            _cell(_pct(p.get("option_notional_pct"))),
            _cell(_pct(p.get("option_pressure_pct"))),
        ],
    ]
    nav = p.get("nav")
    fut_rows: list[list[tuple[str, str | None]]] = []
    for leg in p.get("futures_rows", []):
        notional_pct = (
            leg["contracts"] * leg["index_pt"] * INDEX_MULTIPLIER / nav * 100 if nav else None
        )
        fut_rows.append(
            [
                _cell(leg["name"]),
                _cell(_num(leg["index_pt"])),
                _cell(_contracts(leg["contracts"])),
                _cell(_pct(notional_pct)),
            ]
        )
    section_b: dict[str, Any] = {"title": "b. 风险暴露", "items": []}
    if net_directional is not None:
        section_b["items"].append(
            _line_item(f"净方向性敞口(正股+期货+期权Delta)≈ {net_directional:.2f}%", bold=True)
        )
    section_b["items"].append(
        _table_item(
            [("仓位", False), ("名义占净值", True), ("Delta调整后", True)],
            expo_rows,
            "当前无敞口数据。",
        )
    )
    section_b["items"].append(
        _table_item(
            [("期货合约", False), ("指数点", True), ("张数", True), ("名义占净值", True)],
            fut_rows,
            "当前无期货腿数据。",
        )
    )

    # --- c. top 5 ------------------------------------------------------
    top = eq.sort_values(WEIGHT_COL, ascending=False).head(5)
    top5_pct = float(top[WEIGHT_COL].sum())
    top_rows = [
        [
            _cell(str(i)),
            _cell(str(r[NAME_COL])),
            _cell(str(r[TICKER_COL])),
            _cell(_pct(float(r[WEIGHT_COL]))),
        ]
        for i, (_, r) in enumerate(top.iterrows(), 1)
    ]
    section_c = {
        "title": f"c. 前五大重仓占比 {top5_pct:.2f}%",
        "items": [
            _table_item(
                [("#", True), ("名称", False), ("代码", False), ("权重", True)],
                top_rows,
                "正股持仓为空。",
            )
        ],
    }

    # --- d. diff vs previous snapshot ----------------------------------
    has_prev = bool(diff and diff.get("has_prev"))
    base_date = (diff or {}).get("base_date")
    section_d: dict[str, Any] = {"title": "d. 快照差异", "items": []}
    if has_prev:
        cur_label = as_of_label
        section_d["items"].append(
            _subtitle_item(f"{base_date or '?'} vs {cur_label}")
        )

    if not has_prev:
        section_d["items"].append(
            _table_item([("说明", False)], [], "首次运行,无历史快照可对比。")
        )
    else:
        eq_d, fu_d, op_d = diff["equities"], diff["futures"], diff["options"]

        metric_rows = []
        for m in diff["metrics"]:
            prev, cur, fmt = m["prev"], m["cur"], m["fmt"]
            delta = (cur - prev) if (prev is not None and cur is not None) else None
            metric_rows.append(
                [
                    _cell(m["label"]),
                    _cell(_num(prev, fmt)),
                    _cell(_num(cur, fmt)),
                    _cell(_delta(delta, fmt), _delta_tone(delta)),
                ]
            )
        section_d["items"].append(_line_item("汇总指标", bold=True))
        section_d["items"].append(
            _table_item(
                [("指标", False), ("前日", True), ("当日", True), ("变化", True)],
                metric_rows,
                "无可比指标。",
            )
        )

        def name_list(items: list[tuple[str, ...]]) -> str:
            return ", ".join(f"{n}({k})" for n, k, *_ in items) or "无"

        if eq_d["added"] or eq_d["removed"]:
            section_d["items"].append(
                _line_item(
                    f"正股新增 {len(eq_d['added'])} 只({name_list(eq_d['added'])});"
                    f"剔除 {len(eq_d['removed'])} 只({name_list(eq_d['removed'])})"
                )
            )
        else:
            section_d["items"].append(_line_item("正股成分无变动"))

        wc_rows = [
            [
                _cell(d["name"]),
                _cell(d["ticker"]),
                _cell(_pct(d["prev"])),
                _cell(_pct(d["cur"])),
                _cell(_pct(d["delta"], sign=True), _delta_tone(d["delta"])),
            ]
            for d in eq_d["weight_changes"]
        ]
        section_d["items"].append(_line_item(f"权重变化(≥ {WEIGHT_DIFF_TOL_PP:.2f}pp)"))
        section_d["items"].append(
            _table_item(
                [
                    ("证券", False),
                    ("代码", False),
                    ("前日", True),
                    ("当日", True),
                    ("变化", True),
                ],
                wc_rows,
                f"权重变化均 < {WEIGHT_DIFF_TOL_PP:.2f}pp",
            )
        )

        def leg_rows(
            added: list[dict[str, Any]],
            removed: list[dict[str, Any]],
            changed: list[dict[str, Any]],
        ) -> list[list[tuple[str, str | None]]]:
            rows = [
                [
                    _cell(d["label"]),
                    _cell("—"),
                    _cell(_contracts(d.get("contracts"))),
                    _cell("新增", "pos"),
                ]
                for d in added
            ]
            rows += [
                [
                    _cell(d["label"]),
                    _cell(_contracts(d.get("contracts"))),
                    _cell("—"),
                    _cell("剔除", "neg"),
                ]
                for d in removed
            ]
            rows += [
                [
                    _cell(d["label"]),
                    _cell(_contracts(d["prev"])),
                    _cell(_contracts(d["cur"])),
                    _cell("变化"),
                ]
                for d in changed
            ]
            return rows

        leg_headers = [("合约", False), ("前日张数", True), ("当日张数", True), ("备注", False)]
        section_d["items"].append(
            _line_item(f"期货合约行:{fu_d['rows_prev']} → {fu_d['rows_cur']} 条")
        )
        section_d["items"].append(
            _table_item(
                leg_headers,
                leg_rows(fu_d["added"], fu_d["removed"], fu_d["changed"]),
                "期货合约无变动。",
            )
        )
        section_d["items"].append(
            _line_item(
                f"期权合约行:{op_d['rows_prev']} → {op_d['rows_cur']} 条;"
                f"空头总张数(绝对值) {_contracts(op_d['contracts_abs_prev'])} → "
                f"{_contracts(op_d['contracts_abs_cur'])}"
            )
        )
        section_d["items"].append(
            _table_item(
                leg_headers,
                leg_rows(op_d["added"], op_d["removed"], op_d["changed"]),
                "期权合约无变动(按行权价/到期日对齐)。",
            )
        )

    # --- notes ---------------------------------------------------------
    iv_note = f"Delta = Black-Scholes N(d1),{iv_desc}"
    if not p.get("iv_all_from_market"):
        iv_note += f";带 * 的 IV 为假设值 {IMPLIED_VOL:.0%}"
    notes = [
        iv_note + "。",
        f"敞口算法:正股 = 总正股市值 / 净值;期货/期权名义 = Σ(张数 × 指数点 × "
        f"{INDEX_MULTIPLIER:.0f}) / 净值;Delta 调整 = 名义 × N(d1)。",
        f"{nav_note};完整持仓见附件 CSV。",
    ]
    if p.get("option_notional_pct_page") is not None and p.get("option_notional_pct") is not None:
        notes.append(
            f"期权名义敞口两来源并列供对照:持仓表逐腿加总 "
            f"{p['option_notional_pct']:.2f}% / 官网敞口表 {p['option_notional_pct_page']:.2f}%。"
        )
    notes.append(f"数据来源:{FUND_URL}")

    prefix = "【待补全】" if blocking else ("【更新】" if update_mode else "")
    subject = f"{prefix}{as_of_compact} {FUND_NAME} 持仓日报"

    return {
        "subject": subject,
        "as_of_label": as_of_label,
        "bars": bars,
        "sections": [section_a, section_b, section_c, section_d],
        "notes": notes,
        "generated_at": datetime.now(),
    }


def render_html(model: dict[str, Any]) -> str:
    bars_html = []
    tone_style = {
        "error": ("#c00", "#fff4f4", "#c00"),
        "warn": ("#c80", "#fffbf0", "#a60"),
        "ok": ("#0a0", "#f3fff3", "#070"),
    }
    for bar in model["bars"]:
        border, bg, fg = tone_style[bar["tone"]]
        items = "".join(f"<li>{escape(i)}</li>" for i in bar["items"])
        ul = (
            f"<ul style='margin:6px 0 0 18px;padding:0'>{items}</ul>" if bar["items"] else ""
        )
        tail = f"<div style='margin-top:6px'>{escape(bar['tail'])}</div>" if bar["tail"] else ""
        bars_html.append(
            f"<div style='border:2px solid {border};background:{bg};color:{fg};"
            f"padding:8px 12px;margin-bottom:12px'><b>{escape(bar['title'])}</b>{ul}{tail}</div>"
        )

    sections_html = []
    for sec in model["sections"]:
        parts = [
            f"<h3 style='margin:22px 0 2px;font-size:15px;text-decoration:underline'>"
            f"{escape(sec['title'])}</h3>"
        ]
        for kind, payload in sec["items"]:
            if kind == LINE:
                text, bold = payload
                parts.append(
                    f"<p style='margin:8px 0 4px'><b>{escape(text)}</b></p>"
                    if bold
                    else f"<p style='margin:8px 0 4px'>{escape(text)}</p>"
                )
            elif kind == SUBTITLE:
                parts.append(
                    f"<p style='color:#667085;font-size:12px;margin:2px 0 10px'>{escape(payload)}</p>"
                )
            else:
                parts.append(_html_table(payload))
        sections_html.append("".join(parts))

    notes_html = "<br>".join(escape(n) for n in model["notes"])
    generated = model["generated_at"].strftime("%Y-%m-%d %H:%M:%S")
    return f"""\
<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(model['subject'])}</title>
<style>{MOBILE_CSS}</style></head>
<body style="margin:0;padding:12px;font-family:Arial,'Microsoft YaHei',sans-serif;\
font-size:14px;color:#222;background:#fff">
<div style="max-width:900px">
{''.join(bars_html)}
<p style="margin:6px 0"><b>持仓截止日期:</b>{escape(model['as_of_label'])}</p>
{''.join(sections_html)}
<p style="color:#888;font-size:12px;line-height:1.6;margin-top:18px">
{notes_html}<br>
数据截止 {escape(model['as_of_label'])};本邮件生成于 {generated}(本地时区)。</p>
</div></body></html>"""


def render_text(model: dict[str, Any]) -> str:
    lines: list[str] = []
    for bar in model["bars"]:
        lines.append(bar["title"])
        lines += [f"  - {i}" for i in bar["items"]]
        if bar["tail"]:
            lines.append(f"  {bar['tail']}")
        lines.append("")

    lines.append(f"持仓截止日期:{model['as_of_label']}")
    lines.append("")
    for sec in model["sections"]:
        lines.append(sec["title"])
        for kind, payload in sec["items"]:
            if kind == LINE:
                lines.append(f"  {payload[0]}")
            elif kind == SUBTITLE:
                lines.append(f"  {payload}")
            else:
                lines.append(_text_table(payload))
        lines.append("")

    lines.append("注:")
    lines += [f"  - {n}" for n in model["notes"]]
    generated = model["generated_at"].strftime("%Y-%m-%d %H:%M:%S")
    lines.append(
        f"  - 数据截止 {model['as_of_label']};本邮件生成于 {generated}(本地时区)。"
    )
    return "\n".join(lines)


def build_report(
    data: dict[str, Any],
    diff: dict[str, Any],
    positions: dict[str, Any] | None = None,
    blocking: list[str] | None = None,
    warnings: list[str] | None = None,
    update_mode: bool = False,
) -> tuple[str, str, str]:
    """Return ``(subject, html_body, text_body)``."""
    model = _build_model(
        data,
        diff,
        positions if positions is not None else _compute_positions(data),
        list(blocking or []),
        list(warnings or []),
        update_mode,
    )
    return model["subject"], render_html(model), render_text(model)
