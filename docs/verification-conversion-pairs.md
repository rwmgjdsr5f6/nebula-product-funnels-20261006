# 转化配对明细（--include-pairs / --include-users）：源码行为核对说明

核对日期：2026-10-07
核对基线：当前工作树，HEAD `473c37a`，工作树干净。

## 1. 核对依据与范围

本说明只覆盖一条**已有**报告流程的配对明细部分：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> --include-pairs --include-users
       [--within-seconds N]
```

目的是让复核者用一份小型合成输入，逐条核对"哪些事件构成有效配对、每个转化
用户最终保留哪一组时间戳、最终 JSON 长什么样"。结论全部指向当前仓库的真实
文件、函数与条件；本说明不要求也不描述任何功能变更，现有命令、输出字段、
数据格式、追加导入、访问时段筛选语义、报告不改写事件记录的行为均保持现状。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、配对归约、输出 |
| `tests/test_report_include_pairs.py` | 配对明细开关、配对选择规则、排序去重、只读性的回归测试 |
| `tests/test_report_include_users.py` | 编号明细开关的回归测试 |
| `docs/verification-visit-window.md` | 访问时段与转化时限的核对说明（姊妹篇） |
| `docs/verification-jsonl-funnel.md` | 导入与基础报告的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `TIMESTAMP_RE` / `VALID_EVENTS` | 20 / 25 | 导入侧时间格式与合法事件 |
| `WITHIN_SECONDS_RE` | 28 | `--within-seconds` 严格形态（`\A[0-9]+\Z` 锚定） |
| `parse_line` / `load_events` | 53–127 | 单行校验与整文件严格 UTF-8 校验 |
| `cmd_import` | 130–156 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 159–178 | `--within-seconds` 取值校验（大于零） |
| `_converted_sql` / `_converted_params` | 188–216 | 有效配对枚举 SQL 与参数装配 |
| `cmd_report` | 239–349 | `report` 子命令：参数检查、查询、配对归约、JSON 输出 |
| `cmd_report` 中 `--include-users` 块 | 288–305 | 编号明细：与汇总同一 SQL，Python 侧排序 |
| `cmd_report` 中 `--include-pairs` 块 | 306–330 | 配对明细：与汇总同一 SQL，Python 侧归约 |
| `main` | 352–403 | argparse 子命令与参数装配 |

**数值来源声明：** 下文所有退出码、标准输出/标准错误文本、人数、比例与配对
时间戳，均为对 `funnel/__main__.py` 当前源码（含其 SQL 与 Python 表达式）以及
CPython 标准库 `json`/`argparse`/`sqlite3` 行为的**静态推导**，不是在本机实际
执行命令的观测结果。验收时应以"完整输入 + 确定输出 + 源码对应关系"三者一致
为准；若实际观察与本文推导不符，以实际观察为准并据此修正本文，而不是反过来。

## 2. 配对选择流程：从公开入口到最终 JSON

以下每一步均可直接在源码中核对，串联起来就是 `--include-pairs` 的完整链路：

1. **公开入口。** `python -m funnel report` 由 `main`（`:352-403`）装配，
   `--include-pairs` 与 `--include-users` 都是不带取值的开关
   （`action="store_true"`，`:387-399`），可单独或合用，也可与
   `--within-seconds`、`--visit-from/--visit-before` 叠加。参数解析在
   `parse_args`（`:402`）完成，非法取值在此阶段即以退出码 2 拒绝。
2. **有效事件配对（SQL 枚举）。** `cmd_report` 调用 `_converted_sql(args,
   "v.user_id, v.timestamp, s.timestamp")`（`:313-315`）——与汇总转化人数
   使用**同一函数、同一筛选条件**，仅 `select` 列表不同。该 SQL
   （`:188-207`）把 `events` 自连接：visit 一侧 `v.event = 'visit'`，signup
   一侧 `s.event = 'signup'` 且 **`s.timestamp > v.timestamp`（严格大于，
   `:197`）**；提供 `--within-seconds N` 时追加秒差 `<= N`（`:199-204`，上界
   含等值）；提供访问时段时只对 visit 一侧加 `>= 起点 AND < 终点`
   （`_visit_window_clause`，`:181-185`）。连接结果即该用户的全部**有效
   (visit, signup) 行对**——只有这些行对里的时间戳有资格进入配对明细。
3. **每人保留一组时间戳（Python 归约）。** 归约循环在 `:312-321`：
   遍历全部有效行对，按 `user_id` 归并到 `best_by_user`：
   - 若该 `signup_ts` **早于**当前保留的注册时刻，整组替换
     （`signup_ts < current[0]`，`:318-319`）——效果是先锁定**时间最早的
     有效注册**；
   - 若 `signup_ts` 与当前保留值**相等**而 `visit_ts` 更晚，只更新访问时刻
     （`:320-321`）——效果是在能与该最早注册配对的 visit 中取**时间最晚的
     一次**。
   - 晚于最早注册的 signup 行对两个分支都不命中，被丢弃——**后发生的注册
     不会替代最早有效注册**。
4. **最终 JSON。** 归约结果按 `user_id` 原值的 Unicode 码点字典序升序
   （Python `sorted`，`:329`）展开为 `conversion_pairs` 数组
   （`:323-330`），每个对象只含 `user_id`、`visit_timestamp`、
   `signup_timestamp` 三个字段，时间戳原样输出定宽 ISO 文本（含完整日期）。
   汇总字段与两个明细数组在 `:338-348` 装配，由 `json.dumps` 单行打印
   （`:348`）。

**为什么先选最早有效注册、再选最晚可配对访问：** 这是 `:317-321` 两个分支
直接决定的确定性规则。一个转化用户往往有多次访问、多次注册，能枚举出多组
有效行对；若不做归约，明细行数会随事件数膨胀且随行序变化。固定"最早注册 +
最晚可配对访问"后：每个转化用户恰好一行，结果只取决于事件集合本身，与行序、
重复事件无关（见第 6 节）。最早注册回答"该用户最早何时完成转化"，最晚可配对
访问回答"转化前最后一次访问是哪次"——两者都来自同一组有效行对，因此该访问
必然严格早于该注册（且满足时限），配对本身必然合法。

## 3. 固定合成样例（2026-10-06，九条事件）

以下九条完整 UTF-8 JSONL 记录（字段沿用现有格式：`user_id` / `event` /
`timestamp`，与 `parse_line` 的校验一一对应），保存为 `acceptance.jsonl`：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:30"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:03:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:02:00"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:59:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
```

