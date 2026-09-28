# A 股多因子研究实验室 | A-Share Factor Lab

一个面向量化研究实习岗位的可复现项目：从日线数据构建 **动量、短期反转、低波动、流动性** 四类因子，检验截面 Rank IC，并用固定规则构造多因子组合。项目优先展示研究设计、时间对齐、数据质量和样本外评估；收益数字只有在数据条件成立时才有意义。

## 研究问题

在 A 股股票池中，价格与成交信息能否在未来 5 个交易日的截面收益上提供稳定排序？

- `mom_60_5`：`close[t-5] / close[t-60] - 1`，跳过最近 5 日的中期动量。
- `reversal_5`：`1 - close[t] / close[t-5]`，短期反转。
- `low_vol_20`：最近 20 日收盘收益标准差的负值，低波动排前。
- `log_amount_20`：最近 20 日平均成交额的对数，流动性排前。

四个因子在每个交易日各自做百分位排名，再等权平均。这里固定因子方向、窗口和权重，**没有用测试集挑因子或调参数**。这是简单、可解释的基线，方便以后与机器学习排序模型比较。

四份可独立阅读的因子研究笔记：[中期动量](studies/momentum.md)、[短期反转](studies/reversal.md)、[低波动](studies/low-volatility.md)、[成交额](studies/liquidity.md)。每份都保留训练段与测试段结果、失败解释和下一步验证问题。

## 防止常见回测错误

1. 在交易日 `t` 收盘后才计算含当天收盘价与成交额的信号；在 `t+1` 开盘买入，在 `t+6` 开盘卖出。标签为 `open[t+6] / open[t+1] - 1`。
2. 各股票先对齐交易日历。停牌或缺失数据不会把未来收益偷换成该股下一笔可用记录的收益。
3. 单因子每日 Spearman Rank IC 的均值使用 Newey–West 标准误统计量，缓解 5 日标签重叠引起的自相关。训练段的标签必须在测试段开始前结束。
4. 组合每 5 个市场交易日调仓一次，买入因子综合排名前 20% 的股票，等权持有。以上一持有期收益漂移后的权重计算调仓交易额，对每单位买卖交易额扣 10 bps 的示例成本；同时报告相同股票池的等权基准。
5. 信号日只用当时可见的价格、成交额和成分资格过滤。未来开盘缺失的头寸按该期 0 收益计价并单独计数；这是研究近似，不能当作停牌/涨跌停撮合模型。

### 数据与股票池口径

输入为每只股票一个 CSV，至少包含 `date,open,high,low,close,amount`；文件名是六位股票代码。代码会剔除非正数或缺失的 OHLC 行，并在结果中记录数量。使用复权价格时，应确认开盘和收盘价格口径一致。

`--symbols-json` 只会按**当前名单**筛选文件，历史检验存在严重的成分股生存偏差。若有授权的历史成分数据，可提供 `--membership-csv`，其每行是当日已知的 `date,symbol` 成分资格。没有历史名单时，本项目把结果标记为 `snapshot / biased`；请勿把它包装成可交易策略收益。

公开仓库不包含行情数据。示例数据由固定随机种子生成，供验证程序运行，**不代表真实市场证据**。

## 快速运行

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe examples\make_demo_data.py
.\.venv\Scripts\python.exe -m factorlab.cli --data-dir data\demo --synthetic --start 2022-04-01 --end 2025-05-30 --test-start 2024-01-01 --min-amount 0
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

真实日线数据示例：

```powershell
.\.venv\Scripts\python.exe -m factorlab.cli `
  --data-dir C:\path\to\daily_csv `
  --symbols-json C:\path\to\current_symbols.json `
  --start 2021-01-01 --end 2026-08-18 --test-start 2025-01-01 `
  --output-dir outputs\real_run
```

若提供真正的逐日历史成分文件，改用 `--membership-csv C:\path\to\daily_membership.csv`。对于大型股票池，运行时间主要消耗在按日计算截面相关系数；无需 GPU。

输出：`summary.json`（机器可读指标）、`ic_daily.csv`（逐日逐因子 IC）、`portfolio_periods.csv`（每次调仓的收益与成本）、`report.md`（可读摘要）。运行生成的行情和结果默认被 `.gitignore` 排除。可审阅的真实样本汇总见 [results/observed-run.md](results/observed-run.md)。

## 项目结构

- `factorlab/core.py`：数据校验、交易日对齐、四个因子与未来执行收益标签。
- `factorlab/research.py`：Rank IC、Newey–West 统计量、定期调仓和费用。
- `factorlab/cli.py`：完整研究流程与结果导出。
- `tests/`：时间对齐、缺失交易日、股票池资格和成本测试。
- `examples/`：确定性合成行情。

## 下一步研究

- 换用可靠的历史成分、退市股与财务数据时间戳，解决当前股票池偏差。
- 加入涨跌停、停牌、最小交易单位、佣金和冲击成本的事件驱动撮合。
- 在严格滚动训练和验证框架下，对比固定等权因子与机器学习排序模型；保留从未触碰的最终测试段。

本项目供研究和求职展示，不构成投资建议。
