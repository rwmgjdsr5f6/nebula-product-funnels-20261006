# 访问时段筛选下三种输出一致性：固定合成样例说明

核对日期：2026-10-07

## 1. 核对依据与范围

本说明覆盖一条**已有**报告流程——访问时段筛选与日期分组、编号明细、配对明细
三种输出同时启用：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite>
       --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-08T00:00:00
       --within-seconds 60 --include-users --include-pairs
       --group-by visit-date
```

目的是用一份固定的小型合成输入，核对同一访问时段下汇总、编号明细、配对明细
与日期分组四种视图的口径一致。本说明不要求也不描述任何功能变更；现有命令、
参数、输出字段与统计规则均保持现状。对应的回归测试为
`tests/test_window_grouped_report_consistency.py`，可用
`python -m unittest discover -s tests` 一并验收。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、输出 |
| `tests/test_window_grouped_report_consistency.py` | 本样例的回归测试 |
| `docs/verification-visit-window.md` | 访问时段筛选规则的核对说明（姊妹篇） |
| `docs/verification-visit-date.md` | `--group-by visit-date` 分组规则的核对说明（姊妹篇） |
| `docs/verification-conversion-pairs.md` | `--include-pairs` 配对明细规则的核对说明（姊妹篇） |

**数值来源声明：** 下文所有退出码、人数、比例与明细内容，均为对
`funnel/__main__.py` 当前源码及 CPython 标准库行为的静态推导；若实际观察与
本文推导不符，以实际观察为准并据此修正本文。

## 2. 固定合成样例（十条事件，年份均为 2026）

以下十条完整 UTF-8 JSONL 记录（字段沿用现有格式：`user_id` / `event` /
`timestamp`，时间戳无时区后缀，统一按 UTC 解释），保存为 `events.jsonl`：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-05T23:59:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T23:59:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T23:59:30"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-08T00:00:30"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-07T12:00:00"}
{"user_id": "u2", "event": "signup", "timestamp": "2026-10-07T12:00:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-08T00:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-08T00:00:30"}
{"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T12:00:00"}
```

人物设定：

- **u1**：5 日 23:59:00 访问（段外历史）；6 日 10:00:00 访问（恰在时段起点）；
  7 日 23:59:00 与 23:59:30 各访问一次；8 日 00:00:30 注册。
- **u2**：7 日 12:00:00 同一时刻访问并注册。
- **u3**：8 日 00:00:00 访问（恰在时段终点）；00:00:30 注册。
- **u4**：仅 6 日 12:00:00 访问。

导入新库：

```sh
python -m funnel import events.jsonl --db events.sqlite
```

预期标准输出（一行，退出码 0，标准错误为空）：

```json
{"imported": 10}
```

## 3. 访问时段与逐人判定

访问时段为 `[2026-10-06T10:00:00, 2026-10-08T00:00:00)`：含起点、不含终点
（`funnel/__main__.py` 的 `_visit_window_clause`：`timestamp >= ? AND
timestamp < ?`）。逐人判定：

- **u1**：5 日 23:59:00 的 visit 在段外——不计入访问人数，不参与转化配对，
  也不影响归组。段内 visit 为 6 日 10:00:00（恰在起点，含起点）、7 日
  23:59:00 与 23:59:30。signup 8 日 00:00:30 与 23:59:30 的 visit 间隔恰
  60 秒：严格晚于该 visit 且间隔 ≤ 60（上界含等值），转化成立；signup 晚于
  段终点不影响配对。归组取最早段内 visit 的日期，即 **6 日组**。
- **u2**：visit 与 signup 同在 7 日 12:00:00；配对要求 signup 严格晚于
  visit，相等时刻不算转化。计入访问，归 **7 日组**，不转化。
- **u3**：唯一 visit 在 8 日 00:00:00，恰在终点；终点不含，该 visit 被排除，
  u3 不进入任何统计，其 signup 与结果无关。
- **u4**：6 日 12:00:00 visit 在段内，无 signup。计入访问，归 **6 日组**，
  不转化。

