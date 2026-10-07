# 按访问日期分组报告（--group-by visit-date）：源码行为核对说明

核对日期：2026-10-07
核对基线：当前工作树，HEAD `78356d5`，工作树干净。

## 1. 核对依据与范围

本说明只覆盖一条**已有**报告流程：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> --group-by visit-date
       [--within-seconds N]
       [--visit-from YYYY-MM-DDTHH:MM:SS --visit-before YYYY-MM-DDTHH:MM:SS]
```

目的是让复核者用一份小型合成输入，逐人核对"谁归到哪一天的组、组内访问与
转化人数怎样从事件得到"，以及分组数组与顶层汇总、用户编号、配对明细之间的
关系。结论全部指向当前仓库的真实文件、函数与条件；本说明不要求也不描述任何
功能变更，现有命令、输出字段、追加导入、用户原值去重、报告不改写事件记录的
行为均保持现状。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、分组与输出 |
| `tests/test_group_by_visit_date.py` | 分组语义、验收样例、参数拒绝、报告只读的回归测试 |
| `docs/verification-jsonl-funnel.md` | 导入与无时段报告的核对说明（姊妹篇） |
| `docs/verification-visit-window.md` | 访问时段筛选与转化时限的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `parse_line` / `load_events` | 54–128 | 单行校验与整文件严格 UTF-8 校验 |
| `cmd_import` | 131–157 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 160–179 | `--within-seconds` 取值校验 |
| `_visit_window_clause` | 182–186 | 段内 visit 过滤片段 `>= ? AND < ?` |
| `_converted_sql` / `_converted_params` | 189–217 | 转化人数/编号/配对查询与参数装配 |
| `parse_group_by` | 220–230 | `--group-by` 取值校验（只接受 `visit-date`） |
| `parse_visit_bound` | 233–250 | `--visit-from` / `--visit-before` 取值校验 |
| `cmd_report` | 253–405 | `report` 子命令：参数检查、查询、分组、JSON 输出 |
| 分组主体（`args.group_by == "visit-date"` 分支） | 345–384 | 归组、组内计数、数组装配 |
| 汇总与输出装配 | 391–404 | 顶层字段与 `visit_date_groups` 追加 |
| `main` | 408–468 | argparse 子命令装配（`--group-by` 定义在 456–464） |

**数值来源声明：** 下文第 3–6 节的导入与分组报告命令及其预期输出，已于
2026-10-07 在本机以当前源码**实际执行**（临时目录、独立 SQLite 文件），
观察到的退出码与标准输出逐字节符合文中预期，标记为**实测**；第 7–8 节的
参数拒绝与缺库行为同样实际执行过，标记为**实测**。其余未逐条执行的结论
（如空数组形态）为对源码的**静态推导**，并引用
`tests/test_group_by_visit_date.py` 中既有测试核对。若实际观察与本文推导
不符，以实际观察为准并据此修正本文，而不是反过来。

## 2. 分组语义：先筛时段，再按最早合格 visit 的 UTC 日期归组

以下每条均可直接在源码中核对：

1. **先应用访问时段，再归组。** 分组与汇总共用同一份段内 visit 过滤条件：
   提供 `--visit-from/--visit-before` 时为
   `WHERE event = 'visit' AND timestamp >= ? AND timestamp < ?`
   （含起点、不含终点），不提供时段时为 `WHERE event = 'visit'`（全库访问）
   （`cmd_report`，`funnel/__main__.py:285-294`；变量 `visit_where` 同时
   喂给分母查询 `:295-298` 与归组查询 `:351-356`）。
2. **每人只归一个组：最早合格 visit 的 UTC 日期。** 归组查询为
   `SELECT user_id, substr(MIN(timestamp), 1, 10) FROM events <visit_where>
   GROUP BY user_id`（`:351-356`）：对每个用户取**段内最早** visit 的时间戳，
   前 10 个字符即 UTC 日期 `YYYY-MM-DD`（时间戳为定宽 ISO 文本，见
   `:346-349` 注释）。一个 `user_id` 在 `date_by_user` 中只占一键，因此
   每人仅属于一组；段外历史不参与归组。
3. **转化判定与汇总完全一致，不限于归组那次访问。** 组内转化人数来自
   `_converted_sql(args, "DISTINCT v.user_id")`（`:360-366`），与顶层
   `converted_users` 用的是同一函数、同一参数（`:299-301`）：自连接枚举该
   用户全部 `(合格 visit, signup)` 行对，任一满足 `s.timestamp >
   v.timestamp`（严格大于，`:198`）即转化；带 `--within-seconds N` 时追加
   秒差 `<= N`（**上界包含等值**，`:201-205`）。配对用的 visit 可以是该用户
   任意一次合格访问，不要求等于归组用的最早那次；signup 一侧没有时段条件，
   **注册可以晚于时段终点**。
4. **日期组升序、只列有访问用户的日期。** 数组按 `sorted(visits_by_date)`
   生成（`:375-384`），字典序即日期升序；只有至少一名用户归入的日期才出现，
   没有访问用户的日期不产生条目。
5. **两种人数的分组总和分别等于汇总。** 每个合格用户恰好归入一组（第 2 条），
   组内转化是"归组 ∩ 转化集合"的计数（`:369-372`），因此
   `sum(visit_users)` 等于顶层 `visit_users`，`sum(converted_users)` 等于
   顶层 `converted_users`（回归证据：`assertGroups`，
   `tests/test_group_by_visit_date.py:120-140`）。
6. **比例按组内人数计算。** 每组 `conversion_rate = converted_users /
   visit_users`（`:380-381`），是组内真除法，与顶层比例（`:391`）各自独立
   计算；组内无人转化时为浮点 `0.0`。
7. **无合格访问时数组为空。** 没有任何 visit 落入筛选（全库无 visit，或时段
   内无 visit）时 `visits_by_date` 为空，`visit_date_groups` 为 `[]`
   （`:375-384`；回归证据：`test_no_qualifying_visits_gives_empty_groups`、
   `test_window_without_visits_gives_empty_groups`）。
8. **重复事件、重复导入、行序不改变人数。** 人数一律
   `COUNT(DISTINCT user_id)` / `DISTINCT`，归组取 `MIN(timestamp)`，完全相同
   的事件行或同一批事件的再次导入不产生新用户、不改变最早时间戳；行序不影响
   `MIN` 与集合归约（回归证据：
   `test_shuffled_and_duplicate_events_give_same_groups`、
   `test_reimport_same_batch_keeps_groups`）。
9. **未启用分组时不输出该数组；启用后其余输出不变。** `visit_date_groups`
   只在 `args.group_by == "visit-date"` 时追加（`:402-403`）；不传
   `--group-by` 时输出与之前完全一致（回归证据：
   `test_no_group_by_leaves_output_unchanged`）。分组与 `--include-users` /
   `--include-pairs` 可叠加：顶层的 `visit_user_ids`、`converted_user_ids`、
   `conversion_pairs` 保持原有语义与排序，分组只是追加（回归证据：
   `test_groups_coexist_with_include_users_and_pairs`）。`cmd_report` 对数据库
   只执行 `CREATE TABLE IF NOT EXISTS` 与 `SELECT`，分组报告不改写任何事件
   记录（回归证据：`test_grouped_report_keeps_events_unchanged`）。

## 3. 固定合成样例（2026 年 10 月，六条事件）

以下六条完整 UTF-8 JSONL 记录（字段沿用现有格式：`user_id` / `event` /
`timestamp`，与 `parse_line` 的校验一一对应），保存为 `acceptance.jsonl`：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T10:00:30"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T11:00:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T12:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-07T12:00:00"}
```

