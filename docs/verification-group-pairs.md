# 组内转化配对明细（--include-group-pairs）：源码行为核对说明

核对日期：2026-10-07
核对基线：HEAD `e315640`，工作树干净。

## 1. 核对依据与范围

本说明覆盖**已有**的组内配对明细流程，即从 JSONL 导入到分组报告中每个
日期组内 `conversion_pairs` 数组的完整统计链路：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> --group-by visit-date
       --include-group-pairs [--include-pairs] [--within-seconds N]
```

目的是让复核者用一份小型合成输入，逐人核对"谁归到哪一天、谁算转化、每个
转化用户在顶层与所在日期组里各自保留哪一组配对时间戳、各组配对合并后与
顶层是什么关系"。结论全部指向当前仓库的真实文件、函数与判断条件；本说明
不要求也不描述任何功能变更，现有命令入口、输出字段、SQLite 表结构、追加
导入协议、访问时段筛选语义、报告不改写事件记录的行为均保持现状。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、归组、配对归约、输出 |
| `tests/test_report_include_group_pairs.py` | 组内配对开关的 17 个回归测试（验收样例、开关独立性、组内配对语义、不变量、错误协议） |
| `docs/verification-visit-date.md` | 按访问日期分组的核对说明（姊妹篇） |
| `docs/verification-conversion-pairs.md` | 顶层配对明细的核对说明（姊妹篇） |
| `docs/verification-visit-window.md` | 访问时段与转化时限的核对说明（姊妹篇） |
| `docs/verification-jsonl-funnel.md` | 导入与基础报告的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `TIMESTAMP_RE` / `VALID_EVENTS` | 21 / 26 | 导入侧时间格式与合法事件 |
| `parse_line` / `load_events` | 54–128 | 单行校验与整文件严格 UTF-8 校验 |
| `cmd_import` | 131–157 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 160–179 | `--within-seconds` 取值校验（正整数，上界钳制） |
| `_visit_window_clause` | 182–186 | 段内 visit 过滤片段 `>= ? AND < ?` |
| `_converted_sql` / `_converted_params` | 189–217 | 有效配对枚举 SQL 与参数装配（汇总、编号、配对、分组共用） |
| `parse_group_by` | 220–230 | `--group-by` 取值校验（只接受 `visit-date`） |
| `_qualifying_visit_date_by_user` | 253–269 | 归组查询：每人最早合格 visit 的 UTC 日期 |
| `_reduce_pairs` | 272–287 | 配对归约：每人最早有效 signup 配对最晚可配对 visit |
| `_pairs_payload` | 290–306 | 由归约结果装配 `conversion_pairs` 数组（顶层与组内共用） |
| `_build_visit_date_groups` | 309–355 | 分组结果唯一装配点：人数、比例、组内编号与组内配对 |
| `cmd_report` | 358–495 | `report` 子命令：参数检查、查询、归约、归组、JSON 输出 |
| `cmd_report` 中开关依赖检查 | 385–391 | `--include-group-pairs` 必须与 `--group-by visit-date` 合用 |
| `cmd_report` 中配对归约块 | 436–448 | 顶层与组内共用的一次性归约 |
| `cmd_report` 中 `--group-by` 块 | 449–474 | 归组查询、转化集合、装配调用 |
| `main` | 498–569 | argparse 子命令与参数装配（`--include-group-pairs` 在 547–554） |

**数值来源声明（两种标记）：**

- 标 **【实测】** 的命令输出、退出码与标准错误文本，是 2026-10-07 在本机
  （Linux/WSL2，CPython 3.14.4）以当前工作树源码实际执行的观测结果；
  `funnel` 包经项目根目录解析（`PYTHONPATH` 指向项目根），样例数据放在
  临时目录，与"在项目根目录执行下文命令"等价。
- 其余结论标 **【源码推导】**，依据为 `funnel/__main__.py` 当前源码（含其
  SQL 与 Python 表达式）及 CPython 标准库 `json`/`argparse`/`sqlite3` 的
  行为。文中引用 `tests/test_report_include_group_pairs.py` 的具名测试仅
  作**交叉核对**：测试断言本身不是实测证据。该测试文件本次已**整体执行
  一遍，17 个用例全部通过**（`python3 -m unittest
  tests.test_report_include_group_pairs`，OK）——此句是对"测试被运行并
  通过"这一事实的实测记录，不代表其中断言被当作本文数值的来源。
- 若实际观察与本文推导冲突，以实际观察为准并据此修正本文，而不是反过来。

## 2. 从公开入口到组内 `conversion_pairs`

以下每一步均可直接在源码中核对，串联起来就是组内配对明细的完整链路：

1. **公开入口与参数解析。** `python -m funnel report` 由 `main`
   （`:498-569`）装配；`--include-group-pairs` 是不带取值的开关
   （`action="store_true"`，`:547-554`），`--group-by` 带一个取值，合法值
   只有 `visit-date`（`type=parse_group_by`，`:555-565`）。参数解析在
   `parse_args`（`:568`）完成，非法取值在此阶段即以退出码 2 拒绝。
2. **开关依赖先于数据库检查。** `cmd_report`（`:358`）先做时段成对与先后
   检查（`:361-380`），再做开关依赖检查（`:385-391`）：
   `args.include_group_pairs` 为真而 `args.group_by != "visit-date"` 时，
   标准错误指出该开关及 `--group-by visit-date` 依赖并返回退出码 2——
   **先于 `os.path.exists`（`:393-395`）、先于一切数据库访问**，不创建
   数据库、不改动记录（见第 8 节）。
3. **先确定"合格访问"的筛选条件。** 访问条件在分组之前一次性构造
   （`:401-410`）：无时段时 `visit_where = "WHERE event = 'visit'"`，即
   全库访问；有时段时为
   `WHERE event = 'visit' AND timestamp >= ? AND timestamp < ?`
   （含起点、不含终点）。同一个 `visit_where` / `visit_params` 同时用于
   汇总分母（`:411-414`）、`--include-users` 编号（`:418-435`）与归组
   查询——口径完全相同。
4. **有效行对枚举（SQL）。** 任一明细开关（`--include-pairs` 或
   `--include-group-pairs`）开启时，`cmd_report` 调用
   `_converted_sql(args, "v.user_id, v.timestamp, s.timestamp")`
   （`:441-446`）——与汇总转化人数（`:415-417`）**同一函数、同一筛选
   条件**，仅 `select` 列表不同。该 SQL（`:189-208`）把 `events` 自连接：
   visit 一侧 `v.event = 'visit'`，signup 一侧 `s.event = 'signup'` 且
   **`s.timestamp > v.timestamp`（严格大于，`:198`）**；提供
   `--within-seconds N` 时追加秒差 `<= N`（`:200-205`，上界含等值）；
   提供访问时段时只对 visit 一侧加 `>= 起点 AND < 终点`
   （`_visit_window_clause`，`:182-186`，在 `:207` 引用）。连接结果即全部
   **有效 (visit, signup) 行对**。
5. **每人保留一组时间戳，且只归约一次。** `_reduce_pairs`（`:272-287`）
   遍历全部有效行对，按 `user_id` 归并到 `best_by_user`：
   - 若该 `signup_ts` **早于**当前保留的注册时刻，整组替换
     （`signup_ts < current[0]`，`:283-284`）——效果是先锁定**时间最早的
     有效注册**；
   - 若 `signup_ts` 与当前保留值**相等**而 `visit_ts` 更晚，只更新访问
     时刻（`:285-286`）——效果是在能与该最早注册配对的 visit 中取
     **时间最晚的一次**；
   - 晚于最早注册的 signup 行对两个分支都不命中，被丢弃——**后发生的
     注册不会替代最早有效注册**。

   归约在 `:441-446` 只执行一次，得到唯一的 `best_by_user`；顶层数组
   （`:447-448`）与各日期组的组内数组都从这一份结果装配，二者天然勾稽。
6. **归组与转化集合。** `--group-by visit-date` 时（`:449-474`）：
   `_qualifying_visit_date_by_user`（`:253-269`）执行
   `SELECT user_id, substr(MIN(timestamp), 1, 10) FROM events
   <visit_where> GROUP BY user_id`——先应用访问时段（无时段即全库），
   再按 `user_id` 取 `MIN(timestamp)`，前 10 个字符即 UTC 日期
   `YYYY-MM-DD`；`GROUP BY` 保证每人恰好一行，**每个用户编号只归一个
   日期组**。转化集合 `converted_id_set`（`:462-468`）由
   `_converted_sql(args, "DISTINCT v.user_id")` 得出，与汇总
   `converted_users` 同源。
7. **组内配对装配。** `_build_visit_date_groups`（`:309-355`）是分组结果
   唯一的装配点：各组访问成员来自 `date_by_user`，转化成员是本组访问成员
   与转化集合的交集（`:338`）；`pairs_by_user` 非 `None`（即
   `--include-group-pairs` 开启，`:473` 传入 `best_by_user`）时，每组追加
   `conversion_pairs`（`:350-353`）——调用 `_pairs_payload(pairs_by_user,
   converted_ids)`，即**本组转化成员在同一份归约结果中的配对子集**，排序
   与顶层同一口径（`sorted`，`:305`：Python `str` 比较即 Unicode 码点
   字典序，区分大小写、保留空白与中文、不按数字大小）。
8. **输出装配。** 汇总三字段在前（`:482-486`），随后按开关追加
   `visit_user_ids`/`converted_user_ids`（`:487-489`）、顶层
   `conversion_pairs`（`:490-491`），最后追加 `visit_date_groups`
   （`:492-493`），由 `json.dumps` 单行打印（`:494`）。

**为什么配对访问的日期不会改变归组：** 归组（第 6 步）只回答"谁归哪天"，
输入是 `visit_where` 过滤后的 visit 行与 `MIN(timestamp)`；配对（第 4–5
步）只回答"每个转化用户保留哪组时间戳"，输入是自连接枚举的有效行对。两
条链路在源码中是**两次独立的查询**，唯一的交汇点是第 7 步：按"转化成员
的归组日期"把配对子集挂到对应日期组。配对里的 `visit_timestamp` 可以是
该用户任意一次合格 visit（第 3 节第 3 条），包括晚于归组日期的另一天；
装配时没有任何代码据配对时间重新归组——`date_by_user` 在配对归约之前已
由 `MIN` 定稿，且归约结果不回写归组映射。

## 3. 配对与归组语义（逐条可在源码核对）

1. **先时段、后归组、再配对过滤。** 时段条件同时拼在归组 SQL 的
   `<visit_where>`（`:253-269`）与转化 SQL 的 visit 一侧（`:207`）：段外
   visit 既不参与归组，也不进入任何有效行对。访问时段**含起点、不含终点**
   （`timestamp >= ? AND timestamp < ?`，`:182-186` 与 `:407-410`）。
2. **signup 严格晚于 visit。** 配对条件是 `s.timestamp > v.timestamp`
   （`:198`）；同一时刻不成立，逆序不成立。
3. **转化与配对可使用任意合格 visit，不限于归组那次。** 转化集合与配对
   行对都来自独立的自连接枚举，与 `MIN` 归组互不约束：只要该用户任意一次
   合格 visit 能与某次 signup 构成有效行对，他就在转化集合中，其配对也
   取自行对全集——归组日期由最早合格 visit 单独决定。
4. **注册允许晚于时段终点。** 时段片段只拼在 visit 一侧
   （`_visit_window_clause`，`:182-186`），signup 一侧唯一的时间条件是
   严格晚于 visit。交叉核对：
   `test_group_pairs_visit_window`（段内 visit、两天后 signup 仍算组内
   转化并出现在组内配对中）。
5. **转化窗口上界含等值。** 带 `--within-seconds N` 时追加秒差
   `... <= ?`（`:200-205`）：间隔恰为 N 秒计入；严格大于条件同时生效，
   间隔 0 秒仍排除。交叉核对：
   `test_group_pairs_within_seconds_boundary`（间隔恰 30 秒计入、29 秒
   窗口下各组配对为空）。
6. **每人先选最早有效注册、再选能匹配它的最晚访问。** 即 `_reduce_pairs`
   的两个分支（`:283-286`，见第 2 节第 5 步）。这是确定性规则：每个转化
   用户恰好一条配对，结果只取决于事件集合本身，与行序、重复事件无关。
   最早注册回答"该用户最早何时完成转化"，最晚可配对访问回答"转化前最后
   一次访问是哪次"；两者来自同一组有效行对，因此该访问必然严格早于该
   注册（且满足时限），配对本身必然合法。交叉核对：
   `test_group_pairs_earliest_signup_latest_visit`。
7. **组内数组长度等于本组转化人数。** 组内 `conversion_pairs` 由
   `_pairs_payload(pairs_by_user, converted_ids)` 装配（`:353`），对
   `converted_ids` 中每个编号产出恰好一条；`converted_users` 即
   `len(converted_ids)`（`:343`）。同一集合的两种展开，长度天然相等。
8. **各组配对合并排序后与顶层完全一致，且组间没有重复用户。** 每个转化
   用户只归一个日期组（`GROUP BY user_id`，`:253-269`），故各组
   `converted_ids` 两两不交、并集即全体转化用户；组内数组与顶层数组又
   出自同一份 `best_by_user`、同一个 `_pairs_payload` 排序口径（`:305`
   与 `:353`）。把各组配对合并后按 `user_id` 的 Unicode 码点升序排列，
   必与顶层 `conversion_pairs` 逐项相等，且没有任何用户出现在两个组中。
   交叉核对：`test_group_pairs_sorted_and_merge_matches_top_level`（含
   大小写、前导空白、中文与多位数字编号的多转化用户样例）与
   `assertGroupPairs` 辅助函数中的合并比对
   （`tests/test_report_include_group_pairs.py:135-165`）。
9. **无转化的组配对为空数组而非缺键。** 该组 `converted_ids` 为空，
   `_pairs_payload` 产出 `[]`；开关开启时每个组对象都含
   `conversion_pairs` 键（`:350-353`）。
10. **行序、重复事件、重复导入不改变结果。** 归组是 `GROUP BY user_id`
    上的 `MIN` 聚合，配对归约只比较时间戳的 min/max（`:283-286`），都与
    物理行序无关；重复事件行（含整批再次导入产生的相同行）产生相同行对，
    不改变 `MIN` 与 min/max。交叉核对：
    `test_shuffled_and_duplicate_events_give_same_group_pairs`、
    `test_reimport_same_batch_keeps_group_pairs`。
11. **未开启开关时不追加该字段，开关不自动开启其他明细。**
    `pairs_by_user` 只在 `args.include_group_pairs` 为真时传入
    （`:473`），否则组对象维持原有四个键；`--include-group-pairs` 不向
    顶层追加 `conversion_pairs`（顶层配对由 `--include-pairs` 单独控制，
    `:490-491`），也不向顶层或组内追加编号数组（由 `--include-users`
    单独控制，`:487-489` 与 `:345-349`）。交叉核对：
    `test_group_pairs_do_not_auto_enable_other_details`、
    `test_without_flag_groups_have_no_pair_field`、
    `test_metrics_identical_with_and_without_flag`。

## 4. 固定合成样例（2026 年 10 月，六条事件）

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

人物设定：

- **u1**：6 日 10:00:00 与 7 日 10:00:00 各 visit 一次，7 日 10:00:30
  signup（与 7 日 visit 间隔 30 秒）。
- **u2**：仅在 6 日 11:00:00 visit，无 signup。
- **u3**：7 日 12:00:00 同一时刻 visit 与 signup（严格大于不成立）。

## 5. 一次导入

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

**【实测】** 标准输出（一行，退出码 0，标准错误为空）：

```json
{"imported": 6}
```

六条记录全部通过 `parse_line`，`len(events)` 即 6（`:156`）。数据库文件
不存在时导入会新建它并建表（`:142-149`）；导入是事务内追加
（`:144-149`），对已有库重复执行会追加相同行而非替换。注意 `json.dumps`
默认分隔符在冒号后输出一个空格，逐字节文本即 `{"imported": 6}`；本文
其余 JSON 预期同理。

## 6. 验收报告：分组 + 组内配对 + 顶层配对 + 60 秒窗口

任务指定的验收命令同时启用 `--group-by visit-date`、`--include-group-pairs`
与 `--include-pairs`，并设置 `--within-seconds 60`：

```sh
python -m funnel report --db events.sqlite --group-by visit-date \
    --include-group-pairs --include-pairs --within-seconds 60
