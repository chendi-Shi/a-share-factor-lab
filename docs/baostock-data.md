# BaoStock 历史数据采集

本项目增加了一个可复现的免费数据入口：[BaoStock](https://www.baostock.com/) 的历史中证 500 成分、交易日历、个股原始日线、除权事件因子与中证 500 指数日线。采集脚本是 `scripts/fetch_baostock.py`。[中证指数官方资料](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000905factsheet.pdf)列出指数样本数为 500。本地 `data/` 已被 Git 忽略；BaoStock Python 包的 BSD 许可只说明客户端代码许可，**不等于行情数据可以公开再分发**。

## 已核验的小样本

2026 年 9 月 29 日通过 BaoStock 0.9.4 实际查询：2020-01-02、2022-01-04、2024-01-02、2026-09-28 的中证 500 名单各有 500 只；2020 年名单中有 332 只不在 2026 年名单。2020-01-02 至 2020-01-10 的 7 个交易日已下载 3,500 条成分记录、3 只后来退市的股票各 86 条原始日线，以及 86 条指数日线。本地股票基础信息报告：`sh.600260` 于 2023-02-15、`sh.600277` 于 2024-07-18、`sh.600291` 于 2022-06-14 退市。该基础信息是**本次下载时**查询的事后状态。

这证明了接口能返回历史名单与退市股票旧行情，也证明仅用当前 500 只股票会遗漏大量历史成分。它**尚未**证明每一天的 `updateDate` 是当时可知的发布时间；成分文件只记录来源给出的日期与本地抓取时间。

## 复现采集

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . baostock==0.9.4
.\.venv\Scripts\python.exe scripts\fetch_baostock.py `
  --start 2020-01-02 --end 2020-01-10 `
  --symbols sh.600260,sh.600277,sh.600291 `
  --output-dir data\baostock_pilot
```

去掉 `--symbols` 会抓取区间内出现过的全部成分股价格。每个交易日独立请求当日成分，脚本串行访问并检查 500 只、重复代码与未来 `updateDate`；已下载的逐日成分、价格、复权因子可以在同一参数下断点续传。若改日期、回看长度或样本范围，必须换 `--output-dir`，以免把不同区间混入一个数据集。

输出有 `calendar.csv`、`membership.csv`、`membership_observations.csv`、`prices_raw/`、`adjust_factors/`、`stock_basic.csv`、`benchmark.csv` 和 `source_metadata.json`。`--symbols` 只下载列出的股票价格；成分仍是全量，`source_metadata.json` 会把它标记为价格样本，不能将其作为完整股票池回测。

采集器现在保存原始行情的 `preclose`。同一参数可断点续传；新增字段后的数据集使用版本 2，不会与此前没有 `preclose` 的缓存混用。2019-06-17 至 2019-06-25 的 `sh.600006` 独立实测样本涵盖 6 月 20 日除权：原始收盘价 4.93 元，前收参考价 4.85 元。按当日已出现的 `前一收盘价 / 当日前收参考价` 递推，研究价格约为 5.01；6 月 19 日之前的记录保持原值。

```powershell
.\.venv\Scripts\python.exe scripts\fetch_baostock.py `
  --start 2019-06-17 --end 2019-06-25 --lookback-days 15 `
  --symbols sh.600006 --output-dir data\baostock_corp_action
.\.venv\Scripts\python.exe scripts\build_research_prices.py --dataset data\baostock_corp_action
.\.venv\Scripts\python.exe scripts\audit_baostock.py --dataset data\baostock_corp_action
```

转换写入 `prices_research_adjusted/` 和原始文件哈希；审计写入 `readiness.json`。转换只使用当日及此前的 `preclose`，避免把未来除权因子回写到旧信号。它仍采用 BaoStock 的复权假设，不能代替公司行动现金流和可核验的历史数据版本。审计会把完整性与生产验收分别标记；免费数据缺少的时间戳和开盘成交证据使 `production_ready` 保持 `false`。

## 研究边界

- `prices_raw/` 是不复权价格，除权日可能出现机械跳变；因子与持有期收益不能直接当作可交易组合收益。`adjust_factors/` 保存事件因子，尚未验证其历史修订版本或现金分红收益。BaoStock 的[复权因子说明](https://www.baostock.com/helpdocs/pdf/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf)也明确其涨跌幅复权法采用特定再投资假设。
- `membership.csv` 没有可靠的 `known_at`；`source_update_dates` 不应擅自解释为公开发布时间。故这批数据不能伪装成 `--strict` 模式需要的逐时点可知成分。
- `tradestatus` 和 `isST` 只是日线状态，无法证明次日开盘买卖可成交。缺少历史开盘涨跌停/停牌状态、订单簿和未成交持仓路径。
- `benchmark.csv` 是中证 500 指数日线；没有核验其含分红总收益口径。与组合比较时必须标明指数口径。
- BaoStock 客户端[PyPI 页面](https://pypi.org/project/baostock/)称提供免费中国股票数据，公开原始行情的再分发权仍需向数据方确认，因此仓库只提交采集程序、校验和小样本汇总，不提交行情 CSV。

若之后取得有明确授权和可验证发布时间的数据，[Tushare Pro 的指数成分与权重接口](https://tushare.pro/document/2?doc_id=96)可作为交叉核验来源；官方文档列出的 `index_weight` 为月度成分且需至少 2000 积分，仍不能单独代替逐日可知的交易状态数据。
