# 按访问日期分组报告（--group-by visit-date）：源码行为核对说明

核对日期：2026-10-07
核对基线：当前工作树，HEAD `78356d5`，撰写前工作树干净。

## 1. 核对依据与范围

本说明只覆盖一条**已有**报告流程的分组部分：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> [--within-seconds N]
       [--visit-from YYYY-MM-DDTHH:MM:SS --visit-before YYYY-MM-DDTHH:MM:SS]
       [--include-users] [--include-pairs]
       [--group-by visit-date]
```

目的是让复核者用一份小型合成输入，从公开入口一路追到 `visit_date_groups`
数组，逐人核对"谁被归到哪一天、谁算该组转化、组内人数与汇总和两种明细之间
是什么关系"。结论全部指向当前仓库的真实文件、函数与条件；本说明不要求也不
描述任何功能变更，现有命令、输出字段、SQLite 结构、追加导入语义、报告不改写
事件记录的行为均保持现状。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、归组与输出 |
| `tests/test_group_by_visit_date.py` | 分组报告的 16 个回归测试（验收样例、归组语义、不变量、参数拒绝） |
| `docs/verification-visit-window.md` | 访问时段与转化时限的核对说明（姊妹篇） |
| `docs/verification-conversion-pairs.md` | 配对与编号明细的核对说明（姊妹篇） |
| `docs/verification-jsonl-funnel.md` | 导入与基础报告的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `parse_line` / `load_events` | 54–128 | 单行校验与整文件严格 UTF-8 校验 |
| `cmd_import` | 131–157 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 160–179 | `--within-seconds` 取值校验（正整数，上界钳制） |
| `_visit_window_clause` | 182–186 | 段内 visit 过滤片段 `>= ? AND < ?` |
| `_converted_sql` / `_converted_params` | 189–217 | 转化查询 SQL 与参数装配（分组与汇总共用） |
| `parse_group_by` | 220–230 | `--group-by` 取值校验（只接受 `visit-date`） |
| `parse_visit_bound` | 233–250 | `--visit-from` / `--visit-before` 取值校验 |
| `cmd_report` | 253–405 | `report` 子命令：参数检查、查询、归组、JSON 输出 |
| `cmd_report` 中 `--include-users` 块 | 302–319 | 编号明细：与汇总同一 SQL，Python 侧排序 |
| `cmd_report` 中 `--include-pairs` 块 | 320–344 | 配对明细：与汇总同一 SQL，Python 侧归约 |
| `cmd_report` 中 `--group-by` 块 | 345–384 | 归组查询、集合判定、按日期汇总与排序 |
| `main` | 408–468 | argparse 子命令与参数装配（`--group-by` 在 456–464） |

**数值来源声明（两种标记）：**

- 标 **【实测】** 的命令输出、退出码与标准错误文本，是 2026-10-07 在本机
  （Linux/WSL2，Python 3.14.4）以当前工作树源码实际执行的观测结果；`funnel`
  包经项目根目录解析，样例数据放在临时目录，与"在项目根目录执行下文命令"
  等价。
- 其余结论标 **【源码推导】**，依据为 `funnel/__main__.py` 当前源码（含其
  SQL 与 Python 表达式）及 CPython 标准库 `json`/`argparse`/`sqlite3` 的
  行为；凡 `tests/test_group_by_visit_date.py` 中已有断言覆盖的，同时注明
  测试名。该测试文件本次已**整体执行一遍，16 个用例全部通过**
  （`python3 -m unittest tests.test_group_by_visit_date`，OK）。
- 若实际观察与本文推导冲突，以实际观察为准并据此修正本文，而不是反过来。

## 2. 从公开入口到 `visit_date_groups`

以下每一步均可直接在源码中核对，串联起来就是分组报告的完整链路：

1. **公开入口与参数解析。** `python -m funnel report` 由 `main`
   （`:408-468`）装配；`--group-by` 带一个取值，合法值只有 `visit-date`
   （`type=parse_group_by`，`:456-464`）。参数解析在 `parse_args`
   （`:467`）完成，非法取值在此阶段即以退出码 2 拒绝，**先于
   `cmd_report` 运行，更先于任何数据库访问**（见第 8 节）。
2. **时段参数先检查、数据库后打开。** `cmd_report`（`:253`）先完成
   `--visit-from/--visit-before` 成对与先后检查（`:256-275`），再做
   `os.path.exists` 存在性检查（`:277-279`），最后才 `sqlite3.connect`
   （`:282`）。连接后只执行 `CREATE TABLE IF NOT EXISTS` 与 `SELECT`。
3. **先确定"合格访问"的筛选条件。** 访问条件在分组之前一次性构造
   （`:285-294`）：无时段时 `visit_where = "WHERE event = 'visit'"`，
   参数为空，即**全库访问**；有时段时为
   `WHERE event = 'visit' AND timestamp >= ? AND timestamp < ?`
   （含起点、不含终点）。同一个 `visit_where` / `visit_params` 同时用于
   汇总分母（`:295-298`）、`--include-users` 编号（`:306-312`）与下面的
   归组查询——三处口径完全相同。
4. **每人取最早合格 visit，归到它的 UTC 日期。** 归组查询（`:350-357`）：

   ```sql
   SELECT user_id, substr(MIN(timestamp), 1, 10)
   FROM events <visit_where>
   GROUP BY user_id
   ```

   先由 `<visit_where>` 应用访问时段（无时段即全库），再按 `user_id`
   分组取 `MIN(timestamp)`——**该用户最早一次合格 visit**；时间戳是不含
   时区后缀的定宽 ISO 文本，统一按 UTC 解释，前 10 个字符即 UTC 日期
   `YYYY-MM-DD`。`GROUP BY user_id` 保证每人恰好一行，写入 `date_by_user`
   字典（`:357`），因此**每个用户编号只属于一个日期组**。
5. **转化判定复用汇总查询。** `converted_id_set`（`:360-366`）由
   `_converted_sql(args, "DISTINCT v.user_id")` 得出，与汇总
   `converted_users`（`:299-301`）是**同一函数、同一筛选条件**。配对语义
   见第 3 节：signup 严格晚于 visit、可使用任意合格 visit、可晚于时段终点、
   时限上界含等值。
6. **按日期点人数并排序。** 遍历 `date_by_user`（`:367-372`）：每组访问
   人数 +1；该用户在转化集合中则该组转化人数 +1。最后按日期升序展开
   `visit_date_groups`（`:375-384`，键为 `sorted(visits_by_date)`；定宽
   日期文本的字典序即时间先后）。
7. **输出装配。** 汇总三字段在前（`:392-396`），随后按开关追加
   `visit_user_ids`/`converted_user_ids`（`:397-399`）、
   `conversion_pairs`（`:400-401`），最后在 `--group-by visit-date` 时
   追加 `visit_date_groups`（`:402-403`），由 `json.dumps` 单行打印
   （`:404`）。不传 `--group-by` 时 `args.group_by is None`，该数组
   **不出现在输出中**。

## 3. 分组与转化语义（逐条可在源码核对）

1. **先时段、后归组。** 时段条件直接拼在归组 SQL 的 `<visit_where>` 里
   （`:351-356`），先过滤、后 `MIN`；段外 visit 既不参与归组，也不可能成为
   "最早合格 visit"。没有时段限制时使用**全库访问**（`:285-287`）。
2. **每人仅一组，归最早合格 visit 的 UTC 日期。** `GROUP BY user_id` +
   `MIN(timestamp)`（`:352-354`）决定一人一行；同一用户在其他日期的访问
   不再产生第二个组。回归：`test_user_assigned_to_earliest_qualifying_visit_only`
   （6、7、8 日各访问一次的用户只归 6 日组）。
3. **转化可使用任意合格访问，不限于归组那次。** 转化集合来自独立的
   `_converted_sql` 自连接枚举（`:360-366`、`:189-208`），与 `MIN` 归组
   互不约束：只要该用户**任意一次**合格 visit 能与某次 signup 构成有效配对，
   他就在转化集合中，所在日期组（按最早 visit 归的那组）转化人数 +1。
4. **signup 严格晚于 visit。** 配对条件是 `s.timestamp > v.timestamp`
   （`:198`）；同一时刻不成立，逆序不成立。
5. **时限上界包含等值。** 带 `--within-seconds N` 时追加秒差
   `... <= ?`（`:200-205`）：间隔恰为 N 秒计入；严格大于条件同时生效，
   间隔 0 秒仍排除。分组下的回归：
   `test_within_seconds_upper_bound_inclusive_in_groups`（间隔恰 60 秒的
   用户在组内转化，61 秒的不转化）。
6. **注册可以晚于时段终点。** 时段片段只拼在 visit 一侧
   （`_visit_window_clause`，`:182-186`；`_converted_sql` 在 `:207`
   引用），signup 一侧唯一的时间条件是严格晚于 visit。回归：
   `test_signup_after_window_end_still_counts_in_group`（段内 visit、
   两天后 signup 仍算组内转化）。
7. **日期组升序、只出现有访问用户的日期。** 日期键全部来自
   `date_by_user`（即至少有一次合格 visit 的用户），没有任何合格 visit 的
   日期不会出现；输出顺序为 `sorted(visits_by_date)`（`:383`）。
8. **两种人数的分组总和分别等于汇总。** 归组把分母用户集合（与
   `visit_users` 同条件的 `DISTINCT user_id`）按日期**划分**，故
   `Σ visit_users = visit_users`；转化用户集合（与 `converted_users`
   同源）按其归组日期计数，故 `Σ converted_users = converted_users`。
   该等式同时被 `assertGroups` 断言（测试文件 `:131-140`）。
9. **组内比例按组内人数计算。**
   `conversion_rate = converted_by_date.get(day, 0) / visits_by_date[day]`
   （`:380-381`）真除法。出现的日期必有访问，分母不为 0；该组无转化时
   数值为 0，JSON 文本是 `0.0`（`0 / 正整数` 为浮点）。
10. **无合格访问时数组为空。** 没有任何合格 visit 时 `date_by_user` 与
    `visits_by_date` 均为空，列表推导结果为 `[]`（`:375-384`），汇总为
    `visit_users = 0` 且顶层比例为整数 `0`（`:391`）。回归：
    `test_no_qualifying_visits_gives_empty_groups`（只有 signup 的用户）、
    `test_window_without_visits_gives_empty_groups`（时段内无人访问）。

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
  signup（间隔 30 秒）。
- **u2**：仅在 6 日 11:00:00 visit，无 signup。
- **u3**：7 日 12:00:00 同一时刻 visit 与 signup（严格大于不成立）。

## 5. 一次导入

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

**【实测】** 预期标准输出（一行，退出码 0）：

```json
{"imported": 6}
```

六条记录全部通过 `parse_line`，`len(events)` 即 6（`:156`）。数据库文件
不存在时导入会新建它并建表（`:142-149`）。注意 `json.dumps` 默认分隔符
在冒号后输出一个空格，逐字节文本即 `{"imported": 6}`；本文其余 JSON
预期同理。

## 6. 全库分组报告（`--within-seconds 60`）

```sh
python -m funnel report --db events.sqlite --group-by visit-date --within-seconds 60
```

**【实测】** 完整预期标准输出（一行，退出码 0，标准错误为空）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0}]}
```

