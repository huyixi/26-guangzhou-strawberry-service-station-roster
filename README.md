# 26-gz-fuwuzhan

## 生成排班

排班器读取 `docs/mamba-performance.json` 和 `docs/festival-performances.json`，使用 OR-Tools CP-SAT 生成 v3 班表，并写入 `docs/schedule-v3.json` 和 `docs/schedule-report-v3.md`。v2 班表保留在 `docs/schedule-output.json` 和 `docs/schedule-report.md`，用于比较。结果包含每个人的班次、全部需求满足率、分档满足数、限额偏好得分率及可完整观看的演出。“必须看”按填写场数从少到多逐组优先满足；三天总工时差不超过 180 分钟。工时差在需求满足目标之后才优化。

```sh
python3 -m pip install -r requirements.txt
python3 scheduler.py
```

每个优化阶段默认最多求解 60 秒；可通过 `--time-limit` 调整，例如：

```sh
python3 scheduler.py --time-limit 120
```

排班结果会独立核对岗位覆盖、男性覆盖、班次时长和班次间休息。当前模型按每天恰好 4 人在岗安排，因此交接重叠为 0 分钟，满足最多约 3 分钟的重叠上限。自由备注会作为提示写入结果，但不会自动转成约束。
