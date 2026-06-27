# ETF 每日持仓日报

抓取 **Global X HSCEI Covered Call Active ETF** 页面的完整持仓,生成摘要并邮件发送。

> 关键点:该基金页面的 "FULL HOLDINGS (.CSV)" 按钮是纯前端(`table2CSV.js`),
> 把页面里已有的持仓表格在浏览器转成 CSV,**没有后端下载链接**。
> 所以本工具直接抓页面 HTML 解析那张表 —— 结果与点按钮导出的 CSV 等价,且更稳定。

## 它做了什么
1. 抓页面(超时 + 重试 + 内容校验)
2. 解析完整持仓表 + 期权敞口表(按表头列名认表,不依赖易变的 id)
3. 与上一份快照对比:新增 / 剔除 / 权重变化 Top5
4. 邮件发送:HTML 正文 + 当日持仓 CSV 附件
   正文内容
   a. 前5大重仓占比
   b. **算三个真实仓位**：
    - 正股敞口 ≈ 总正股市值占比
    - 期货多头敞口 = （合约张数 × 指数点 × 50）/ 基金净值。
    - 期权空头压力 = （合约张数 × 指数点 × 50）/ 基金净值 × 估算的 Delta。
   c. 指数现价到行权价的距离。
5. 全程日志;失败发告警邮件;同一截止日期不重复发(幂等)

## 本地运行
```bash
pip install -r requirements.txt
cp .env.example .env          # 填好邮箱授权码和收件人
set -a; source .env; set +a   # 加载环境变量
python daily_holdings.py
```
快照、日志、状态都在 `data/` 下。调试想强制重发:`FORCE_SEND=1 python daily_holdings.py`

## Foxmail / QQ 邮箱配置
`SMTP_PASS` 填的是**授权码**,不是登录密码:
QQ邮箱 → 设置 → 账号 → 开启 IMAP/SMTP → 生成授权码。
`SMTP_HOST=smtp.qq.com`,`SMTP_PORT=465`(SSL)。

## 部署:GitHub Actions(推荐,免运维免费)
1. 把本目录推到一个(私有)GitHub 仓库。
2. 仓库 Settings → Secrets and variables → Actions,加这些 Secret:
   `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `MAIL_FROM` `MAIL_TO`
3. `.github/workflows/daily.yml` 已配好:工作日香港时间 11:00 自动运行,
   也可在 Actions 页面点 "Run workflow" 手动测试。
4. 每日快照会自动提交回仓库,用于次日对比。

> 想用自己的服务器(如 Oracle Cloud 永久免费机)也行,crontab:
> ```cron
> 30 9 * * 1-5  cd /path/app && /path/venv/bin/python daily_holdings.py
> ```