对应测试：`test_acceptance_full_db_within_60`。

### 6.1 归组（先时段——此处无时段即全库，再取每人最早 visit）

归组查询（`:350-357`）对三人各产出一行：

| 用户 | 合格 visit | `MIN(timestamp)` | 归入日期 |
|---|---|---|---|
| u1 | 10-06 10:00:00、10-07 10:00:00 | `2026-10-06T10:00:00` | `2026-10-06` |
| u2 | 10-06 11:00:00 | `2026-10-06T11:00:00` | `2026-10-06` |
| u3 | 10-07 12:00:00 | `2026-10-07T12:00:00` | `2026-10-07` |

注意 **u1 虽在 7 日也有 visit，仍只归 6 日组**（每人仅一组，第 3 节
第 2 条）。

### 6.2 汇总与转化的逐人来源

- **分母 `visit_users = 3`**：三人各至少有一次全库 visit，
  `COUNT(DISTINCT user_id)`（`:295-298`）。
- **u1：转化，计入 6 日组。** 枚举两对 `(visit, signup)`：与 6 日
  10:00:00 的间隔为 86430 秒，`<= 60` 不成立；与 **7 日 10:00:00** 的间隔
  为 30 秒，`10:00:30 > 10:00:00` 成立且 `30 <= 60` 成立。转化用的是
  7 日那次 visit，**不是归组用的 6 日那次**（第 3 节第 3 条）。u1 在
  `converted_id_set` 中，其归组日期 6 日的转化人数 +1。
