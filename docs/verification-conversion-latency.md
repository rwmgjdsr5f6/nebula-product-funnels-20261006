# 整体转化耗时（--include-latency）：源码行为核对说明

核对日期：2026-10-08
核对基线：当前工作树，HEAD `241689b`，工作树干净。

## 1. 核对依据与范围

本说明只覆盖一条**已有**报告流程的整体转化耗时部分：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> --include-latency
       [--within-seconds N]
```

目的是让复核者用一份小型合成输入，逐人核对"哪些事件构成有效配对、每个转化
用户贡献哪一个耗时、最终 `conversion_latency` 的三个数值怎么算出来"。结论
全部指向当前仓库的真实文件、函数与条件；本说明不要求也不描述任何功能变更，
现有命令、输出字段、数据格式、追加导入、访问时段筛选语义、报告不改写事件
记录的行为均保持现状。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、配对归约、耗时装配、输出 |
| `tests/test_report_include_latency.py` | 耗时开关、配对选择对耗时的影响、空结果、分组合用、只读性的回归测试 |
| `docs/verification-conversion-pairs.md` | 配对明细的核对说明（姊妹篇，配对选择规则相同） |
| `docs/verification-jsonl-funnel.md` | 导入与基础报告的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `TIMESTAMP_FORMAT` / `VALID_EVENTS` | 23 / 27 | 时间戳解析格式与合法事件 |
| `WITHIN_SECONDS_RE` | 30 | `--within-seconds` 严格形态（`\A[0-9]+\Z` 锚定） |
| `parse_line` / `load_events` | 55–129 | 单行校验与整文件严格 UTF-8 校验 |
| `cmd_import` | 132–158 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 161–180 | `--within-seconds` 取值校验（大于零） |
| `_converted_sql` / `_converted_params` | 190–218 | 有效配对枚举 SQL 与参数装配 |
| `_reduce_pairs` | 273–288 | 配对归约：每人锁定最早有效 signup 与能配对它的最晚 visit |
| `_latency_payload` | 310–337 | 由归约结果装配 `conversion_latency` 三项数值 |
| `cmd_report` | 389–532 | `report` 子命令：参数检查、查询、归约、JSON 输出 |
| `cmd_report` 中配对/耗时块 | 467–482 | 三个明细开关共用一次归约 |
| `cmd_report` 中耗时字段装配 | 526–528 | 只在顶层追加 `conversion_latency` |
| `main` | 535–615 | argparse 子命令与参数装配（`--include-latency` 在 584–592） |

**数值来源声明：** 下文所有退出码、标准输出/标准错误文本、人数、比例与耗时
数值，首先是对 `funnel/__main__.py` 当前源码（含其 SQL 与 Python 表达式）
以及 CPython 标准库 `json`/`argparse`/`sqlite3` 行为的**静态推导**。其中第
3、4、5、7 节列出的命令已在核对当日于本基线上**实际执行一次**，退出码与
标准输出同推导逐字节一致——这几处同时是执行观察；其余未执行的内容（如第 6
节不变量的组合情形）仅为源码推导，不标为实测。验收时应以"完整输入 + 确定
输出 + 源码对应关系"三者一致为准；若实际观察与本文推导不符，以实际观察为
准并据此修正本文，而不是反过来。

## 2. 耗时计算流程：从公开入口到最终 JSON

以下每一步均可直接在源码中核对，串联起来就是 `--include-latency` 的完整
链路：

1. **公开入口。** `python -m funnel report` 由 `main`（`:535-615`）装配，
   `--include-latency` 是不带取值的开关（`action="store_true"`，`:584-592`），
   可单独使用，也可与 `--include-pairs`、`--include-users`、
   `--within-seconds`、`--visit-from/--visit-before`、`--group-by visit-date`
   叠加。参数解析在 `parse_args`（`:614`）完成，非法取值在此阶段即以退出码
   2 拒绝。
2. **有效事件配对（SQL 枚举）。** `cmd_report` 在 `:467-478` 判断：只要
   `--include-pairs`、`--include-group-pairs`、`--include-latency` 三者任一
   开启，就调用 `_converted_sql(args, "v.user_id, v.timestamp, s.timestamp")`
   ——与汇总转化人数使用**同一函数、同一筛选条件**，仅 `select` 列表不同。
   该 SQL（`:190-209`）把 `events` 自连接：visit 一侧 `v.event = 'visit'`，
   signup 一侧 `s.event = 'signup'` 且 **`s.timestamp > v.timestamp`（严格
   大于，`:199`）**；提供 `--within-seconds N` 时追加秒差 `<= N`
   （`:201-206`，上界含等值）；提供访问时段时只对 visit 一侧加
   `>= 起点 AND < 终点`（`_visit_window_clause`，`:183-187`）。连接结果即
   全部**有效 (visit, signup) 行对**——耗时只从这些行对里产生。
3. **每人保留一组时间戳（Python 归约，只归约一次）。** `_reduce_pairs`
   （`:273-288`）遍历全部有效行对，按 `user_id` 归并到 `best_by_user`：
   - 若该 `signup_ts` **早于**当前保留的注册时刻，整组替换
     （`signup_ts < current[0]`，`:284-285`）——效果是先锁定**时间最早的
     有效注册**；
   - 若 `signup_ts` 与当前保留值**相等**而 `visit_ts` 更晚，只更新访问时刻
     （`:286-287`）——效果是在能与该最早注册配对的 visit 中取**时间最晚的
     一次**；
   - 晚于最早注册的 signup 行对两个分支都不命中，被丢弃——**后发生的注册
     不会替代最早有效注册**。
4. **由同一归约结果计算耗时。** `_latency_payload`（`:310-337`）不再查询
   数据库，直接遍历 `best_by_user` 的每个值 `[signup_ts, visit_ts]`，用
   `strptime` 按 UTC 求差（`:322-329`）——**每个转化用户恰好贡献一个耗时，
   即该用户归约配对的时间差，而不是其所有行对中的最短间隔**。随后
   `:332-337` 装配三项：`min_seconds`/`max_seconds` 取最小/最大并转整数，
   `mean_seconds` 为总耗时除以转化人数的**真除**算术平均（不取整、各转化
   用户等权）；`durations` 为空（无转化，含无访问）时三项均为 `None`
   （`:330-331`）。
5. **最终 JSON。** `conversion_latency` 只在**顶层**追加（`:526-528`），
   即使开启 `--group-by visit-date`，日期组内也不追加任何耗时字段。汇总
   字段与各明细在 `:515-531` 装配，由 `json.dumps` 单行打印（`:531`）。

**为什么 u1 贡献 30 秒而不是后续事件的 1 秒：** 这是第 3 步归约顺序直接
决定的。归约先按 signup 时刻取最小——锁定的是**最早的有效注册**；只有
signup 相同才比较 visit 取最大。u1 的最早有效注册是 10:01:00，能与它配对
的 visit 是 10:00:00 与 10:00:30，取最晚的 10:00:30，耗时 30 秒。至于
(10:01:59 访问, 10:02:00 注册) 这组间隔仅 1 秒的行对：它的 signup 更晚，
在归约中被"后发生的注册不替代最早有效注册"分支丢弃，**根本不参与耗时**。
这一口径与配对明细完全一致——耗时逐人对应 `conversion_pairs` 里的那次配对
（见第 6 节），不是"该用户所有配对里的最短间隔"。

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

人物设定：u1 在 10:00:00、10:00:30、10:01:59 各访问一次，在 10:01:00、
10:02:00 各注册一次；u2 在 10:00:00 访问、10:01:00 注册；u3 的访问与注册
同在 10:00:00；u4 仅在 10:00:00 访问。

导入新库（数据库文件不存在时导入会新建它并建表，`funnel/__main__.py:142-152`）：

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

预期标准输出（一行，退出码 0）：

```json
{"imported": 10}
```

十条记录全部通过 `parse_line`，`len(events)` 即 10（`:157`）。注意
`json.dumps` 默认分隔符会在冒号后输出一个空格，逐字节文本是
`{"imported": 10}`；本文其余 JSON 预期同理，按 JSON 值比较时与紧凑写法
等价。

## 4. 无转化时限的耗时报告

```sh
python -m funnel report --db events.sqlite --include-latency
```

预期标准输出（一行，退出码 0）：

```json
{"visit_users": 4, "converted_users": 2, "conversion_rate": 0.5, "conversion_latency": {"min_seconds": 30, "max_seconds": 60, "mean_seconds": 45.0}}
```

字段顺序即 `payload` 的装配顺序（`:516-528`）：三个汇总字段在前，
`conversion_latency` 在 `--include-latency` 单独开启时紧随其后。

**分母 `visit_users = 4`**（`:442-445`，无时段时全库 `event = 'visit'` 按
`user_id` 去重）：u1、u2、u3、u4 各计一人。

**分子 `converted_users = 2` 与逐人耗时来源**（有效行对枚举 `:190-209`，
归约 `:273-288`，求差 `:322-329`）：

- **u1：转化，贡献 30 秒。** 有效行对要求 signup 严格晚于 visit（`:199`）。
  u1 的两次注册与三次访问交叉出五组有效行对：以 10:01:00 为 signup 的
  (10:00:00, 10:01:00)、(10:00:30, 10:01:00)，以 10:02:00 为 signup 的
  (10:00:00, 10:02:00)、(10:00:30, 10:02:00)、(10:01:59, 10:02:00)。归约
  先锁定最早的 signup 10:01:00（`:284-285`）；以 10:02:00 为 signup 的三组
  行对（含间隔仅 1 秒的 (10:01:59, 10:02:00)）落入"更晚的 signup"情形被
  丢弃。signup 固定为 10:01:00 后，能与其配对的 visit 有 10:00:00 与
  10:00:30，取最晚的 10:00:30（`:286-287`），耗时
  `10:01:00 - 10:00:30 = 30` 秒。
- **u2：转化，贡献 60 秒。** 唯一 visit 10:00:00 与唯一 signup 10:01:00
  构成唯一有效行对，耗时 60 秒。
- **u3：不转化。** visit 与 signup 同在 10:00:00，配对条件是严格大于
  （`:199`），**相等时刻不成立**，枚举不到任何行对。
- **u4：不转化。** 只有 visit 没有 signup，同样没有行对。

**三项数值**（`:332-337`）：两个耗时为 30 与 60 秒——
`min_seconds = 30`、`max_seconds = 60`（整数）；`mean_seconds` 为
`(30 + 60) / 2` 真除得浮点 `45.0`，经 `json.dumps` 序列化即文本 `45.0`。

## 5. 同一输入加 29 秒转化时限

```sh
python -m funnel report --db events.sqlite --include-latency --within-seconds 29
```

预期标准输出（一行，退出码 0）：

```json
{"visit_users": 4, "converted_users": 1, "conversion_rate": 0.25, "conversion_latency": {"min_seconds": 1, "max_seconds": 1, "mean_seconds": 1.0}}
```

`--within-seconds N` 只追加在转化查询的 signup 一侧（`:201-206`）：间隔
**小于或等于** N 秒的有效行对保留，严格大于条件同时生效。分母查询不引用
它，访问人数仍是 4。

- **u1 仍转化，但耗时从 30 秒变为 1 秒——因为"最早有效注册"换人了。**
  29 秒窗口下，原先最早的注册 10:01:00 与两次更早访问的间隔分别为 60、30
  秒，均 `> 29`，该注册在当前条件下**没有任何合格 visit，不再是有效
  signup**，其两组行对在枚举阶段即被排除。剩下的有效行对只有
  (10:01:59, 10:02:00) 一组（间隔恰 1 秒，`1 <= 29` 成立）：后一次注册
  10:02:00 此时才成为最早有效注册，能与其配对的最晚 visit 即 10:01:59，
  耗时 1 秒。这正说明"最早 signup"始终指**当前筛选条件下的**最早有效
  signup，而不是无条件下锁定后再套窗口。
- **u2 不再转化**：唯一行对间隔 60 秒，`60 <= 29` 不成立。u3、u4 本不
  转化。分子降为 1，比例 `1 / 4 = 0.25`。
- **三项耗时均为 1**：只剩 u1 一个耗时 1 秒，`min_seconds = 1`、
  `max_seconds = 1`（整数），`mean_seconds = 1 / 1` 真除得浮点 `1.0`。

## 6. 耗时开关的不变量

以下各条由源码结构直接保证，并有回归测试佐证（本节组合情形未逐条执行，
为源码推导）：

1. **耗时开关不改变汇总，也不要求其他明细开关。** `--include-latency`
   只向 `payload` 追加 `conversion_latency`（`:526-528`）；三个汇总字段的
   查询与它无关。它可独立使用：不开 `--include-pairs` 时输出中没有
   `conversion_pairs`（`tests/test_report_include_latency.py` 的
   `test_latency_does_not_require_pairs`、`test_without_flag_outputs_only_metrics`）。
2. **配对明细中的每人时间差与耗时一致。** 配对明细与耗时由**同一份**
   `best_by_user` 装配（`:473-482` 只归约一次）：`conversion_pairs` 里每个
   用户的 `signup_timestamp - visit_timestamp` 就是该用户贡献的耗时，
   `conversion_latency` 即这些耗时的 min/max/真除平均
   （`test_latency_consistent_with_pairs_payload`）。配对条数等于转化人数，
   故耗时个数也等于转化人数。
3. **日期分组开启时仅顶层追加耗时。** `--group-by visit-date` 与
   `--include-latency` 合用时，`conversion_latency` 只出现在顶层
   （`:526-528`），各日期组对象不含任何耗时字段（组内装配见
   `_build_visit_date_groups`，`:340-386`，不接收耗时结果；
   `test_latency_not_added_inside_groups`）。
4. **无转化时三项为 null。** `durations` 为空（无转化，含无访问）时
   `_latency_payload` 返回三个 `None`（`:330-331`），JSON 中为 `null`
   （`test_visit_only_gives_null_latency`、`test_signup_only_gives_null_latency`）。
5. **行序、重复事件与重复导入不改变耗时。** 归约只比较时间戳的 min/max
   （`:284-287`），与行对产出顺序无关；重复导入按追加语义产生相同行，
   min/max 不变（`test_shuffled_and_duplicate_events_give_same_latency`、
   `test_reimport_same_batch_keeps_latency`）。
6. **报告只读。** `cmd_report` 对数据库只执行 `CREATE TABLE IF NOT EXISTS`
   与 `SELECT`，正常报告不改写任何事件记录
   （`test_report_keeps_events_unchanged`）。

## 7. 错误示例：退出码 2、标准输出为空

### 7.1 时限为 0：访问数据库之前拒绝

```sh
python -m funnel report --db events.sqlite --include-latency --within-seconds 0
```

行为：**退出码 2、标准输出为空**，标准错误为 argparse 的用法行加错误行，
错误行同时指出参数名与大于零的要求：

```text
python -m funnel report: error: argument --within-seconds: 必须大于零，得到 '0'
```

`parse_within_seconds`（`:161-180`）先以 `WITHIN_SECONDS_RE`
（`\A[0-9]+\Z`，`:30`）要求完整参数值全为 ASCII 数字，再单独拒绝全零取值
（`:172-174`，原因"必须大于零"）。该校验是 argparse 的 `type` 回调，在
`parse_args`（`:614`）阶段触发——**先于 `cmd_report` 运行，更先于任何数据
库访问**：不创建数据库文件，已存在的库记录不变
（`tests/test_report_include_latency.py` 的
`test_invalid_within_seconds_still_rejected`）。

### 7.2 报告数据库不存在：指出路径且不创建文件

```sh
python -m funnel report --db /path/to/missing.sqlite --include-latency
```

行为：**退出码 2、标准输出为空**，标准错误为：

```text
/path/to/missing.sqlite: 数据库不存在
```

包含数据库路径与不存在原因。`os.path.exists` 检查（`:424-426`）发生在
`sqlite3.connect` 之前，因此**不会顺手创建该库**
（`tests/test_report_include_latency.py` 的 `test_missing_db_still_rejected`）。

## 8. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 3 节十条 JSONL 逐字节）、
  **确定输出**（第 3–5 节的导入与报告 JSON、第 7 节的退出码与标准错误
  形态）、**源码对应**（每条结论标注的 `funnel/__main__.py` 行号与条件）。
- 本文数值为源码静态推导；第 3、4、5、7 节的命令已在核对当日实际执行并
  与推导一致，其余未执行部分不标实测。复核时若实际观察与推导冲突，以实际
  观察为准并修正本文。
- 本次仅交付本说明。README、程序、事件数据格式、导入的追加语义、已有
  访问时段筛选与分组语义均保持现状；报告不改写事件记录；未新增任何功能。