```

**【实测】** 完整标准输出（一行，退出码 0，标准错误为空）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-07T10:00:00", "signup_timestamp": "2026-10-07T10:00:30"}], "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5, "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-07T10:00:00", "signup_timestamp": "2026-10-07T10:00:30"}]}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0, "conversion_pairs": []}]}
```

交叉核对：`test_acceptance_group_pairs_with_include_pairs`。字段顺序即
`payload` 装配顺序（`:482-493`）：三个汇总字段、顶层 `conversion_pairs`、
`visit_date_groups`；组对象内四个基础字段在前、`conversion_pairs` 在最后
（`:339-353`）。

### 6.1 归组（无时段即全库，每人取最早 visit 的 UTC 日期）

归组查询（`:253-269`）对三人各产出一行：

| 用户 | 合格 visit | `MIN(timestamp)` | 归入日期 |
|---|---|---|---|
| u1 | 10-06 10:00:00、10-07 10:00:00 | `2026-10-06T10:00:00` | `2026-10-06` |
| u2 | 10-06 11:00:00 | `2026-10-06T11:00:00` | `2026-10-06` |
| u3 | 10-07 12:00:00 | `2026-10-07T12:00:00` | `2026-10-07` |

**u1 虽在 7 日也有 visit，仍只归 6 日组**（每人仅一组，`GROUP BY
user_id` + `MIN`，`:253-269`）。