- **u2：不转化。** 没有任何 signup 行，自连接枚举不到行对；计入 6 日组
  访问人数，不计转化。
- **u3：不转化。** visit 与 signup 同为 12:00:00，严格大于
  （`:198`）不成立，间隔 0 秒也不可能满足时限；计入 7 日组访问人数，
  不计转化。
- **分子 `converted_users = 1`**，**顶层比例 `1/3`** 真除法序列化即
  `0.3333333333333333`（`:391`）。

### 6.3 两组数值与勾稽

| visit_date | visit_users | converted_users | conversion_rate |
|---|---|---|---|
| `2026-10-06` | 2（u1、u2） | 1（u1） | `1/2 = 0.5` |
| `2026-10-07` | 1（u3） | 0 | `0/1 = 0.0` |

- 数组按日期升序；只出现有访问用户的两个日期（第 3 节第 7 条）。
- 人数勾稽：`2 + 1 = 3 = visit_users`；`1 + 0 = 1 = converted_users`。
- 7 日组比例的数值是 0；因 `0 / 1` 走真除法分支，JSON 文本为 **`0.0`**
  而非 `0`（整数 `0` 只在零访问的顶层 `else` 分支出现，`:391`）。

## 7. 只保留 7 日 UTC 全天访问（时段报告）

