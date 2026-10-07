# 整体转化耗时（--include-latency）：源码行为核对说明

核对日期：2026-10-08
核对基线：当前工作树，HEAD `241689b`，工作树干净。

## 1. 核对依据与范围

本说明只覆盖一条**已有**报告流程的整体转化耗时部分：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> --include-latency
       [--within-seconds N] [--include-pairs]
```

目的是让复核者用一份小型合成输入，逐人核对"哪些事件构成有效配对、每个转化
用户贡献哪一个耗时、最终 `conversion_latency` 的三个数值长什么样"。结论全部
指向当前仓库的真实文件、函数与条件；本说明不要求也不描述任何功能变更，现有
命令、输出字段、数据格式、追加导入、访问时段筛选语义、报告不改写事件记录的
行为均保持现状。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、配对归约、耗时装配、输出 |
| `tests/test_report_include_latency.py` | 耗时开关、逐人耗时口径、时限下最早有效注册切换、空结果、分组合用的回归测试 |
| `tests/test_within_seconds_validation.py` | `--within-seconds` 取值校验的回归测试 |
| `docs/verification-conversion-pairs.md` | 配对明细开关的核对说明（姊妹篇，配对归约部分完全共用） |
| `docs/verification-jsonl-funnel.md` | 导入与基础报告的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `TIMESTAMP_RE` / `VALID_EVENTS` | 22 / 27 | 导入侧时间格式与合法事件 |
| `WITHIN_SECONDS_RE` | 30 | `--within-seconds` 严格形态（`\A[0-9]+\Z` 锚定） |
| `parse_line` / `load_events` | 55–129 | 单行校验与整文件严格 UTF-8 校验 |
| `cmd_import` | 132–158 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 161–180 | `--within-seconds` 取值校验（大于零） |
| `_converted_sql` / `_converted_params` | 190–218 | 有效配对枚举 SQL 与参数装配 |
| `_reduce_pairs` | 273–288 | 配对归约：每人最早有效 signup 配最晚可配对 visit |
| `_latency_payload` | 310–337 | 由归约结果装配 `conversion_latency` 对象 |
| `cmd_report` | 389–532 | `report` 子命令：参数检查、查询、归约、JSON 输出 |
| `cmd_report` 中耗时块 | 467–482、526–528 | 触发归约、装配并仅在顶层追加 `conversion_latency` |
| `main` 中 `--include-latency` | 584–592 | 开关定义（不带取值，`store_true`） |

**数值来源声明（区分推导与实测）：** 第 3–5 节与第 7 节列出的每一条命令，
本次核对都在本机以 `python3 -m funnel`（与 `python -m funnel` 同一模块入口）
**实际执行过**，文中给出的退出码、标准输出/标准错误文本与执行观察逐字节一致；
这些数值同时也都能从源码静态推出。第 2、4、5、6 节中关于"为什么是这个结果"
的机制性解释（哪一行条件排除了哪组配对、归约分支如何命中）是对
`funnel/__main__.py` 当前源码的**静态推导**，文中以"源码推导"字样标出。复核时
若实际观察与本文任何一处不符，以实际观察为准并据此修正本文，而不是反过来。

## 2. 耗时统计流程：从公开入口到最终 JSON

以下每一步均可直接在源码中核对，串联起来就是 `--include-latency` 的完整链路：

1. **公开入口。** `python -m funnel report` 由 `main`（`:535-615`）装配，
   `--include-latency` 是不带取值的开关（`action="store_true"`，`:584-592`），
   可独立使用，不依赖 `--include-pairs`，也可与 `--include-users`、
   `--include-pairs`、`--within-seconds`、`--visit-from/--visit-before`、
   `--group-by visit-date` 叠加。参数解析在 `parse_args`（`:614`）完成，
   `--within-seconds` 的非法取值在此阶段即以退出码 2 拒绝（见第 7 节）。
2. **有效事件配对（SQL 枚举）。** `cmd_report` 在 `--include-pairs`、
   `--include-group-pairs` 或 `--include-latency` 任一开启时，调用
   `_converted_sql(args, "v.user_id, v.timestamp, s.timestamp")`（`:473-478`）
   ——与汇总转化人数使用**同一函数、同一筛选条件**，仅 `select` 列表不同。
   该 SQL（`:190-209`）把 `events` 自连接：visit 一侧 `v.event = 'visit'`，
   signup 一侧 `s.event = 'signup'` 且 **`s.timestamp > v.timestamp`（严格
   大于，`:199`）**；提供 `--within-seconds N` 时追加秒差 `<= N`
   （`:201-206`，上界含等值）；提供访问时段时只对 visit 一侧加
   `>= 起点 AND < 终点`（`_visit_window_clause`，`:183-187`）。连接结果即
   当前筛选条件下的全部**有效 (visit, signup) 行对**——耗时只从这些行对
   里产生。
3. **每人保留一组配对（Python 归约，与配对明细完全共用）。** `_reduce_pairs`
   （`:273-288`）遍历全部有效行对，按 `user_id` 归并到 `best_by_user`：
   - 若该 `signup_ts` **早于**当前保留的注册时刻，整组替换
     （`signup_ts < current[0]`，`:284-285`）——效果是先锁定**时间最早的
     有效注册**；
   - 若 `signup_ts` 与当前保留值**相等**而 `visit_ts` 更晚，只更新访问时刻
     （`:286-287`）——效果是在能与该最早注册配对的 visit 中取**时间最晚的
     一次**；
   - 晚于最早有效注册的 signup 行对两个分支都不命中，被丢弃。
4. **由同一份归约结果装配耗时。** `_latency_payload`（`:310-337`）对
   `best_by_user` 中每个用户的那一组配对，用 `strptime` 求 signup 与 visit
   的 UTC 时间差秒数（`:322-329`）——**每个转化用户恰好贡献一个耗时，取的
   是归约保留的那组配对，不是该用户所有配对中的最短间隔**。归约结果中
   visit 严格早于 signup，耗时恒为正整数秒。`min_seconds`、`max_seconds`
   取各用户耗时的最小、最大并转为整数（`:334-335`）；`mean_seconds` 为全部
   耗时之和除以转化人数的算术平均，**真除不取整**（`:336`，每个转化用户
   等权，与访问次数无关）。无转化（含无访问）时三项均为 `None`，JSON 中即
   `null`（`:330-331`）。
5. **最终 JSON。** `conversion_latency` 只在**顶层**追加（`:526-528`，注释
   明确"组内不追加任何耗时字段"）；`--group-by visit-date` 的组对象由
   `_build_visit_date_groups`（`:340-386`）装配，该函数根本不接收耗时数据，
   因此分组开启时组内不会出现耗时字段。汇总字段在前、`conversion_latency`
   在配对明细之后的字段顺序即 `payload` 的装配顺序（`:516-528`），由
   `json.dumps` 单行打印（`:531`）。

**为什么"最早有效注册"会随时限变化：** "有效"是相对当前筛选条件而言的。
配对枚举 SQL 把 `--within-seconds` 直接追加在连接条件里（`:201-206`），某次
注册若在当前时限下没有任何合格 visit 能与它配成有效行对，它就不会出现在枚举
结果中，归约自然轮不到它——**最早有效注册可能因此让位给更晚的一次注册**。
第 5 节的 29 秒样例专门演示这一点。

## 3. 固定合成样例（2026-10-06，十条事件）

以下十条完整 UTF-8 JSONL 记录（字段沿用现有格式：`user_id` / `event` /
`timestamp`，与 `parse_line` 的校验一一对应），保存为 `acceptance.jsonl`：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:30"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:01:59"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:02:00"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:01:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
```