人物设定：u1 在 6 日 10:00:00 与 7 日 10:00:00 各访问一次，7 日 10:00:30
注册；u2 仅在 6 日 11:00:00 访问；u3 在 7 日 12:00:00 同一时刻访问并注册。
（与 `tests/test_group_by_visit_date.py` 的 `ACCEPTANCE_EVENTS` 逐条一致，
`:29-36`。）

导入新库（数据库文件不存在时导入会新建它并建表，`funnel/__main__.py:141-151`）：

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

预期标准输出（一行，退出码 0，**实测**）：

```json
{"imported": 6}
```

六条记录全部通过 `parse_line`，`len(events)` 即 6（`:156`）。注意
`json.dumps` 默认分隔符会在冒号后输出一个空格，逐字节文本是
`{"imported": 6}`；本文其余 JSON 预期同理，按 JSON 值比较时与紧凑写法等价。

## 4. 全库分组报告：--group-by visit-date --within-seconds 60

```sh
python -m funnel report --db events.sqlite \
    --group-by visit-date --within-seconds 60
```

预期标准输出（一行，退出码 0，**实测**）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0}]}
```

未提供时段，归组与汇总都基于全库访问（第 2 节第 1 条）。逐人核对：

- **u1 → 6 日组，转化。** 合格 visit 有 6 日 10:00:00 与 7 日 10:00:00 两次，
  `MIN` 取 6 日 10:00:00，前 10 字符 `2026-10-06`，归入 6 日组（每人仅一组，
  7 日那次不再产生第二个组）。转化判定不限于归组那次访问：7 日 visit
  10:00:00 与 signup 10:00:30 满足严格大于且间隔 30 秒 ≤ 60，转化成立，
  计入 6 日组的转化人数（配对用的是 7 日那次 visit，归组用的是 6 日那次，
  二者本来就不必是同一次）。
- **u2 → 6 日组，不转化。** 唯一 visit 在 6 日 11:00:00，归 6 日组；没有任何
  signup 行，连接找不到 `s` 行，不转化。
- **u3 → 7 日组，不转化。** 唯一 visit 在 7 日 12:00:00，归 7 日组；signup
  与 visit 同一时刻，`s.timestamp > v.timestamp` 严格大于不成立（相等时刻
  不转化），不计入。

汇总与分组的关系（第 2 节第 5、6 条）：

- 汇总 `visit_users = 3`（u1、u2、u3），`converted_users = 1`（u1），
  `conversion_rate = 1 / 3`，双精度浮点经 `json.dumps` 序列化为
  `0.3333333333333333`。
- 6 日组：u1、u2 两人访问，u1 一人转化，`conversion_rate = 1 / 2 = 0.5`。
- 7 日组：u3 一人访问，无人转化，`conversion_rate = 0 / 1`，浮点 `0.0`
  （JSON 文本为 `0.0`）。
- 两组访问人数之和 `2 + 1 = 3`、转化人数之和 `1 + 0 = 1`，分别等于汇总；
  日期按升序排列，只出现 6 日与 7 日两个有访问用户的日期。

（与 `test_acceptance_full_db_within_60` 的断言一致，
`tests/test_group_by_visit_date.py:151-168`。）

## 5. 只保留 7 日 UTC 全天访问的分组报告

```sh
python -m funnel report --db events.sqlite \
    --group-by visit-date --within-seconds 60 \
    --visit-from 2026-10-07T00:00:00 --visit-before 2026-10-08T00:00:00
