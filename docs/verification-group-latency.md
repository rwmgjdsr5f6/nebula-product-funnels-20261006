# 组内转化耗时（--include-group-latency）：源码行为核对说明

核对日期：2026-10-08
核对基线：HEAD `60ab036`，工作树在其上新增本功能（`funnel/__main__.py`
改动、`tests/test_report_include_group_latency.py` 新增）。

## 1. 核对依据与范围

本说明覆盖**本次新增**的组内转化耗时流程，即从 JSONL 导入到分组报告中每个
日期组内 `conversion_latency` 对象的完整统计链路：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> --group-by visit-date
       --include-group-latency [--include-latency]
       [--include-group-pairs] [--within-seconds N]
```

目的是让复核者用任务指定的七条事件，逐人核对"谁归到哪一天、每个转化用户
贡献哪一个耗时、各组 `conversion_latency` 的三个数值怎么算出来、无转化的
组为什么三项为 null"。结论全部指向当前仓库的真实文件、函数与判断条件。

`--include-group-latency` 是无取值开关，**只与 `--group-by visit-date`
合用**；它不依赖也不自动开启任何其他明细：顶层整体耗时仍只由
`--include-latency` 控制，组内配对仍只由 `--include-group-pairs` 控制。
不传新开关时既有输出逐字节不变。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、归组、配对归约、耗时装配、输出 |
| `tests/test_report_include_group_latency.py` | 组内耗时开关的 22 个回归测试（验收样例、归组与配对口径、开关独立性、空结果、乱序/重复、错误协议） |
| `docs/verification-conversion-latency.md` | 顶层整体耗时的核对说明（姊妹篇，耗时口径相同） |
| `docs/verification-group-pairs.md` | 组内配对明细的核对说明（姊妹篇，归组与组内装配相同） |
| `docs/verification-visit-date.md` | 按访问日期分组的核对说明（姊妹篇） |
| `docs/verification-visit-window.md` | 访问时段与转化时限的核对说明（姊妹篇） |
| `docs/verification-jsonl-funnel.md` | 导入与基础报告的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `TIMESTAMP_FORMAT` / `VALID_EVENTS` | 23 / 27 | 时间戳解析格式与合法事件 |
| `parse_within_seconds` | 161–180 | `--within-seconds` 取值校验（正整数，上界钳制） |
| `_visit_filter` | 183–200 | 段内 visit 过滤片段 `>= ? AND < ?`（含起点、不含终点） |
| `_converted_sql` / `_converted_params` | 203–231 | 有效配对枚举 SQL 与参数装配（汇总、编号、配对、归组共用） |
| `parse_group_by` | 234–244 | `--group-by` 取值校验（只接受 `visit-date`） |
| `_qualifying_visit_date_by_user` | 267–283 | 归组查询：每人最早合格 visit 的 UTC 日期 |
| `_reduce_pairs` | 287–301 | 配对归约：每人最早有效 signup 配对最晚可配对 visit |
| `_pairs_payload` | 305–320 | 由归约结果装配 `conversion_pairs` 数组（顶层与组内共用） |
| `_latency_payload` | 324–355 | 由归约结果装配 `conversion_latency` 三项数值（顶层与组内共用） |
| `_build_visit_date_groups` | 358–423 | 分组结果唯一装配点：人数、比例、组内编号、组内配对与组内耗时 |
| `cmd_report` | 426–582 | `report` 子命令：参数检查、查询、归约、归组、JSON 输出 |
| `cmd_report` 中 `--include-group-latency` 依赖检查 | 461–469 | 必须与 `--group-by visit-date` 合用，先于数据库访问 |
| `cmd_report` 中配对归约块 | 509–529 | 四个明细开关共用一次归约 |
| `cmd_report` 中 `--group-by` 块 | 530–558 | 归组查询、转化集合、装配调用 |
| `cmd_report` 中顶层耗时字段装配 | 576–578 | 顶层 `conversion_latency` 只由 `--include-latency` 控制 |
| `main` | 585–678 | argparse 子命令与参数装配（`--include-group-latency` 在 652–662） |

**数值来源声明（两种标记）：**

- 标 **【实测】** 的命令输出、退出码与标准错误文本，是 2026-10-08 在本机
  （Linux/WSL2，CPython 3.14.4）以当前工作树源码实际执行的观测结果；
  `funnel` 包经项目根目录解析（`PYTHONPATH` 指向项目根），样例数据放在
  临时目录，与"在项目根目录执行下文命令"等价。
- 其余结论标 **【源码推导】**，依据为 `funnel/__main__.py` 当前源码（含其
  SQL 与 Python 表达式）及 CPython 标准库 `json`/`argparse`/`sqlite3` 的
  行为。文中引用 `tests/test_report_include_group_latency.py` 的具名测试
  仅作**交叉核对**：测试断言本身不是实测证据。该测试文件本次已**整体执行
  一遍，22 个用例全部通过**（`python3 -m unittest
  tests.test_report_include_group_latency`，OK；全量 `python3 -m unittest
  discover -s tests` 为 208 个用例全部通过）——此句是对"测试被运行并通过"
  这一事实的实测记录，不代表其中断言被当作本文数值的来源。
- 若实际观察与本文推导冲突，以实际观察为准并据此修正本文，而不是反过来。

## 2. 从公开入口到组内 `conversion_latency`

以下每一步均可直接在源码中核对，串联起来就是组内耗时的完整链路：

1. **公开入口与参数解析。** `python -m funnel report` 由 `main`
   （`:585-678`）装配；`--include-group-latency` 是不带取值的开关
   （`action="store_true"`，`:652-662`），`--group-by` 带一个取值，合法值
   只有 `visit-date`（`type=parse_group_by`，`:663-674`）。参数解析在
   `parse_args`（`:677`）完成，非法取值在此阶段即以退出码 2 拒绝。
2. **开关依赖先于数据库检查。** `cmd_report`（`:426`）先做时段成对与先后
   检查（`:429-448`），再依次做两个组内开关的依赖检查：
   `--include-group-pairs`（`:453-459`）与 `--include-group-latency`
   （`:461-469`）。`args.include_group_latency` 为真而
   `args.group_by != "visit-date"` 时，标准错误指出该开关及
   `--group-by visit-date` 依赖并返回退出码 2——**先于
   `os.path.exists`（`:471-473`）、先于一切数据库访问**，不创建数据库、
   不改动记录（见第 8 节）。
3. **先确定"合格访问"的筛选条件。** 访问条件在分组之前一次性构造
   （`_visit_filter`，`:183-200`）：无时段时
   `visit_where = "WHERE event = 'visit'"`，即全库访问；有时段时为
   `WHERE event = 'visit' AND timestamp >= ? AND timestamp < ?`
   （**含起点、不含终点**）。同一个 `visit_where` / `visit_params` 同时
   用于汇总分母、`--include-users` 编号与归组查询——口径完全相同。
4. **有效行对枚举（SQL）。** 任一明细开关（`--include-pairs`、
   `--include-group-pairs`、`--include-latency`、`--include-group-latency`）
   开启时，`cmd_report` 调用
   `_converted_sql(args, "v.user_id, v.timestamp, s.timestamp")`
   （`:520-525`）——与汇总转化人数（`:488-490`）**同一函数、同一筛选
   条件**，仅 `select` 列表不同。该 SQL（`:203-222`）把 `events` 自连接：
   visit 一侧 `v.event = 'visit'`，signup 一侧 `s.event = 'signup'` 且
   **`s.timestamp > v.timestamp`（严格大于，`:212`）**；提供
   `--within-seconds N` 时追加秒差 `<= N`（`:214-219`，**上界含等值**）；
   提供访问时段时只对 visit 一侧加 `>= 起点 AND < 终点`（在 `:220-221`
   引用）。连接结果即全部**有效 (visit, signup) 行对**——耗时只从这些
   行对里产生。
5. **每人保留一组时间戳，且只归约一次。** `_reduce_pairs`（`:287-301`）
   遍历全部有效行对，按 `user_id` 归并到 `best_by_user`：
   - 若该 `signup_ts` **早于**当前保留的注册时刻，整组替换
     （`signup_ts < current[0]`，`:297-298`）——效果是先锁定**时间最早的
     有效注册**；
   - 若 `signup_ts` 与当前保留值**相等**而 `visit_ts` 更晚，只更新访问
     时刻（`:299-300`）——效果是在能与该最早注册配对的 visit 中取
     **时间最晚的一次**；
   - 晚于最早注册的 signup 行对两个分支都不命中，被丢弃——**后发生的
     注册不会替代最早有效注册**。

   归约在 `:509-525` 只执行一次，得到唯一的 `best_by_user`；顶层耗时
   （`:528-529`）与各日期组的组内耗时（`:552-558`）都从这一份结果装配，
   二者天然勾稽。
6. **归组与转化集合。** `--group-by visit-date` 时（`:530-558`）：
   `_qualifying_visit_date_by_user`（`:267-283`）执行
   `SELECT user_id, substr(MIN(timestamp), 1, 10) FROM events
   <visit_where> GROUP BY user_id`——先应用访问时段（无时段即全库），
   再按 `user_id` 取 `MIN(timestamp)`，前 10 个字符即 UTC 日期
   `YYYY-MM-DD`；`GROUP BY` 保证每人恰好一行，**每个用户编号只归一个
   日期组**。转化集合 `converted_id_set`（`:545-551`）由
   `_converted_sql(args, "DISTINCT v.user_id")` 得出，与汇总
   `converted_users` 同源。
7. **组内耗时装配。** `_build_visit_date_groups`（`:358-423`）是分组结果
   唯一的装配点：各组访问成员来自 `date_by_user`，转化成员是本组访问成员
   与转化集合的交集（`:399`）；`latency_by_user` 非 `None`（即
   `--include-group-latency` 开启，`:557` 传入 `best_by_user`）时，每组
   追加 `conversion_latency`（`:415-421`）——调用
   `_latency_payload(latency_by_user, converted_ids)`，即**本组转化成员
   在同一份归约结果中的耗时子集统计**。
8. **三项数值怎么算。** `_latency_payload`（`:324-355`）对给定用户集合中
   每个编号取 `best_by_user[user_id]` 的 `[signup_ts, visit_ts]`，用
   `strptime` 按 UTC 求差（`:340-347`）；随后：
   `min_seconds`/`max_seconds` 取最小/最大并转 `int`（`:352-353`），
   `mean_seconds` 为耗时总和除以人数的**真除**算术平均
   （`total / len(durations)`，`:354`，不取整、不人为舍入）；给定集合
   为空（无转化的组）时三项均为 `None`（`:348-349`），JSON 序列化为
   `null`。
9. **输出装配。** 汇总三字段在前（`:566-570`），随后按开关追加
   编号数组（`:571-573`）、顶层 `conversion_pairs`（`:574-575`）、顶层
   `conversion_latency`（`:576-578`），最后追加 `visit_date_groups`
   （`:579-580`），由 `json.dumps` 单行打印（`:581`）。

**为什么配对访问落在次日也不会移动所属组：** 归组（第 6 步）只回答"谁归
哪天"，输入是 `visit_where` 过滤后的 visit 行与 `MIN(timestamp)`；配对
（第 4–5 步）只回答"每个转化用户保留哪组时间戳"，输入是自连接枚举的有效
行对。两条链路在源码中是**两次独立的查询**，唯一交汇点是第 7 步：按"转化
成员的归组日期"把耗时挂到对应日期组。配对里的 `visit_timestamp` 可以是该
用户任意一次合格 visit，包括晚于归组日期的另一天；装配时没有任何代码据配对
时间重新归组——`date_by_user` 在配对归约之前已由 `MIN` 定稿，归约结果不
回写归组映射。验收样例的 u1 正是如此：耗时 30 秒来自 7 日的 visit，本人仍
归 6 日组。

## 3. 耗时与归组语义（逐条可在源码核对）

1. **每人只贡献一个耗时：最早有效 signup 配最晚可配对 visit。** 即
   `_reduce_pairs` 的两个分支（`:297-300`，见第 2 节第 5 步），与顶层
   `conversion_latency`、顶层/组内 `conversion_pairs` 出自**同一份**
   归约结果。**不是**取该用户所有配对中的最短间隔。筛选条件变化时最早有效
   signup 可能改变：某个注册在更紧的 `--within-seconds` 下若没有任何合格
   visit，便不再是有效注册（其行对在 SQL 枚举阶段即被排除，`:214-219`）。
2. **用户仍按最早合格 visit 的 UTC 日期归组。** 归组只看
   `visit_where` 过滤后的 `MIN(timestamp)`（`:267-283`）；配对访问在次日
   不移动所属组。组内耗时是"挂"在该组上的该用户唯一耗时。
3. **访问时段含起点、不含终点，段外访问不参与。** 时段片段同时拼在归组
   SQL（经 `visit_where`，`:276-282`）与转化 SQL 的 visit 一侧（`:220`）：
   段外 visit 既不参与归组，也不进入任何有效行对。
4. **注册可晚于时段终点。** 时段片段只拼在 visit 一侧（`:197-199` 与
   `:220`），signup 一侧唯一的时间条件是严格晚于 visit（`:212`）。
   交叉核对：`test_visit_window_signup_may_fall_after_end`（段内 visit、
   两天后 signup 仍算本组转化，耗时 86460 秒）。
5. **注册严格晚于访问；时限上界含等值。** `s.timestamp > v.timestamp`
   （`:212`）使同刻与逆序不成立；带 `--within-seconds N` 时追加秒差
   `<= N`（`:214-219`），间隔恰为 N 秒计入。交叉核对：
   `test_within_seconds_boundary_inclusive`（u2 间隔恰 60 秒计入、59 秒
   窗口被排除）。
6. **组内三项的口径。** `min_seconds`、`max_seconds` 为**整数秒**
   （`int(...)`，`:352-353`）；`mean_seconds` 为**本组**转化用户耗时之和
   除以**本组**转化人数（`:354`），真除不取整或人为舍入。分母是组内人数，
   不是全体转化人数——多组并存时组内均值与顶层均值可以不同（见第 7 节
   对照）。交叉核对：`test_group_mean_is_unrounded_and_scoped_per_group`。
7. **无转化的组三项均为 null。** 该组 `converted_ids` 为空集合，
   `_latency_payload(..., set())` 的 `durations` 为空，在 `:348-349` 返回
   三个 `None`；开关开启时每个已生成的组对象都含 `conversion_latency` 键。
   **无合格访问时分组数组整体为空**（没有组对象可挂，`:396-423`）。
   交叉核对：`test_no_conversion_group_gives_nulls_other_group_kept`、
   `test_no_qualifying_visits_gives_empty_groups`。
8. **各组组内耗时的人数之和等于总转化人数。** 每个转化用户只归一个日期组
   （`GROUP BY user_id`，`:276-282`），故各组 `converted_ids` 两两不交、
   并集即全体转化用户；组内耗时与顶层耗时又出自同一份 `best_by_user`。
   因而各组 min/max 的并集取极值即顶层 min/max，各组耗时总和除以总人数即
   顶层均值。交叉核对：
   `test_top_level_latency_independent_from_group_latency`。
9. **与组内配对逐人一致。** 同开 `--include-group-pairs` 时，组内
   `conversion_pairs` 里每个用户的 `signup_timestamp - visit_timestamp`
   就是该用户计入组内耗时的那一个数。交叉核对：
   `test_group_latency_consistent_with_group_pairs`。
10. **行序、重复事件、重复导入不改变结果。** 归约只比较时间戳的 min/max
    （`:297-300`），归组是 `MIN` 聚合，都与物理行序无关；重复事件行（含
    整批再次导入产生的相同行）产生相同行对，不改变结果。交叉核对：
    `test_shuffled_and_duplicate_events_give_same_group_latency`、
    `test_reimport_same_batch_keeps_group_latency`。
11. **未开启开关时不追加该字段，开关不自动开启其他明细。**
    `latency_by_user` 只在 `args.include_group_latency` 为真时传入
    （`:557`），否则组对象不出现耗时字段；新开关不向顶层追加
    `conversion_latency`（顶层由 `--include-latency` 单独控制，`:576-578`），
    不追加顶层或组内配对（`--include-pairs` / `--include-group-pairs`），
    也不追加编号数组（`--include-users`）。交叉核对：
    `test_flag_does_not_auto_enable_other_details`、
    `test_without_flag_groups_have_no_latency_field`、
    `test_metrics_identical_with_and_without_flag`、
    `test_top_level_latency_independent_from_group_latency`。

## 4. 固定合成样例（任务指定的七条事件）

以下七条完整 UTF-8 JSONL 记录（字段沿用现有格式：`user_id` / `event` /
`timestamp`，与 `parse_line` 的校验一一对应），保存为 `group.jsonl`：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:30"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T10:01:00"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T12:00:00"}
{"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T12:01:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T11:00:00"}
```