人物设定（均为 2026-10-06 的 UTC 时间）：u1 在 10:00:00、10:00:30、10:01:59
各访问一次，在 10:01:00、10:02:00 各注册一次；u2 在 10:00:00 访问、10:01:00
注册；u3 的访问与注册同在 10:00:00；u4 仅在 10:00:00 访问。

导入新库（数据库文件不存在时导入会新建它并建表，`funnel/__main__.py:142-150`）：

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

预期标准输出（一行，退出码 0；本次核对已实际执行，与源码推导一致）：

```json
{"imported": 10}
```

十条记录全部通过 `parse_line`，`len(events)` 即 10（`:157`）。注意 `json.dumps`
默认分隔符会在冒号后输出一个空格，逐字节文本是 `{"imported": 10}`；本文其余
JSON 预期同理，按 JSON 值比较时与紧凑写法等价。

## 4. 无转化时限的整体耗时报告

```sh
python -m funnel report --db events.sqlite --include-latency
```

预期标准输出（一行，退出码 0；本次核对已实际执行，与源码推导一致）：

```json
{"visit_users": 4, "converted_users": 2, "conversion_rate": 0.5, "conversion_latency": {"min_seconds": 30, "max_seconds": 60, "mean_seconds": 45.0}}
```

**分母 `visit_users = 4`**（`:442-445`，无时段时全库 `event = 'visit'` 按
`user_id` 去重）：u1、u2、u3、u4 各计一人。