## 4. `--within-seconds 60`：三种输出一致

```sh
python -m funnel report --db events.sqlite \
    --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-08T00:00:00 \
    --within-seconds 60 --include-users --include-pairs \
    --group-by visit-date
```

预期标准输出（一行，退出码 0，标准错误为空；按 JSON 值比较）：

```json
{
  "visit_users": 3,
  "converted_users": 1,
  "conversion_rate": 0.3333333333333333,
  "visit_user_ids": ["u1", "u2", "u4"],
  "converted_user_ids": ["u1"],
  "conversion_pairs": [
    {
      "user_id": "u1",
      "visit_timestamp": "2026-10-07T23:59:30",
      "signup_timestamp": "2026-10-08T00:00:30"
    }
  ],
  "visit_date_groups": [
    {"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5},
    {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0}
  ]
}
```

一致性要点：

- **汇总**：访问 3 人（u1/u2/u4）、转化 1 人（u1）、比例 1/3。
- **编号明细**：`visit_user_ids` 依次为 u1、u2、u4（Unicode 码点升序）；
  `converted_user_ids` 仅 u1，其成员都在访问数组中，数组长度等于对应人数。
- **配对明细**：仅 u1 一条。u1 只有一个 signup（8 日 00:00:30，即最早有效
  signup）；能与它在 60 秒内配对的段内 visit 只有 7 日 23:59:30（23:59:00
  间隔 90 秒、6 日 10:00:00 更远，均不满足），故 `visit_timestamp` 为
  `2026-10-07T23:59:30`，`signup_timestamp` 为 `2026-10-08T00:00:30`，对象
  只含 `user_id` / `visit_timestamp` / `signup_timestamp` 三个既有字段。
- **日期分组**：按日期升序。6 日组 u1、u4 共 2 人访问、1 人转化、比例 0.5；
  7 日组 u2 共 1 人访问、0 人转化、比例 0。u1 的段外历史（5 日 visit）不
  影响归组，仍归 6 日组；各组两种人数之和分别等于汇总人数（2+1=3、1+0=1）。

## 5. 同一样例改用 `--within-seconds 59`

只把上一条命令中的 `60` 换成 `59`：

- 访问人数与归组不变：`visit_users` 仍为 3，`visit_user_ids` 仍为
  `["u1", "u2", "u4"]`，两组仍为 6 日 2 人、7 日 1 人。
- u1 的最短间隔恰为 60 秒，超出 59 秒上界：**所有转化人数与比例归零**——
  `converted_users` 为 0、`conversion_rate` 为 0、各组 `converted_users`
  与 `conversion_rate` 均为 0；`converted_user_ids` 与 `conversion_pairs`
  均为空数组。

## 6. 缺少配对参数

只提供访问起点（省略 `--visit-before`，其余参数不变）：

```sh
python -m funnel report --db events.sqlite \
    --visit-from 2026-10-06T10:00:00 --within-seconds 60 \
    --include-users --include-pairs --group-by visit-date
```

行为：**退出码 2、标准输出为空**，标准错误指出 `--visit-from` 与
`--visit-before` 必须成对使用、已提供前者、缺少后者（参数拒绝先于一切
数据库访问），数据库已有记录不变。

## 7. 只读保证与验收要点

- 导入与成功报告退出码均为 0、标准错误为空；报告对数据库只执行
  `CREATE TABLE IF NOT EXISTS` 与 `SELECT`，报告前后事件记录逐行一致
  （含参数错误的报告尝试）。
- 验收以三者的对应关系为准：**完整输入**（第 2 节十条 JSONL）、**确定输出**
  （第 2、4、5、6 节的导入结果、报告 JSON 与退出码）、**测试对应**
  （`tests/test_window_grouped_report_consistency.py` 中各断言均核对解析后
  的 JSON 值与数组顺序，不绑定输出空格或错误全文）。
- 本次仅交付本说明与上述回归测试。产品源码、README、数据结构及现有输出
  协议保持现状，未新增任何功能。