人物设定：u1 在 10:00:00、10:00:30、10:03:00 各访问一次，在 10:01:00、
10:02:00 各注册一次；u2 在 10:00:00 同一时刻访问并注册；u3 在 09:59:00
注册、10:00:00 访问。

导入新库（数据库文件不存在时导入会新建它并建表，`funnel/__main__.py:140-148`）：

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

源码推导的预期标准输出（一行，退出码 0）：

```json
{"imported": 9}
```

九条记录全部通过 `parse_line`，`len(events)` 即 9（`:155`）。注意 `json.dumps`
默认分隔符会在冒号后输出一个空格，逐字节文本是 `{"imported": 9}`；本文其余
JSON 预期同理，按 JSON 值比较时与紧凑写法等价。

## 4. 同时开启两个明细开关的报告

```sh
python -m funnel report --db events.sqlite --include-pairs --include-users
```

源码推导的预期标准输出（一行，退出码 0）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_user_ids": ["u1", "u2", "u3"], "converted_user_ids": ["u1"], "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-06T10:00:30", "signup_timestamp": "2026-10-06T10:01:00"}]}
```

字段顺序即 `payload` 的装配顺序（`:338-348`）：三个汇总字段在前，
`--include-users` 的两个编号数组居中，`--include-pairs` 的
`conversion_pairs` 在最后。

**分母 `visit_users = 3`**（`:271-284`，无时段时全库 `event = 'visit'` 按
`user_id` 去重）：u1、u2、u3 都至少有一次 visit，各计一人。

**分子 `converted_users = 1` 与唯一配对的逐人来源**（有效行对枚举
`:188-207`，归约 `:312-321`）：

- **u1：转化，配对为 10:00:30 访问 + 10:01:00 注册。**
  有效行对要求 signup 严格晚于 visit（`:197`）。u1 的 10:03:00 访问晚于两次
  注册，与任何 signup 配对都有 `s.timestamp > v.timestamp` 不成立——
  **注册之后的访问不能进入任何有效行对**，枚举阶段即被排除。其余两次访问
  与两次注册交叉出四组有效行对：(10:00:00, 10:01:00)、(10:00:30, 10:01:00)、
  (10:00:00, 10:02:00)、(10:00:30, 10:02:00)。归约先锁定最早的 signup
  10:01:00（`:318-319`）；10:02:00 的行对落入"更晚的 signup"情形，两个
  分支都不命中——**后一次注册不会替代最早有效注册**。signup 固定为
  10:01:00 后，能与其配对的 visit 有 10:00:00 与 10:00:30 两次，取最晚的
  10:00:30（`:320-321`）。
- **u2：不转化。** visit 与 signup 同在 10:00:00，配对条件是严格大于
  （`:197`），**相等时刻不成立**，连接枚举不到任何行对，`best_by_user`
  中没有 u2。
- **u3：不转化。** 唯一的 signup（09:59:00）早于唯一的 visit（10:00:00），
  **逆序**使 `s.timestamp > v.timestamp` 不成立，同样枚举不到行对。

**编号数组**：`visit_user_ids = ["u1", "u2", "u3"]`（`:292-298`，与分母同一
筛选、Python 侧按码点升序），`converted_user_ids = ["u1"]`（`:299-305`，与
分子同一 SQL 仅改 select）。`conversion_pairs` 中的编号集合 `{"u1"}` 与
`converted_user_ids` 一致，数组长度 1 等于 `converted_users`。

**比例 `0.3333333333333333`**：`conversion_rate = converted_users /
visit_users` 真除法（`:337`），`1 / 3` 的双精度浮点值经 `json.dumps`
序列化即该文本。

## 5. 同一输入加转化时限：30 秒与 29 秒

`--within-seconds N` 只追加在转化查询的 signup 一侧（`:199-204`）：间隔
**小于或等于** N 秒的有效行对保留，严格大于条件同时生效。分母查询不引用它，
两种时限下访问人数都是 3。

### 5.1 `--within-seconds 30`：配对保持

```sh
python -m funnel report --db events.sqlite --within-seconds 30 \
    --include-pairs --include-users