人物设定（时间均为 UTC）：

- **u1**：6 日 23:59:00 visit，次日（7 日）10:00:00、10:00:30 各 visit
  一次，7 日 10:01:00 signup。
- **u2**：6 日 12:00:00 visit、12:01:00 signup。
- **u3**：仅 7 日 11:00:00 visit，无 signup。

## 5. 一次导入新库

```sh
python -m funnel import group.jsonl --db events.sqlite
```

**【实测】** 标准输出（一行，退出码 0，标准错误为空）：

```json
{"imported": 7}
```

七条记录全部通过 `parse_line`，`len(events)` 即 7（`cmd_import`
`:132-158`）。数据库文件不存在时导入会新建它并建表；导入是事务内追加，对
已有库重复执行会追加相同行而非替换。`json.dumps` 默认分隔符在冒号后输出
一个空格，逐字节文本即 `{"imported": 7}`。

## 6. 验收报告：分组 + 组内耗时 + 60 秒窗口

任务指定的验收命令：

```sh
python -m funnel report --db events.sqlite --group-by visit-date \
    --include-group-latency --within-seconds 60
```

**【实测】** 完整标准输出（一行，退出码 0，标准错误为空）：

```json
{"visit_users": 3, "converted_users": 2, "conversion_rate": 0.6666666666666666, "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 2, "conversion_rate": 1.0, "conversion_latency": {"min_seconds": 30, "max_seconds": 60, "mean_seconds": 45.0}}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0, "conversion_latency": {"min_seconds": null, "max_seconds": null, "mean_seconds": null}}]}
```