```sh
python -m funnel report --db events.sqlite --group-by visit-date \
    --within-seconds 60 \
    --visit-from 2026-10-07T00:00:00 --visit-before 2026-10-08T00:00:00
```

区间 `[2026-10-07T00:00:00, 2026-10-08T00:00:00)` 即 7 日 UTC 全天
（含起点、不含终点）。**【实测】** 完整预期标准输出（一行，退出码 0）：

```json
{"visit_users": 2, "converted_users": 1, "conversion_rate": 0.5, "visit_date_groups": [{"visit_date": "2026-10-07", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5}]}
```

对应测试：`test_acceptance_window_only_7th`。

- **段外历史不参与归组。** 时段先于 `MIN` 应用（第 3 节第 1 条）：
  u1 的 6 日 visit 在段外，被 `visit_where` 过滤后，其最早**合格**
  visit 变成 7 日 10:00:00，u1 改归 **7 日组**；u2 唯一 visit 在 6 日，
  段内无任何 visit，既不进分母也不归任何组。
- **汇总 2、1、0.5。** 段内访问用户为 u1、u3；转化仍只有 u1（30 秒
  配对未受影响；visit 与 signup 都在段内，本例 signup 未越过终点，但
  越过终点同样有效——第 3 节第 6 条）。
- **唯一的 7 日组同样是 2、1、0.5**，组人数与汇总逐项相等（只有一组时
  勾稽必然成立）。日期为 6 日的组整组消失：它没有任何段内访问用户。

## 8. 分组与用户编号、配对明细的关系

`--group-by` 与 `--include-users`、`--include-pairs` 可叠加；三者共用同
一组筛选条件，分组只在顶层**追加**数组（`:397-403`）。

```sh
python -m funnel report --db events.sqlite --group-by visit-date \
    --within-seconds 60 --include-users --include-pairs
```