### 6.2 汇总与转化的逐人来源

- **分母 `visit_users = 3`**：三人各至少有一次全库 visit，
  `COUNT(DISTINCT user_id)`（`:411-414`）。
- **u1：转化，计入 6 日组。** 有效行对枚举（`:189-208`）对 u1 产出两组
  候选：6 日 visit 与 signup 的间隔为 86430 秒，`<= 60` 不成立；**7 日
  10:00:00** visit 与 10:00:30 signup 满足 `10:00:30 > 10:00:00`
  （`:198`）且间隔 30 秒 `<= 60`（`:200-205`）。只剩一组有效行对，归约
  结果即 `visit_timestamp = 2026-10-07T10:00:00`、
  `signup_timestamp = 2026-10-07T10:00:30`。**配对用的是 7 日那次
  visit，但 u1 的归组日期仍是 6 日**——归组由最早合格 visit 的 `MIN`
  单独决定，配对时间戳不参与归组（第 2 节末段、第 3 节第 3 条），因此
  这条 7 日的配对挂在 6 日组内。交叉核对：
  `test_pair_visit_may_fall_on_other_date`。
- **u2：不转化。** 没有任何 signup 行，自连接枚举不到行对；计入 6 日组
  访问人数，不计转化。
- **u3：不转化。** visit 与 signup 同在 7 日 12:00:00，配对条件要求
  signup **严格**晚于 visit（`:198`），**相等时刻不成立**，枚举不到任何
  行对；间隔 0 秒也不可能满足任何时限（严格大于同时生效）。u3 计入
  7 日组访问人数，不计转化，7 日组配对因此为空数组。