交叉核对：`test_acceptance_seven_events_within_60`、
`test_acceptance_seven_events_within_60_types`。字段顺序即 `payload`
装配顺序（`:566-580`）：三个汇总字段、`visit_date_groups`；组对象内四个
基础字段在前、`conversion_latency` 在最后（`:400-421`）。注意顶层**没有**
`conversion_latency`——新开关不自动开启顶层耗时。

### 6.1 归组（无时段即全库，每人取最早合格 visit 的 UTC 日期）

归组查询（`:267-283`）对三人各产出一行：

| 用户 | 全部 visit | `MIN(timestamp)` | 归入日期 |
|---|---|---|---|
| u1 | 10-06 23:59:00、10-07 10:00:00、10-07 10:00:30 | `2026-10-06T23:59:00` | `2026-10-06` |
| u2 | 10-06 12:00:00 | `2026-10-06T12:00:00` | `2026-10-06` |
| u3 | 10-07 11:00:00 | `2026-10-07T11:00:00` | `2026-10-07` |

**u1 虽在 7 日有两次 visit，仍只归 6 日组**（每人仅一组，`GROUP BY
user_id` + `MIN`）。

### 6.2 汇总与逐人耗时来源（60 秒窗口）

- **分母 `visit_users = 3`**：三人各至少有一次全库 visit，
  `COUNT(DISTINCT user_id)`。