**分子 `converted_users = 2` 与逐人耗时来源**（有效行对枚举 `:190-209`，归约
`:273-288`，耗时 `:322-329`；以下为源码推导）：

- **u1：转化，贡献 30 秒。** 有效行对要求 signup 严格晚于 visit（`:199`）。
  u1 的三次访问与两次注册交叉出五组有效行对：以 10:01:00 为 signup 的
  (10:00:00, 10:01:00) 间隔 60 秒、(10:00:30, 10:01:00) 间隔 30 秒；以
  10:02:00 为 signup 的 (10:00:00, 10:02:00) 间隔 120 秒、(10:00:30, 10:02:00)
  间隔 90 秒、(10:01:59, 10:02:00) 间隔 1 秒。归约先锁定**最早的 signup
  10:01:00**（`:284-285`）；以 10:02:00 为 signup 的三组行对（含间隔仅 1 秒
  的那组）落入"更晚的 signup"情形，两个分支都不命中——**后一次注册不会替代
  最早有效注册**。signup 固定为 10:01:00 后，能与其配对的 visit 有 10:00:00
  与 10:00:30 两次，取**最晚的 10:00:30**（`:286-287`）。因此 u1 贡献
  10:01:00 − 10:00:30 = **30 秒**，而不是后续事件（10:01:59 访问与 10:02:00
  注册）之间更吸引眼球的 1 秒——耗时口径是"最早有效注册配对能匹配它的最晚
  访问"这一固定顺序，不是全配对最短间隔。
- **u2：转化，贡献 60 秒。** 唯一 visit 10:00:00 与唯一 signup 10:01:00 构成
  唯一有效行对，耗时 60 秒。
- **u3：不转化。** visit 与 signup 同在 10:00:00，配对条件是严格大于
  （`:199`），**相等时刻不成立**，枚举不到任何行对。
- **u4：不转化。** 只有 visit 没有 signup，同样枚举不到行对。

**三项耗时**：两个转化用户各贡献一个耗时 30、60。`min_seconds = 30`、
`max_seconds = 60`（整数，`:334-335`）；`mean_seconds = (30 + 60) / 2 = 45.0`
（真除，`:336`）——每个转化用户等权，与各自的访问、注册次数无关。`45.0` 是
浮点真除的结果经 `json.dumps` 序列化的文本，不是格式化舍入。

**比例 `0.5`**：`conversion_rate = converted_users / visit_users` 真除法
（`:515`），`2 / 4` 即 `0.5`。