- **分子 `converted_users = 1`**，**顶层比例 `1/3`** 真除法（`:481`）
  序列化即 `0.3333333333333333`。

### 6.3 两组数值、配对与勾稽

| visit_date | visit_users | converted_users | conversion_rate | conversion_pairs |
|---|---|---|---|---|
| `2026-10-06` | 2（u1、u2） | 1（u1） | `1/2 = 0.5` | `[u1 的 7 日配对]` |
| `2026-10-07` | 1（u3） | 0 | `0/1 = 0.0` | `[]` |

- **组内数组长度 = 本组转化人数**（第 3 节第 7 条）：6 日组 1 条配对对
  应 `converted_users = 1`；7 日组 0 条对应 `converted_users = 0`，且为
  空数组而非缺键（第 3 节第 9 条）。
- **合并勾稽**（第 3 节第 8 条）：两组的 `conversion_pairs` 合并后只有
  u1 一条，按 `user_id` 码点升序排列即顶层 `conversion_pairs` 本身，
  逐项相等；u1 只出现在 6 日组，组间没有重复用户。
- **人数勾稽**：`2 + 1 = 3 = visit_users`；`1 + 0 = 1 = converted_users`。
- 7 日组比例的数值是 0；因 `0 / 1` 走真除法分支（`:343`），JSON 文本为
  **`0.0`** 而非 `0`（整数 `0` 只在零访问的顶层 `else` 分支出现，
  `:481`）。