```

源码推导的预期标准输出（一行，退出码 0）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_user_ids": ["u1", "u2", "u3"], "converted_user_ids": ["u1"], "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-06T10:00:30", "signup_timestamp": "2026-10-06T10:01:00"}]}
```

u1 的四组有效行对中，(10:00:30, 10:01:00) 间隔恰为 30 秒，`30 <= 30`
成立（上界含等值）；(10:00:00, 10:01:00) 间隔 60 秒被排除；以 10:02:00 为
signup 的两组间隔分别为 120、90 秒，同样被排除。只剩一组行对，归约结果
不变，配对仍是 10:00:30 访问 + 10:01:00 注册。

### 5.2 `--within-seconds 29`：无人转化，明细为空

```sh
python -m funnel report --db events.sqlite --within-seconds 29 \
    --include-pairs --include-users
```

源码推导的预期标准输出（一行，退出码 0）：

```json
{"visit_users": 3, "converted_users": 0, "conversion_rate": 0.0, "visit_user_ids": ["u1", "u2", "u3"], "converted_user_ids": [], "conversion_pairs": []}
```

- **分子为 0**：u1 最紧的一组行对间隔 30 秒，`30 <= 29` 不成立，四组行对
  全部被时限排除；u2、u3 本来就没有有效行对。
- **分母仍是 3**：时限不改变访问统计（`:271-284` 不引用
  `args.within_seconds`）。
- **两个明细数组均为空**：没有转化用户，`converted_user_ids` 与
  `conversion_pairs` 都是 `[]`；`visit_user_ids` 不受时限影响，仍为三人。
