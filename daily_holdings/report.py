from datetime import date, datetime
from typing import Any

from daily_holdings.config import (
    FUND_NAME,
    FUND_URL,
    IMPLIED_VOL,
    INDEX_MULTIPLIER,
    WEIGHT_DIFF_TOL_PP,
)
from daily_holdings.positions import compute_positions as _compute_positions


def build_summary(
    data: dict[str, Any],
    diff: dict[str, Any],
    positions: dict[str, Any] | None = None,
    blocking: list[str] | None = None,
    warnings: list[str] | None = None,
    update_mode: bool = False,
) -> tuple[str, str]:
    blocking = list(blocking or [])
    warnings = list(warnings or [])
    eq = data["equities"]
    as_of = data["as_of"]
    weight_col, name_col, key_col = "Net Assets (%)", "Name of Securities", "Exchange Ticker"

    top = eq.sort_values(weight_col, ascending=False).head(5)
    top5_pct = float(top[weight_col].sum())
    top_rows = "".join(
        f"<tr><td>{i}</td><td>{r[name_col]}</td><td>{r[key_col]}</td>"
        f"<td style='text-align:right'>{r[weight_col]:.2f}%</td></tr>"
        for i, (_, r) in enumerate(top.iterrows(), 1)
    )

    p = positions if positions is not None else _compute_positions(data)

    def pct(v: float | None) -> str:
        return "N/A" if v is None else f"{v:.2f}%"

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
    pos_rows = (
        f"<tr><td>正股敞口</td><td style='text-align:right'>{pct(p.get('equity_pct'))}</td>"
        f"<td style='text-align:right'>—</td><td>≈ 总正股市值 / 基金净值</td></tr>"
        f"<tr><td>期货多头敞口</td>"
        f"<td style='text-align:right'>{pct(p.get('futures_pct'))}</td>"
        f"<td style='text-align:right'>—</td>"
        f"<td>合约张数 × 指数点 × {INDEX_MULTIPLIER:.0f} / 净值</td></tr>"
        f"<tr><td>期权空头敞口</td>"
        f"<td style='text-align:right'>{pct(p.get('option_notional_pct'))}</td>"
        f"<td style='text-align:right'>{pct(p.get('option_pressure_pct'))}</td>"
        f"<td>名义 = Σ(张数 × 指数 × {INDEX_MULTIPLIER:.0f}) / 净值;"
        f"调整 = 名义 × Delta({iv_desc})</td></tr>"
    )
    net_directional = None
    vals = [p.get("equity_pct"), p.get("futures_pct"), p.get("option_pressure_pct")]
    if None not in vals:
        net_directional = sum(vals)
    net_html = (
        ""
        if net_directional is None
        else f"<p><b>净方向性敞口(正股+期货+期权Delta)≈ {net_directional:.2f}%</b></p>"
    )

    dist_rows = (
        "".join(
            f"<tr><td style='text-align:right'>{d['index']:,.0f}</td>"
            f"<td style='text-align:right'>{d['strike']:,.0f}</td>"
            f"<td style='text-align:right'>{d['dist_pct']:+.2f}%</td>"
            f"<td style='text-align:right'>{d['days'] if d['days'] is not None else 'N/A'}</td>"
            f"</tr>"
            for d in p.get("strike_distances", [])
        )
        or "<tr><td colspan=4>无期权数据</td></tr>"
    )

    def _fmt_val(v: Any, fmt: str) -> str:
        return "N/A" if v is None else format(v, fmt)

    def _fmt_contracts(v: Any) -> str:
        if v is None:
            return "—"
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"

    changes_html = "<p>首次运行,无历史快照可对比。</p>"
    if diff and diff.get("has_prev"):
        eq_d, fu_d, op_d = diff["equities"], diff["futures"], diff["options"]

        def names(items: list) -> str:
            if not items:
                return "无"
            return ", ".join(f"{n}({k})" for n, k, *_ in items)

        metric_rows = "".join(
            f"<tr><td>{m['label']}</td>"
            f"<td style='text-align:right'>{_fmt_val(m['prev'], m['fmt'])}</td>"
            f"<td style='text-align:right'>{_fmt_val(m['cur'], m['fmt'])}</td></tr>"
            for m in diff["metrics"]
        )
        wc_rows = (
            "".join(
                f"<tr><td>{d['name']}</td><td>{d['ticker']}</td>"
                f"<td style='text-align:right'>{d['prev']:.2f}%</td>"
                f"<td style='text-align:right'>{d['cur']:.2f}%</td>"
                f"<td style='text-align:right'>{d['delta']:+.2f}%</td></tr>"
                for d in eq_d["weight_changes"]
            )
            or f"<tr><td colspan=5>权重变化均 &lt; {WEIGHT_DIFF_TOL_PP:.2f}pp</td></tr>"
        )

        def leg_rows(added: list, removed: list, changed: list) -> str:
            rows = (
                [(d["label"], "—", _fmt_contracts(d.get("contracts")), "新增") for d in added]
                + [(d["label"], _fmt_contracts(d.get("contracts")), "—", "剔除") for d in removed]
                + [
                    (d["label"], _fmt_contracts(d["prev"]), _fmt_contracts(d["cur"]), "变化")
                    for d in changed
                ]
            )
            return (
                "".join(
                    f"<tr><td>{lb}</td><td style='text-align:right'>{pv}</td>"
                    f"<td style='text-align:right'>{cv}</td><td>{tag}</td></tr>"
                    for lb, pv, cv, tag in rows
                )
                or "<tr><td colspan=4>无变化</td></tr>"
            )

        changes_html = (
            "<p><b>汇总指标:</b></p>"
            "<table border='1' cellspacing='0' cellpadding='4'>"
            "<tr><th>指标</th><th>前日</th><th>当日</th></tr>"
            f"{metric_rows}</table>"
            f"<p><b>正股:</b>新增 {len(eq_d['added'])} 只({names(eq_d['added'])});"
            f"剔除 {len(eq_d['removed'])} 只({names(eq_d['removed'])})</p>"
            "<table border='1' cellspacing='0' cellpadding='4'>"
            f"<tr><th>权重变化(≥{WEIGHT_DIFF_TOL_PP:.2f}pp)</th><th>代码</th>"
            "<th>前日</th><th>当日</th><th>变化</th></tr>"
            f"{wc_rows}</table>"
            f"<p><b>期货合约行:{fu_d['rows_prev']} → {fu_d['rows_cur']} 条</b></p>"
            "<table border='1' cellspacing='0' cellpadding='4'>"
            "<tr><th>合约</th><th>前日张数</th><th>当日张数</th><th>备注</th></tr>"
            f"{leg_rows(fu_d['added'], fu_d['removed'], fu_d['changed'])}</table>"
            f"<p><b>期权合约行:{op_d['rows_prev']} → {op_d['rows_cur']} 条;"
            f"空头总张数(绝对值){_fmt_contracts(op_d['contracts_abs_prev'])} → "
            f"{_fmt_contracts(op_d['contracts_abs_cur'])}</b></p>"
            "<table border='1' cellspacing='0' cellpadding='4'>"
            "<tr><th>合约(按行权价/到期日对齐)</th><th>前日张数</th>"
            "<th>当日张数</th><th>备注</th></tr>"
            f"{leg_rows(op_d['added'], op_d['removed'], op_d['changed'])}</table>"
        )

    def _bar(border: str, bg: str, fg: str, title: str, items: list, tail: str = "") -> str:
        li = "".join(f"<li>{i}</li>" for i in items)
        return (
            f"<div style='border:2px solid {border};background:{bg};color:{fg};"
            f"padding:8px 12px;margin-bottom:12px'><b>{title}</b>"
            f"<ul style='margin:6px 0 0 18px;padding:0'>{li}</ul>{tail}</div>"
        )

    banner = ""
    if blocking:
        banner = _bar(
            "#c00",
            "#fff4f4",
            "#c00",
            "⚠️ 以下项目缺失或无法计算,相关口径未纳入本报告",
            blocking + warnings,
            "<div style='margin-top:6px'>若后续数据补齐,将自动补发一封「【更新】」版。</div>",
        )
    else:
        if update_mode:
            banner = (
                "<div style='border:2px solid #0a0;background:#f3fff3;color:#070;"
                "padding:8px 12px;margin-bottom:12px'><b>✅ 更新版</b> —— "
                "此前一封因部分项目缺失而先行兜底发出;现相关数据已可完整计算,"
                "本封为其替代版。</div>"
            )
        if warnings:
            banner += _bar("#c80", "#fffbf0", "#a60", "⚠️ 以下情况请留意", warnings)

    as_of_str = as_of.strftime("%Y-%m-%d") if as_of else "未知"
    cross_note = ""
    if p.get("option_notional_pct_page") is not None and p.get("option_notional_pct") is not None:
        cross_note = (
            f"期权名义敞口两来源并列供对照:持仓表逐腿加总 "
            f"{p['option_notional_pct']:.2f}% / 官网敞口表 "
            f"{p['option_notional_pct_page']:.2f}%。<br>"
        )
    body = f"""\
<html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;font-size:14px">
{banner}
<p><b>持仓截止日期:</b>{as_of_str}</p>

<h3>a. 指数现价 → 行权价距离</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>指数现价</th><th>行权价</th><th>距离</th><th>剩余天数</th></tr>
{dist_rows}
</table>

<h3>b. 风险暴露</h3>
{net_html}
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>仓位</th><th>名义占净值</th><th>Delta调整后</th><th>算法</th></tr>
{pos_rows}
</table>

<h3>c. 前五大重仓占比 {top5_pct:.2f}%</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>#</th><th>名称</th><th>代码</th><th>权重</th></tr>
{top_rows}
</table>

<h3>较上一交易日差异</h3>
{changes_html}

<p style="color:#888;font-size:12px">
注:Delta = Black-Scholes N(d1),{iv_desc};期货/期权敞口取自完整持仓表的合约行
(名义 = 张数 × 指数 × {INDEX_MULTIPLIER:.0f}),{nav_note}。完整持仓见附件 CSV。<br>
{cross_note}数据来源:{FUND_URL}<br>本邮件由脚本自动生成于 {datetime.now():%Y-%m-%d %H:%M:%S}。</p>
</body></html>"""

    as_of_compact = as_of.strftime("%Y%m%d") if as_of else date.today().strftime("%Y%m%d")
    prefix = "【待补全】" if blocking else ("【更新】" if update_mode else "")
    subject = f"{prefix}{as_of_compact} {FUND_NAME} 持仓"
    return subject, body
