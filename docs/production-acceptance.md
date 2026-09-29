# 生产验收状态

本仓库目前是**可审计的量化研究候选系统，未通过生产验收**。这里的生产验收指：有权使用的数据能复现历史时点可见的信号、成分与价格；日常运行能解释缺失数据和未成交订单；输出可追溯到输入版本并接受独立复核。连接券商实盘还需要独立的订单、风险、合规与故障恢复验收。

## 已实现和验证

- 历史数据采集保留逐日中证 500 名单、退市股旧行情、原始价格、前收参考价、公司行动因子、指数日线与本地抓取时间；同参数断点续传，缓存与区间不能混用。
- 原始价到研究价格的调整链逐日向前计算。2019-06-20 的 `sh.600006` 除权样本已实测；单元测试确认后来的除权不会重写此前信号。
- 数据审计检查每日 500 只、股票代码、日历、原始与调整后价格、公司行动事件、指数覆盖和文件哈希。`--require-production` 会在任一验收条件未通过时返回失败状态。
- 严格研究输入要求来源声明、带时区的价格/成分/交易状态时间戳；逐日开盘账本记录现金、持仓、成交、受阻订单与费用。受阻卖单延续持仓，受阻买单保留现金；持仓缺开盘价时必须提供独立估值，否则停止。
- 合成端到端运行覆盖 65 个信号日和 13 次调仓；额外运行让一笔买单受阻，账本记录 1 笔受阻订单。合成结果没有市场含义。

## 尚未通过的验收门槛

本地 BaoStock 真实样本的 `readiness.json` 把以下项标成失败：全量历史股票价格（当前只有小样本）、历史成分和行情的可核验发布时间、复权因子的历史版本、开盘可成交状态、数据使用与公开发布权限。`updateDate` 与本地下载时间都不能充当历史 `known_at`。价格调整链也没有建模实际分红、配股资金流。

逐日账本目前使用可分割研究单位和统一 bps 成本；A 股整手、最低佣金、印花税版本、冲击成本、竞价排队、部分成交、公司行动现金权利/到账、停牌期间独立估值与运行告警仍需实现并用真实数据验收。因此输出的 `production_approved` 固定为 `false`。

[Tushare Pro 的历史成分](https://tushare.pro/document/2?doc_id=96)、[停复牌](https://tushare.pro/document/2?doc_id=214)、[每日涨跌停价](https://tushare.pro/document/2?doc_id=183)和[复权因子](https://tushare.pro/document/2?doc_id=28)接口可作为后续交叉核验候选，官方文档列出至少 2000 积分门槛。这些接口的历史数据也需要进一步核验公开时间、修订版本和使用权；当前没有相应凭据，因此项目没有把它们伪装成已经接入。

## 复现检查

```powershell
.\.venv\Scripts\python.exe scripts\audit_baostock.py `
  --dataset data\baostock_corp_action --require-production
```

该命令会写入本地 `readiness.json`，并按预期以失败状态退出。公开仓库不提交原始行情或个人凭据。

```powershell
.\.venv\Scripts\python.exe examples\make_demo_data.py
.\.venv\Scripts\python.exe scripts\run_event_research.py --synthetic `
  --data-dir data\demo\prices --calendar-csv data\demo\calendar.csv `
  --membership-csv data\demo\membership.csv --execution-csv data\demo\execution.csv `
  --benchmark-csv data\demo\benchmark.csv --manifest data\demo\manifest.json `
  --start 2022-04-01 --end 2022-06-30 --output-dir outputs\event_demo
```

逐日账本输出 `targets.csv`、`ledger.csv`、`orders.csv`、`positions.csv`、`summary.json`、包含输入与代码 SHA-256 的 `audit.json`，以及最后写入的 `complete.json` 产品哈希。旧版 `factorlab.cli --strict` 仍是遇到受阻订单即停止的保守研究路径；新账本是独立的持仓延续路径，二者不会混用业绩。