- **u1：转化，归 6 日组，贡献 30 秒。** 有效行对枚举（`:203-222`）在
  60 秒窗口下对 u1 只保留 7 日的两组：signup 10:01:00 分别与 visit
  10:00:00（间隔 60 秒）、10:00:30（间隔 30 秒）配对，二者都满足严格晚于
  且 `<= 60`；与 6 日 23:59:00 visit 的间隔超过 10 小时，被窗口排除。
  归约（`:287-301`）锁定唯一（最早也仅有）的有效 signup 10:01:00，再取
  能与它配对的最晚 visit 10:00:30，耗时
  `10:01:00 - 10:00:30 = 30` 秒。**配对 visit 是 7 日的，u1 的组仍是
  6 日**（第 2 节末段、第 3 节第 2 条）。
- **u2：转化，归 6 日组，贡献 60 秒。** 唯一 visit 12:00:00 与唯一
  signup 12:01:00 构成唯一有效行对，间隔恰 60 秒，上界含等值
  （`:214-219`），计入。
- **u3：不转化，归 7 日组。** 只有 visit 没有 signup，枚举不到行对；
  计入 7 日组访问人数，不计转化。
- **分子 `converted_users = 2`**，**顶层比例 `2/3`** 真除法（`:565`）
  序列化即 `0.6666666666666666`。