```

预期标准输出（一行，退出码 0，**实测**）：

```json
{"visit_users": 2, "converted_users": 1, "conversion_rate": 0.5, "visit_date_groups": [{"visit_date": "2026-10-07", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5}]}
```

逐人核对（时段含起点、不含终点，段外历史不参与归组，第 2 节第 1、2 条）：

- **u1 → 7 日组，转化。** 6 日 10:00:00 的 visit 在段外被排除；段内最早合格
  visit 变为 7 日 10:00:00，归入 7 日组——同一批事件在不同筛选下归组可以
  不同，因为归组只看**段内**最早 visit。signup 10:00:30 与该 visit 间隔
  30 秒 ≤ 60，转化成立。
- **u2 → 不出现。** 唯一 visit 在 6 日，段内没有任何 visit，既不进入分母也
  不产生分组条目。
- **u3 → 7 日组，不转化。** 与第 4 节相同：相等时刻不转化。

汇总与唯一的 7 日组完全一致：访问 2 人、转化 1 人、比例 `0.5`；6 日没有
任何合格访问用户，数组中不出现 6 日条目（第 2 节第 4 条）。

（与 `test_acceptance_window_only_7th` 的断言一致，
`tests/test_group_by_visit_date.py:170-189`。）

## 6. 非法或缺值的 --group-by：退出码 2、标准输出为空、先于数据库访问

`parse_group_by`（`:220-230`）只接受 `visit-date`，其余一切取值——空串、
其他维度名、大小写变体、带空白——都抛出 `argparse.ArgumentTypeError`，由
argparse 在 `parse_args`（`:467`）阶段以**退出码 2** 结束：标准输出为空，
标准错误指出参数 `--group-by` 与原因（"只接受 visit-date……"及所给原值）。
`--group-by` 后缺参数值同样由 argparse 以退出码 2 拒绝，标准错误指出
`--group-by` 缺少参数值。两类拒绝都发生在 `cmd_report` 运行之前，更在任何
数据库访问之前：**不创建数据库文件**，已存在的库记录不变（**实测**；回归
证据：`test_invalid_group_by_rejected_before_db_access`、
`test_group_by_missing_value_rejected`、
`test_invalid_group_by_does_not_create_db`）。

```sh
python -m funnel report --db events.sqlite --group-by signup-date
# 实测：退出码 2，标准输出为空，标准错误含 --group-by 与 "只接受 visit-date"
```

## 7. 参数合法但数据库不存在

`--group-by visit-date` 合法但 `--db` 路径不存在时，`cmd_report` 的
`os.path.exists` 检查（`:277-279`）返回**退出码 2、标准输出为空**，标准
错误为：

```text
<数据库路径>: 数据库不存在
```

包含数据库路径与不存在原因；判断发生在 `sqlite3.connect` 之前，**不会顺手
创建该库**（**实测**；回归证据：`test_missing_db_follows_path_error_protocol`）。

```sh
python -m funnel report --db /path/to/missing.sqlite --group-by visit-date
# 实测的 stderr：/path/to/missing.sqlite: 数据库不存在
```

## 8. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 3 节六条 JSONL 逐字节）、
  **确定输出**（第 3–5 节的导入与分组报告 JSON、第 6–7 节的退出码与标准
  错误形态）、**源码对应**（每条结论标注的 `funnel/__main__.py` 行号与
  条件）。
- 第 3–7 节的命令与输出为本机实测（2026-10-07，当前源码）；未逐条执行的
  结论为源码静态推导，并以 `tests/test_group_by_visit_date.py` 的既有测试
  核对。复核时若实际观察与本文冲突，以实际观察为准并修正本文。
- 本次仅交付本说明。现有命令与参数、输出字段（`imported` 与
  `visit_users`/`converted_users`/`conversion_rate` 及可选的
  `visit_user_ids`/`converted_user_ids`/`conversion_pairs`/
  `visit_date_groups`）、导入的追加语义、按 `user_id` 原值去重、报告不改写
  事件记录的行为均保持现状，未新增任何功能。