**【实测】** 完整预期标准输出（字段顺序即 `payload` 装配顺序）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_user_ids": ["u1", "u2", "u3"], "converted_user_ids": ["u1"], "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-07T10:00:00", "signup_timestamp": "2026-10-07T10:00:30"}], "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0}]}
```

对应测试：`test_groups_coexist_with_include_users_and_pairs`。

- **编号集合 = 被分组用户集合。** `visit_user_ids` 与归组查询使用同一
  `visit_where`（`:306-312` 对 `:350-357`），故
  `["u1", "u2", "u3"]` 恰好是两个日期组所划分用户的并集；
  `converted_user_ids`（`:313-319`）与分组用的 `converted_id_set`
  同源，均为 `["u1"]`。
- **配对明细不要求是归组那次 visit。** u1 的配对是
  `2026-10-07T10:00:00` visit + `10:00:30` signup，而 u1 归在 6 日组：
  归组看"最早合格 visit"，配对看"最早有效 signup 与最晚可配对 visit"
  （归约规则见 `docs/verification-conversion-pairs.md` 与 `:320-344`），
  两条规则独立。每个转化用户恰好一条配对，配对的 `user_id` 集合等于
  `converted_user_ids`，也等于各日期组转化人数所对应的用户。
- **顶层明细与事件记录均不变。** 加 `--group-by` 后三个汇总字段、两个
  编号数组、`conversion_pairs` 的值与不加时一致，仅末尾多出
  `visit_date_groups`；报告对数据库只做 `CREATE TABLE IF NOT EXISTS` 与
  `SELECT`。回归：`test_grouped_report_keeps_events_unchanged`（连续
  分组报告后事件快照不变）。

## 9. 不变量：行序、重复、重复导入、未启用

以下各条由源码结构保证，并有回归测试佐证（均在本次 16 个通过的用例中）：

1. **重复事件、重复导入、行序不改变人数。** 归组是
   `GROUP BY user_id` 上的 `MIN` 聚合与集合成员判定
   （`:350-372`），与物理行序无关；重复事件行（含整批再次导入产生的
   相同行）不改变 `MIN`，也不改变 `DISTINCT` 转化集合。回归：
   `test_shuffled_and_duplicate_events_give_same_groups`（固定打乱行序、
   混入两条重复事件，组数值不变）、
   `test_reimport_same_batch_keeps_groups`（同一文件连导两次，组数值
   不变）。
2. **未启用分组时不输出该数组。** 不传 `--group-by` 时
   `args.group_by is None`，`:402-403` 不执行，输出只有三个汇总字段。
   回归：`test_no_group_by_leaves_output_unchanged`。**【实测】** 同库
   执行 `python -m funnel report --db events.sqlite --within-seconds 60`
   的完整输出为：

   ```json
   {"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
   ```

3. **启用分组后顶层明细及事件记录不变。** 见第 8 节；分组纯粹是追加。
4. **无合格访问时数组为空。** 见第 3 节第 10 条及两个空数组测试。

## 10. 参数与路径错误：退出码 2、标准输出为空

### 10.1 非法或缺失的 `--group-by`

`parse_group_by`（`:220-230`）只接受字面量 `visit-date`，其余取值一律
抛出 `argparse.ArgumentTypeError`：

> 只接受 visit-date（按最早合格 visit 的 UTC 日期分组），得到 '<值>'

它是 argparse 的 `type` 回调，在 `parse_args`（`:467`）阶段触发，因此
**先于 `cmd_report`、先于 `os.path.exists`、先于一切数据库访问**：
退出码 2、标准输出为空、标准错误指出参数名 `--group-by` 与原因，且
**不创建数据库文件**。测试覆盖的取值：`""`、`"signup-date"`、
`"VISIT-DATE"`、`"visit_date"`、`" visit-date"`
（`test_invalid_group_by_rejected_before_db_access`、
`test_invalid_group_by_does_not_create_db`）；命令行缺取值（`--group-by`
后无参数）由 argparse 直接拒绝（`test_group_by_missing_value_rejected`）。

**【实测】** 以空值为例（`--group-by ""`，库已存在），退出码 2、标准
输出为空，标准错误为：

```text
usage: python -m funnel report [-h] --db DB [--within-seconds N]
                               [--visit-from YYYY-MM-DDTHH:MM:SS]
                               [--visit-before YYYY-MM-DDTHH:MM:SS]
                               [--include-users] [--include-pairs]
                               [--group-by visit-date]
python -m funnel report: error: argument --group-by: 只接受 visit-date（按最早合格 visit 的 UTC 日期分组），得到 ''
```

**【实测】** 库路径不存在且取值非法（`--group-by signup-date`）时，错误
仍是上面的参数错误（参数校验先发生），退出码 2，目标数据库文件**未被
创建**。缺取值时 **【实测】** 错误行为
`argument --group-by: expected one argument`，同样退出码 2、标准输出
为空。用法行的具体排版随 CPython 版本略有差异；错误行同时包含参数名与
拒绝原因这一形态由源码与回归测试固定。

### 10.2 参数合法但数据库不存在

`--group-by visit-date` 等参数全部合法、仅 `--db` 路径不存在时，通过
全部参数检查后由 `os.path.exists` 分支处理（`:277-279`）：退出码 2、
标准输出为空，标准错误单行指出**路径与不存在原因**，且因为判断在
`sqlite3.connect` 之前，**不会顺手创建该库**。回归：
`test_missing_db_follows_path_error_protocol`。

**【实测】**

```sh
python -m funnel report --db /tmp/fverify/missing.sqlite --group-by visit-date
# 退出码 2；stderr 逐字为：
# /tmp/fverify/missing.sqlite: 数据库不存在
```

## 11. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 4 节六条 JSONL 逐字节）、
  **确定输出**（第 5–8 节的导入与报告 JSON、第 10 节的退出码与标准错误
  形态）、**源码对应**（每条结论标注的 `funnel/__main__.py` 行号与
  条件）。
- 逐人复核顺序建议：先按第 6.1 节的归组表确认每人的日期，再按第 6.2 节
  确认谁在转化集合中，最后用第 6.3 节的勾稽（两种人数组和分别等于汇总）
  对账；需要追事件级证据时叠加第 8 节的两个明细开关。
- 本文数值分两类标记：**【实测】** 为本机 Python 3.14.4 的实际执行
  观测；**【源码推导】** 为静态推导并以
  `tests/test_group_by_visit_date.py` 的具名测试交叉核对（该文件 16 个
  用例本次全部通过）。复核时若实际观察与推导冲突，以实际观察为准并修正
  本文。
- 本次仅交付本说明。程序、README、SQLite 表结构、既有输入输出与其他
  三份核对说明均保持现状，未新增任何功能。