### 6.3 两组耗时与勾稽

| visit_date | visit_users | converted_users | conversion_rate | conversion_latency |
|---|---|---|---|---|
| `2026-10-06` | 2（u1、u2） | 2（u1、u2） | `2/2 = 1.0` | min 30 / max 60 / mean 45.0 |
| `2026-10-07` | 1（u3） | 0 | `0/1 = 0.0` | 三项 `null` |

- **6 日组**：两个耗时为 30 与 60 秒——`min_seconds = 30`、
  `max_seconds = 60`（`int`），`mean_seconds = (30 + 60) / 2` 真除得
  浮点 `45.0`（`:350-354`），JSON 文本即 `45.0`。
- **7 日组**：`converted_ids` 为空，`_latency_payload` 在 `:348-349`
  返回三个 `None`，JSON 为 `null`；组对象仍含 `conversion_latency` 键
  （不是缺键）。
- **人数勾稽**：`2 + 1 = 3 = visit_users`；`2 + 0 = 2 =
  converted_users`。组内比例 `2/2 = 1.0`、`0/1 = 0.0` 都走真除法
  （`:404`），JSON 文本带 `.0`。
- 若同库再开 `--include-latency`，顶层 `conversion_latency` 同为
  30/60/45.0（本组即全体转化用户）；多组并存时两者可以不同，见第 7 节。

