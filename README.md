# 26-gz-fuwuzhan

## 生成排班

排班器读取 `docs/mamba-performance.json` 和 `docs/festival-performances.json`，使用 OR-Tools CP-SAT 生成班表。v5 公平版见 [v5 排班报告](docs/schedule-report-v5.md) 和 [v5 排班数据](docs/schedule-v5.json)；v4 调整版、v3 对比版和 v2 原版也均保留。结果包含每个人的班次、已满足及未满足的观演需求、全部需求满足率、分档满足数、限额偏好得分率及可完整观看的演出。三天总工时差不超过 180 分钟。

默认命令重新生成 v3 班表：

```sh
python3 -m pip install -r requirements.txt
python3 scheduler.py
```

生成 v4 班表（以当前 v3 班表为基准，优先满足陈思含的两项需求）：

```sh
python3 scheduler.py --version v4
```

生成 v5 公平班表：保证胡一希可完整观看汉堡黄，取消至少一名男性在岗要求，并要求陈思含比原 v5 多满足至少 1 项、贺金意少满足至少 1 项；随后依次最大化最低个人全部需求加权满足率、最小化全员满足率差距、最大化个人满足率之和，再尽量减少相对 v4 的班次调整。每项“必须看”计 100 分、“特别想看”计 30 分、“有点想看”计 10 分，分母包含所有填报项。

```sh
python3 scheduler.py --version v5
```

生成 v6：沿用胡一希汉堡黄观演保证与公平目标，以 v5 班表作为初始解和班次偏移基准；不设置男性覆盖要求，也不设置 v5 对陈思含和贺金意的满足数调整目标。

```sh
python3 scheduler.py --version v6
```

每个优化阶段默认最多求解 60 秒；可通过 `--time-limit` 调整，例如：

```sh
python3 scheduler.py --time-limit 120
```

排班结果会独立核对岗位覆盖、班次时长和班次间休息；v3/v4 另核对男性覆盖，v5/v6 不设置男性覆盖要求。当前模型按每天恰好 4 人在岗安排，因此交接重叠为 0 分钟，满足最多约 3 分钟的重叠上限。自由备注会作为提示写入结果，但不会自动转成约束。