## 7. 开关独立性与未开启行为（实测对照）

**【源码推导】** 以下三种形态的差异全部由装配条件直接决定：
`--include-group-pairs` 只控制组内 `conversion_pairs`（`:473` 与
`:350-353`），顶层配对由 `--include-pairs`（`:490-491`）、编号数组由
`--include-users`（`:487-489` 与 `:345-349`）各自独立控制，互不自动
开启。

**【实测】** 同库执行
`python -m funnel report --db events.sqlite --group-by visit-date --include-group-pairs --within-seconds 60`
（不开 `--include-pairs`），顶层**没有** `conversion_pairs`，组内仍有：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5, "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-07T10:00:00", "signup_timestamp": "2026-10-07T10:00:30"}]}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0, "conversion_pairs": []}]}
```

**【实测】** 同库执行
`python -m funnel report --db events.sqlite --group-by visit-date --within-seconds 60`
（不开 `--include-group-pairs`），组对象维持原有四个键、不追加
`conversion_pairs`，其余字段逐项相同：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0}]}
```

交叉核对：`test_group_pairs_do_not_auto_enable_other_details`、
`test_without_flag_groups_have_no_pair_field`、
`test_metrics_identical_with_and_without_flag`。

## 8. 错误协议：单独使用 `--include-group-pairs`

