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
4. 全程日志;失败发告警邮件;**按持仓截止日期幂等去重**(详见下文「调度与幂等」)

## 调度与幂等(为什么可以每 10 分钟跑)
官网持仓是「隔日更新」,且当天具体几点更新不固定。为尽快收到、又不重复发,
采用「**高频触发 + 按日期去重**」:

- 触发:工作日每 10 分钟跑一次(见部署)。
- 每次跑都读页面上的 `As of <日期>` 作为「持仓截止日」,与状态文件 `data/state.json`
  里 `last_as_of` 比对:
  - **日期没更新** → 直接跳过:不发邮件、不存快照、工作流仍成功(`nothing to commit`)。
  - **日期推进到新交易日** → 发一封(带「较上一交易日变化」),并把新日期写回 `state.json`。
- 净效果:**数据更新后最多 10 分钟内收到,且每个交易日最多一封**。
- 调试强制重发:`FORCE_SEND=1`(忽略去重)。

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

## 部署:GitHub Actions(已上线,免运维免费)
当前部署在 **公开仓库**(公开仓库的 Actions 额度无限,故可每 10 分钟跑;
私有仓库免费额度 2,000 分钟/月,高频会不够)。授权码等放加密 Secret,**公开不泄露**。

1. 推到一个 **公开** GitHub 仓库。
2. 仓库 Settings → Secrets and variables → Actions,加这 6 个 Secret:
   `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `MAIL_FROM` `MAIL_TO`
   (`FUND_NAME`、`FUND_URL` 非敏感,直接写在 workflow 的 `env:` 里,改基金只改这两行。)
3. `.github/workflows/daily.yml` 已配好:
   - 定时 `*/10 3-15 * * 1-5`(UTC)= **工作日 港时 11:00–23:50 每 10 分钟**;
   - 也可在 Actions 页面点 "Run workflow" 手动触发;
   - 跑完把当日快照(`data/holdings_*.csv`、`state.json`)提交回仓库,供次日对比。
4. 用 `gh` 一键创建并配置(参考)::
   ```bash
   gh repo create <name> --public --source=. --push
   set -a; source .env; set +a
   for k in SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS MAIL_FROM MAIL_TO; do
     gh secret set "$k" --body "${!k}"; done
   gh workflow run daily-holdings        # 手动触发一次验证
   ```

> 注:GitHub 定时任务不保证准点,高峰期可能延迟几分钟或偶尔跳过;配合幂等去重不影响结果。
>
> 想改用自己的服务器(如 Oracle Cloud 永久免费机)也行,等价 crontab:
> ```cron
> */10 11-23 * * 1-5  cd /path/app && set -a && . ./.env && set +a && /path/venv/bin/python daily_holdings.py
> ```