**与配对明细互相印证（可选核对手段）**：加开 `--include-pairs` 时，配对数组
与耗时来自同一份归约结果（`:473-482`），逐人时间差必然与耗时一致。本次核对
实际执行 `python -m funnel report --db events.sqlite --include-latency
--include-pairs`，输出中 `conversion_pairs` 为 u1（visit 10:00:30、signup
10:01:00，差 30 秒）与 u2（visit 10:00:00、signup 10:01:00，差 60 秒），
`conversion_latency` 仍为 `{"min_seconds": 30, "max_seconds": 60,
"mean_seconds": 45.0}`，逐人差值与三项统计完全勾稽（回归测试
`test_latency_consistent_with_pairs_payload` 守护同一性质）。

## 5. 加 29 秒转化时限：最早注册失去合格配对

```sh
python -m funnel report --db events.sqlite --include-latency --within-seconds 29
```

预期标准输出（一行，退出码 0；本次核对已实际执行，与源码推导一致）：

```json
{"visit_users": 4, "converted_users": 1, "conversion_rate": 0.25, "conversion_latency": {"min_seconds": 1, "max_seconds": 1, "mean_seconds": 1.0}}
```

`--within-seconds 29` 只追加在转化查询的连接条件里（`:201-206`）：间隔
**小于或等于** 29 秒的有效行对保留，严格大于条件同时生效。分母查询不引用它，
访问人数仍是 4。逐人来源（源码推导）：

- **u1：仍转化，但贡献的耗时从 30 秒变为 1 秒。** 原先最早的注册 10:01:00
  与两次更早访问的间隔分别为 60、30 秒，`60 <= 29` 与 `30 <= 29` 都不成立——
  **该注册在当前时限下没有任何合格配对，不再是有效注册**，它的两组行对在
  SQL 枚举阶段即被排除。以 10:02:00 为 signup 的三组行对中，120、90 秒的
  两组同样被排除，只剩 (10:01:59, 10:02:00) 间隔恰 1 秒，`1 <= 29` 成立。
  于是**最早有效注册让位给 10:02:00**，能与其配对的最晚（也是唯一）visit 为
  10:01:59，u1 贡献 1 秒。这正是第 2 节所述"最早有效注册随时限变化"的实例：
  时限收紧后，耗时不是"在原来的 30 秒上判断是否合格"，而是重新枚举、重新
  归约后的结果。
- **u2：不再转化。** 唯一行对间隔 60 秒，`60 <= 29` 不成立。
- **u3、u4：本不转化，不受影响。**

只剩 u1 一人转化：`converted_users = 1`，`conversion_rate = 1 / 4 = 0.25`；
单人耗时 1 秒，故 `min_seconds = 1`、`max_seconds = 1`、`mean_seconds = 1.0`
（`1 / 1` 真除得浮点 `1.0`）。本次核对实际执行加开 `--include-pairs` 的同一
报告，`conversion_pairs` 为 u1（visit 10:01:59、signup 10:02:00），差值 1 秒
与三项耗时一致。

## 6. 耗时开关的不变量

以下各条由源码结构直接保证（源码推导），并有回归测试佐证：

1. **开关独立且不改变汇总。** `--include-latency` 只向 `payload` 追加
   `conversion_latency`（`:526-528`）；三个汇总字段的查询与该开关无关。不传
   该开关时输出保持只有汇总字段（`test_without_flag_outputs_only_metrics`）；
   单独传该开关时不出现 `conversion_pairs`
   （`test_latency_does_not_require_pairs`）。
2. **耗时与配对明细同源。** 耗时与 `conversion_pairs` 从同一次
   `_reduce_pairs` 结果装配（`:473-482`），每个转化用户恰好一个键、恰好贡献
   一个耗时；两开关同开时逐人时间差必然等于各人耗时
   （`test_latency_consistent_with_pairs_payload`）。