**【源码推导】** 开关依赖检查（`:385-391`）在 `os.path.exists`
（`:393-395`）与 `sqlite3.connect`（`:398`）之前执行：
`args.include_group_pairs` 为真而 `args.group_by != "visit-date"`（含未
传 `--group-by` 的 `None`）时，打印错误行并返回退出码 2——不创建数据库
文件，已存在的库记录不变。交叉核对：`test_flag_without_group_by_rejected`、
`test_flag_without_group_by_does_not_create_db`、
`test_report_keeps_events_unchanged`。

**【实测】** 数据库路径不存在时：

```sh
python -m funnel report --db missing.sqlite --include-group-pairs
```

退出码 2、标准输出为空（0 字节），标准错误逐字为：

```text
--include-group-pairs 必须与 --group-by visit-date 合用：已提供 --include-group-pairs，缺少 --group-by visit-date
```

目标数据库文件**未被创建**（错误行同时指出该开关与 `--group-by
visit-date` 依赖；注意此处**不是**"数据库不存在"的路径错误——开关依赖
检查先于存在性检查）。

**【实测】** 数据库已存在且含记录时（同一条错误命令，另加
`--within-seconds 60`）：退出码 2、标准输出为空、标准错误同上；命令前后
对 `events` 表做全表快照比对，记录逐行一致——**未改动任何事件记录**。

## 9. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 4 节六条 JSONL 逐字节）、
  **确定输出**（第 5 节导入 JSON、第 6 节验收报告 JSON、第 7 节两种对照
  输出、第 8 节的退出码与标准错误形态）、**源码对应**（每条结论标注的
  `funnel/__main__.py` 行号与条件）。
- 逐人复核顺序建议：先按第 6.1 节的归组表确认每人的日期（u1 归 6 日，
  与其配对访问落在 7 日无关），再按第 6.2 节确认谁在转化集合中（u2 无
  signup、u3 同刻注册均不转化），然后用第 6.3 节的三组勾稽对账——组内
  数组长度等于本组转化人数、各组配对合并排序后等于顶层、组间无重复
  用户。
- 本文数值分两类标记：**【实测】** 为当前工作树源码在本机（Linux/WSL2，
  CPython 3.14.4）的实际执行观测；**【源码推导】** 为静态推导，并以
  `tests/test_report_include_group_pairs.py` 的具名测试交叉核对（该文件
  17 个用例本次全部通过；测试断言本身不作为实测证据）。复核时若实际
  观察与推导冲突，以实际观察为准并修正本文。
- 本次仅交付本说明。README、程序、命令入口、SQLite 表结构、追加导入
  协议、事件数据格式、访问时段筛选语义均保持现状；报告对数据库只执行
  `CREATE TABLE IF NOT EXISTS` 与 `SELECT`，不改写事件记录；未新增任何
  功能。