## 7. 开关独立性与未开启行为（实测对照）

**【实测】** 不开新开关（`--group-by visit-date --within-seconds 60`），
组对象维持原有四个键、不追加 `conversion_latency`，顶层也没有该字段——
既有输出逐字节不变：

```json
{"visit_users": 3, "converted_users": 2, "conversion_rate": 0.6666666666666666, "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 2, "conversion_rate": 1.0}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0}]}
```

**【实测】** 同库同开 `--include-latency`：顶层与 6 日组各有一个
`conversion_latency`（此处数值恰好相同，因为转化用户都在 6 日组），
7 日组仍为三项 `null`，两个字段各自由独立开关控制：

```json
{"visit_users": 3, "converted_users": 2, "conversion_rate": 0.6666666666666666, "conversion_latency": {"min_seconds": 30, "max_seconds": 60, "mean_seconds": 45.0}, "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 2, "conversion_rate": 1.0, "conversion_latency": {"min_seconds": 30, "max_seconds": 60, "mean_seconds": 45.0}}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0, "conversion_latency": {"min_seconds": null, "max_seconds": null, "mean_seconds": null}}]}
```

**【源码推导】组内均值只按本组人数、与顶层均值可以不同。** 设 6 日组
a、b 分别耗时 30、61 秒（组内均值 45.5），7 日组 c 耗时 5 秒：顶层均值
为 `(30 + 61 + 5) / 3 = 32.0`，与任一组都不同。这证明 `mean_seconds`
的分母是传入 `_latency_payload` 的**本组** `converted_ids`
（`:339-354`），而不是全体转化用户。实测见
`test_group_mean_is_unrounded_and_scoped_per_group`。