3. **字段类型与空结果口径。** `min_seconds`、`max_seconds` 为整数秒；
   `mean_seconds` 为真除浮点（如 45.5 不取整，
   `test_min_max_are_ints_mean_is_unrounded_float`）；无转化（含无访问、
   有访问无人转化）时三项均为 `null`（`:330-331`；
   `test_visit_only_gives_null_latency`、`test_signup_only_gives_null_latency`）。
4. **分组开启时仅顶层追加耗时。** `conversion_latency` 的追加点在顶层
   `payload`（`:526-528`）；组对象由 `_build_visit_date_groups` 装配，不接收
   耗时数据，组内不出现任何耗时字段（`test_latency_not_added_inside_groups`；
   本次核对实际执行 `--include-latency --group-by visit-date
   --include-group-pairs`，顶层有耗时对象、组对象内无耗时字段）。
5. **行序、重复事件与重复导入不改变结果。** 归约只比较时间戳的 min/max
   （`:284-287`），与行对产出顺序无关；重复事件（含重复导入产生的相同行）
   产生相同行对，min/max 不变（`test_shuffled_and_duplicate_events_give_same_latency`、
   `test_reimport_same_batch_keeps_latency`）。
6. **报告只读。** `cmd_report` 对数据库只执行 `CREATE TABLE IF NOT EXISTS`
   与 `SELECT`，正常报告不改写任何事件记录
   （`test_report_keeps_events_unchanged`）。

## 7. 错误示例：退出码 2、标准输出为空

### 7.1 时限为 0：访问数据库之前拒绝

```sh
python -m funnel report --db events.sqlite --include-latency --within-seconds 0
```

预期行为（本次核对已实际执行，与源码推导一致）：**退出码 2、标准输出为空**，
标准错误为 argparse 的用法行加错误行，错误行同时指出参数名与大于零的要求：

```text
python -m funnel report: error: argument --within-seconds: 必须大于零，得到 '0'
```

`parse_within_seconds`（`:161-180`）先以 `WITHIN_SECONDS_RE`（`\A[0-9]+\Z`，
`:30`）要求完整参数值全为 ASCII 数字，再单独拒绝全零取值（`:172-174`，原因
"必须大于零"）。该校验是 argparse 的 `type` 回调，在 `parse_args`（`:614`）
阶段触发——**先于 `cmd_report` 运行，更先于任何数据库访问**：不创建数据库
文件，已存在的库记录不变（本次核对对一个不存在的库路径执行该命令，执行后
该路径仍不存在；`tests/test_report_include_latency.py` 的
`test_invalid_within_seconds_still_rejected`、
`tests/test_within_seconds_validation.py` 守护同一性质）。

### 7.2 报告数据库不存在：指出路径且不创建文件

```sh
python -m funnel report --db /path/to/missing.sqlite --include-latency
```

预期行为（本次核对已实际执行，与源码推导一致）：**退出码 2、标准输出为空**，
标准错误为：

```text
/path/to/missing.sqlite: 数据库不存在
```

包含数据库路径与不存在原因。`os.path.exists` 检查（`:424-426`）发生在
`sqlite3.connect` 之前，因此**不会顺手创建该库**（本次核对执行后该路径仍
不存在；`test_missing_db_still_rejected` 守护同一性质）。

## 8. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 3 节十条 JSONL 逐字节）、
  **确定输出**（第 3–5 节的导入与报告 JSON、第 7 节的退出码与标准错误
  形态）、**源码对应**（每条结论标注的 `funnel/__main__.py` 行号与条件）。
- 第 3–5、7 节的命令与输出本次核对均已实际执行复核；机制性解释为源码静态
  推导，文中已分别标明。复核时若实际观察与本文冲突，以实际观察为准并修正
  本文。
- 本次仅交付本说明。README、程序、事件数据格式、导入的追加语义、已有访问
  时段筛选语义与各明细开关语义均保持现状；报告不改写事件记录；未新增任何
  功能。
