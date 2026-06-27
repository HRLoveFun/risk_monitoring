# ETF 每日持仓日报

抓取 **Global X HSCEI Covered Call Active ETF** 页面的完整持仓,生成摘要并邮件发送。

> 关键点:该基金页面的 "FULL HOLDINGS (.CSV)" 按钮是纯前端(`table2CSV.js`),
> 把页面里已有的持仓表格在浏览器转成 CSV,**没有后端下载链接**。
> 所以本工具直接抓页面 HTML 解析那张表 —— 结果与点按钮导出的 CSV 等价,且更稳定。

## 它做了什么
1. 抓页面(超时 + 重试 + 内容校验)
2. 解析完整持仓表 + 期权敞口表(按表头列名认表,不依赖易变的 id),
   拆分正股 / 衍生品,并由「市值 ÷ 权重」反推基金净值 NAV
3. 邮件发送:HTML 正文 + 当日完整持仓 CSV 附件

   **邮件主题**:`YYYYMMDD 3416.HK 持仓`(日期为持仓截止日,代码取自 `FUND_NAME`)

   **正文内容**:
   - 抬头:持仓截止日期 | 基金净值(估)
   - **a. 前五大重仓占比 XX%**(标题即合计占比,下接明细表)
   - **b. 风险暴露**(同时给「名义口径」与「Delta 调整后」两列):
     - 正股敞口 ≈ 总正股市值 / 基金净值
     - 期货多头敞口 = (合约张数 × 指数点 × 50) / 基金净值
     - 期权空头敞口:名义 = Σ 名义占比;Delta 调整 = 名义 × 估算 Delta
       (Black-Scholes,假设波动率 `IMPLIED_VOL`,默认 22%)
     - 净方向性敞口 = 正股 + 期货 + 期权(Delta 调整)
   - **c. 指数现价 → 行权价距离**(含剩余到期天数)
   - 较上一交易日变化:新增 / 剔除 / 权重变化 Top5(仅正股)
4. 全程日志;失败发告警邮件;同一截止日期不重复发(幂等)

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