**【实测】** 59 秒窗口下 u2 的 60 秒间隔被排除：6 日组只剩 u1 的 30 秒，
7 日组仍三项 `null`——窗口同时改变转化集合与各组耗时：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5, "conversion_latency": {"min_seconds": 30, "max_seconds": 30, "mean_seconds": 30.0}}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0, "conversion_latency": {"min_seconds": null, "max_seconds": null, "mean_seconds": null}}]}
```

交叉核对：`test_flag_does_not_auto_enable_other_details`、
`test_without_flag_groups_have_no_latency_field`、
`test_top_level_latency_independent_from_group_latency`、
`test_within_seconds_boundary_inclusive`。

## 8. 错误协议

### 8.1 单独使用 `--include-group-latency`：访问数据库之前拒绝

**【源码推导】** 开关依赖检查（`:461-469`）在 `os.path.exists`
（`:471-473`）与 `sqlite3.connect`（`:476`）之前执行：
`args.include_group_latency` 为真而 `args.group_by != "visit-date"`（含
未传 `--group-by` 的 `None`）时，打印错误行并返回退出码 2——不创建数据
库文件，已存在的库记录不变。交叉核对：
`test_flag_without_group_by_rejected`、
`test_flag_without_group_by_does_not_create_db`、
`test_report_keeps_events_unchanged`。

**【实测】** 数据库路径不存在时执行

```sh
python -m funnel report --db missing.sqlite --include-group-latency
```

退出码 2、标准输出为空（0 字节），标准错误逐字为：

```text
--include-group-latency 必须与 --group-by visit-date 合用：已提供 --include-group-latency，缺少 --group-by visit-date
```

目标数据库文件**未被创建**（错误行同时指出该开关与 `--group-by
visit-date` 依赖；此处**不是**"数据库不存在"的路径错误——开关依赖检查
先于存在性检查）。

**【实测】** 数据库已存在且含记录时（同一条错误命令，另加
`--within-seconds 60`）：退出码 2、标准输出为空、标准错误同上；命令前后
对 `events` 表做全表快照比对，记录逐行一致——**未改动任何事件记录**。

### 8.2 数据库不存在或不可访问：路径错误协议

**【实测】** 开关组合合法但数据库不存在时执行

```sh
python -m funnel report --db /tmp/none.sqlite --group-by visit-date --include-group-latency
```

退出码 2、标准输出为空，标准错误为：

```text
/tmp/none.sqlite: 数据库不存在
```

含数据库路径与原因（`os.path.exists` 检查，`:471-473`，先于
`sqlite3.connect`，不会顺手创建该库）。已存在但无法访问的库由
`sqlite3.Error` 分支处理（`:561-563`），同样退出码 2、标准输出为空、
标准错误含路径与原因。交叉核对：`test_missing_db_still_follows_path_error_protocol`。

## 9. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 4 节七条 JSONL 逐字节）、
  **确定输出**（第 5 节导入 JSON、第 6 节验收报告 JSON、第 7 节对照输出、
  第 8 节的退出码与标准错误形态）、**源码对应**（每条结论标注的
  `funnel/__main__.py` 行号与条件）。
- 逐人复核顺序建议：先按第 6.1 节归组表确认每人日期（u1 归 6 日，与其
  配对访问落在 7 日无关），再按第 6.2 节确认每人唯一耗时（u1=30 来自
  最早 signup 10:01:00 配最晚可配对 visit 10:00:30；u2=60 恰为上界
  等值；u3 无 signup），最后用第 6.3 节对账——6 日组 30/60/45.0、7 日组
  三项 null，人数 `2+1=3`、转化 `2+0=2`。
- 既有汇总、明细、排序与事件记录不变：不传 `--include-group-latency`
  时输出与本功能加入前逐字节一致（第 7 节首条实测）；报告对数据库只执行
  `CREATE TABLE IF NOT EXISTS` 与 `SELECT`。
- 本文数值分两类标记：**【实测】** 为当前工作树源码在本机（Linux/WSL2，
  CPython 3.14.4）的实际执行观测；**【源码推导】** 为静态推导，并以
  `tests/test_report_include_group_latency.py` 的具名测试交叉核对
  （该文件 22 个用例本次全部通过；测试断言本身不作为实测证据）。复核时
  若实际观察与推导冲突，以实际观察为准并修正本文。