- **比例文本是 `0.0` 而非 `0`**：此处走 `0 / 3` 真除法分支得浮点 `0.0`；
  整数 `0` 只在零访问的 `else` 分支出现（`:337`）。

## 6. 明细开关的不变量

以下各条由源码结构直接保证，并有回归测试佐证：

1. **明细开关不改变汇总。** `--include-users`（`:288-305`）与
   `--include-pairs`（`:306-330`）只向 `payload` 追加字段；三个汇总字段的
   查询与这两个开关无关。两种模式的汇总数值一致
   （`tests/test_report_include_pairs.py` 的
   `test_metrics_identical_between_modes`）。
2. **配对数量等于转化人数。** 每个转化用户在 `best_by_user` 中恰好一个键，
   展开后恰好一行；`converted_users` 是同一 SQL 的
   `COUNT(DISTINCT v.user_id)`，两者同源（`:285-287` 与 `:313-315`）。
3. **配对编号集合与转化编号一致。** `conversion_pairs` 的 `user_id` 集合
   等于 `converted_user_ids`——前者是归约字典的键，后者是同一 SQL 的
   `DISTINCT v.user_id`（`test_acceptance_pairs_with_include_users`）。
4. **行序与重复事件不改变选择结果。** 归约只比较时间戳的 min/max
   （`:317-321`），与行对的产出顺序无关；重复事件（含重复导入产生的相同
   行）产生相同行对，min/max 不变（`:306-311` 注释；
   `test_shuffled_and_duplicate_events_give_same_pairs`、
   `test_reimport_same_batch_keeps_pairs`）。
5. **报告只读。** `cmd_report` 对数据库只执行 `CREATE TABLE IF NOT EXISTS`
   与 `SELECT`，正常报告不改写任何事件记录
   （`test_report_keeps_events_unchanged`）。

## 7. 错误示例：退出码 2、标准输出为空

### 7.1 时限为 0：访问数据库之前拒绝

```sh
python -m funnel report --db events.sqlite --within-seconds 0 \
    --include-pairs --include-users
```

源码推导的行为：**退出码 2、标准输出为空**，标准错误为 argparse 的用法行加
错误行，错误行同时指出参数名与大于零的要求：

```text
python -m funnel report: error: argument --within-seconds: 必须大于零，得到 '0'
```

`parse_within_seconds`（`:159-`）先以 `WITHIN_SECONDS_RE`（`\A[0-9]+\Z`，
`:28`）要求完整参数值全为 ASCII 数字，再单独拒绝全零取值（`:170-172`，原因
"必须大于零"）。该校验是 argparse 的 `type` 回调，在 `parse_args`（`:402`）
阶段触发——**先于 `cmd_report` 运行，更先于任何数据库访问**：不创建数据库
文件，已存在的库记录不变（`tests/test_report_include_pairs.py` 的
`test_invalid_within_seconds_still_rejected`；
`tests/test_within_seconds_validation.py` 的
`test_invalid_param_with_missing_db_creates_nothing`）。

### 7.2 报告数据库不存在：指出路径且不创建文件

```sh
python -m funnel report --db /path/to/missing.sqlite \
    --include-pairs --include-users
```

源码推导的行为：**退出码 2、标准输出为空**，标准错误为：

```text
/path/to/missing.sqlite: 数据库不存在
```

包含数据库路径与不存在原因。`os.path.exists` 检查（`:263-265`）发生在
`sqlite3.connect` 之前，因此**不会顺手创建该库**
（`tests/test_report_include_pairs.py` 的 `test_missing_db_still_rejected`）。

## 8. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 3 节九条 JSONL 逐字节）、
  **确定输出**（第 3–5 节的导入与报告 JSON、第 7 节的退出码与标准错误
  形态）、**源码对应**（每条结论标注的 `funnel/__main__.py` 行号与条件）。
- 本文数值为源码静态推导，非实际执行观测；复核时若实际观察与推导冲突，
  以实际观察为准并修正本文。
- 本次仅交付本说明。README、程序、事件数据格式、导入的追加语义、已有
  访问时段筛选语义均保持现状；报告不改写事件记录；未新增任何功能。
