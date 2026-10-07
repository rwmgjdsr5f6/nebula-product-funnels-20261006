# `--include-pairs` 配对明细：逐条复核源码说明

核对日期：2026-10-07
核对基线：当前工作树，HEAD `473c37a`，工作树干净。

## 1. 核对依据与范围

本说明只围绕**现有配对选择流程**，让复核者用一份九条记录的小型合成输入，逐条复核
`conversion_pairs` 中每个配对的事件依据：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> [--within-seconds N]
                      [--include-users] [--include-pairs]
```

叙述从公开 `report` 入口开始，经有效事件配对、每人归约为一组时间戳，到最终 JSON 结束。
关键结论全部指向当前仓库的真实文件、函数与条件；本说明不要求也不描述任何功能变更，
README、程序、数据格式、追加导入语义与已有的 `--within-seconds` /
`--visit-from`/`--visit-before` 时间筛选语义均保持现状，报告不改写事件记录。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、配对归约与输出 |
| `tests/test_report_include_pairs.py` | `--include-pairs` 的验收样例、配对选择、排序、行序/重复导入与错误协议回归 |
| `tests/test_report_include_users.py` | `--include-users` 与两开关合用的回归 |
| `README.md` | 公开命令、统计规则（含配对规则的公开描述） |
| `docs/verification-jsonl-funnel.md` | 导入与无时段报告的核对说明（姊妹篇） |
| `docs/verification-visit-window.md` | 访问时段筛选与时限参数的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `VALID_EVENTS` | 25 | 导入侧合法事件 `("visit", "signup")` |
| `cmd_import` | 130–156 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 159–178 | `--within-seconds` 取值校验（全 ASCII 数字且大于零） |
| `_visit_window_clause` | 181–185 | 段内 visit 过滤片段 `>= ? AND < ?`（本说明不展开时段语义） |
| `_converted_sql` | 188–207 | 转化查询：自连接、有效配对条件，`select` 可换人数/编号/明细 |
| `_converted_params` | 210–216 | 转化查询的绑定参数装配 |
| `cmd_report` | 239–349 | `report` 子命令：参数检查、分母/分子查询、明细归约、JSON 输出 |
| — 配对归约 | 306–330 | `--include-pairs`：每人先最早 signup 再最晚 visit 的 Python 归约 |
| — 编号明细 | 288–305 | `--include-users`：两个编号数组的查询与排序 |
| `main` | 352–403 | argparse 子命令与开关装配 |

**数值来源声明：** 下文所有退出码、标准输出/标准错误文本、人数、比例与配对内容，均为
对 `funnel/__main__.py` 当前源码（含其 SQL 与 Python 表达式）以及 CPython 标准库
`json`/`argparse`/`sqlite3` 行为的**静态推导**，不是在本机实际执行命令的观测结果。
验收时应以"完整输入 + 确定输出 + 源码对应关系"三者一致为准；若实际观察与本文推导
不符，以实际观察为准并据此修正本文，而不是反过来。

## 2. 流程总览：从公开入口到最终 JSON

1. **入口与参数解析。** 公开入口是 `python -m funnel ...`
   （`funnel/__main__.py:406-407` → `main`，`:352-403`）。`report` 子命令在
   `:364-400` 装配；`--include-pairs` 与 `--include-users` 都是不带取值的
   `store_true` 开关（`:387-399`），可单独或合用，默认均为假。`--within-seconds`
   的取值经 `type=parse_within_seconds`（`:367-372`）在 `parse_args`
   （`:402`）阶段完成校验——非法值在 `cmd_report` 运行前即被拒绝（见第 8 节）。
2. **参数检查与数据库存在性。** `cmd_report`（`:239`）先做时段成对/顺序检查
   （`:242-261`，本说明样例不用时段），再用 `os.path.exists(args.db)` 判断库存在
   （`:263-265`）；不存在即退出码 2、标准输出为空，且不创建文件（第 8 节）。随后
   `sqlite3.connect` 并执行 `CREATE TABLE IF NOT EXISTS events`（`:268-270`），
   之后只有 `SELECT`：报告只读，不改写任何事件记录（`:270` 之后无
   `INSERT`/`UPDATE`/`DELETE`）。
3. **分母：访问人数。** `SELECT COUNT(DISTINCT user_id) FROM events WHERE
   event = 'visit'`（`:271-284`；带时段时 `WHERE` 追加段内条件）。该查询不引用
   `--within-seconds`，也不引用两个明细开关。
4. **分子：转化人数。** `_converted_sql(args)`（`:188-207`）生成 visit 与 signup 的
   同表自连接，默认 `select` 为 `COUNT(DISTINCT v.user_id)`（`:188`），在
   `:285-287` 执行。**有效配对**的条件全部在这一条 SQL 中：
   - `s.user_id = v.user_id`（`:195`）：同一用户；
   - `s.event = 'signup'`（`:196`），外层 `WHERE v.event = 'visit'`（`:205`）；
   - `s.timestamp > v.timestamp`（`:197`）：注册**严格晚于**访问，同刻与逆序都不成立；
   - 给了 `--within-seconds N` 时追加秒差
     `strftime('%s', s.timestamp) - strftime('%s', v.timestamp) <= ?`
     （`:199-204`）：间隔小于或等于 N 秒，上界含等值；
   - 给了时段时经 `_visit_window_clause` 在 visit 一侧追加
     `v.timestamp >= ? AND v.timestamp < ?`（`:206`）。
5. **编号明细（仅 `--include-users`）。** 用**同一条** `_converted_sql`、只把 select
   换成 `DISTINCT v.user_id`（`:299-305`），分母编号同理（`:292-298`），结果在
   Python 侧 `sorted(...)` 按 Unicode 码点字典序排列。
6. **配对明细（仅 `--include-pairs`）。** 仍用**同一条** `_converted_sql`，select 换成
   `v.user_id, v.timestamp, s.timestamp`（`:313-316`），SQL 返回**全部**有效配对行；
   Python 侧归约为每人一组时间戳（`:312-330`，详见第 3 节）。
7. **比例与最终 JSON。** `conversion_rate = converted_users / visit_users if
   visit_users else 0`（`:337`，真除法；零访问才走整数 `0` 分支）。`payload` 先放
   三个汇总字段（`:338-342`），仅在开关打开时追加编号数组（`:343-345`）与
   `conversion_pairs`（`:346-347`），最后单个 `json.dumps(payload)` 打到标准输出
   （`:348`），退出码 0。

由此可见两个明细开关都**只追加查询与输出字段**：分母查询、分子查询与三个汇总值在开关
打开与否时完全一致。

## 3. 配对归约：为什么先最早注册、再最晚访问

归约代码（`funnel/__main__.py:306-330`）原文逻辑：

```python
best_by_user = {}  # user_id -> [signup_ts, visit_ts]
for user_id, visit_ts, signup_ts in conn.execute(
    _converted_sql(args, "v.user_id, v.timestamp, s.timestamp"),
    _converted_params(args),
):
    current = best_by_user.get(user_id)
    if current is None or signup_ts < current[0]:
        best_by_user[user_id] = [signup_ts, visit_ts]
    elif signup_ts == current[0] and visit_ts > current[1]:
        current[1] = visit_ts
```

随后每个用户输出一个对象（`:323-330`）：

```python
{"user_id": user_id,
 "visit_timestamp": best_by_user[user_id][1],
 "signup_timestamp": best_by_user[user_id][0]}
```

数组按 `sorted(best_by_user)`（`:329`）——即 `user_id` 原值的 Unicode 码点字典序
——排列。规则可直接逐行核对：

1. **候选集 = 全部有效配对。** 进入循环的行都已通过第 2 节第 4 步的 SQL 条件；同刻、
   逆序、超时限、段外访问的行对根本不会被枚举。"最早""最晚"都只在**有效配对内部**
   比较。
2. **先定注册：每用户取时间最早的有效 signup。** 比较只发生在 signup 上
   （`signup_ts < current[0]`，`:318`）：遇到更早的 signup 就整体替换为
   `[signup_ts, visit_ts]`。这一步不看 visit 早晚——注册的选择从不依赖访问。
3. **再定访问：只在 signup 并列最早时，取时间最晚的有效 visit。** `elif`
   （`:320`）保证只有 `signup_ts == current[0]`（仍属于最早那次注册）且
   `visit_ts > current[1]` 时才更新 visit。因此最终 visit 是"**能与最早有效注册配对的
   访问中最晚的一次**"，而不是该用户全局最晚的访问。
4. **为什么是这个次序。** 归约以 signup 最小值为唯一主键、visit 仅作同注册下的次级
   选择：一个多次转化的用户只呈现一次，呈现的是其**最早被转化到的注册时刻**（首次
   转化），以及与该次注册相容的最晚访问（距注册最近的有效访问依据）。全局最晚 visit
   若晚于最早 signup（注册后的访问），它连有效配对都不是，自然不可能中选；后一次
   signup 也不可能把更早的注册"挤掉"。
5. **与行序、重复无关。** 循环没有 `ORDER BY` 依赖，结果由 `<`/`==`/`>` 的最小值/
   最大值语义决定，SQL 行按什么顺序返回都一样；重复事件或重复导入产生的相同行只重复
   比较，不改变 min/max（`:308-311` 注释同义）。
6. **每人至多一条。** 字典按 `user_id` 归并，每个在候选行中出现的用户恰好产生一个
   对象；而"候选行中出现的用户"正是分子查询 `COUNT(DISTINCT v.user_id)` 计数的同一
   集合（同一条 `_converted_sql`，仅 select 不同），所以配对数量恒等于转化人数，
   编号集合恒等于 `converted_user_ids`（第 7 节）。

## 4. 固定合成样例（2026-10-06，九条完整 UTF-8 JSONL 记录）

以下九条记录字段沿用现有格式（`user_id` / `event` / `timestamp`，与 `parse_line`
的校验一一对应），保存为 `acceptance.jsonl`：

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

人物设定（日期统一为 2026-10-06，时间均为 UTC 文本）：

- **u1**：10:00:00、10:00:30、10:03:00 三次 visit；10:01:00、10:02:00 两次 signup。
- **u2**：10:00:00 同时 visit 与 signup（同刻）。
- **u3**：09:59:00 signup、10:00:00 visit（注册早于访问，逆序）。

向新库导入（库文件不存在时导入会新建它并建表，`funnel/__main__.py:141-148`）：

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

源码推导的预期标准输出（一行，退出码 0）：

```json
{"imported": 9}
```

九条记录全部非空且通过 `parse_line`，`len(events)` 即 9（`:155`）。导入为追加语义，
但此处是新库，故库内恰有这九行。

## 5. 同时开启两个明细开关：完整预期 JSON

```sh
python -m funnel report --db events.sqlite --include-users --include-pairs
```

源码推导的预期标准输出（一行，退出码 0，标准错误为空）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_user_ids": ["u1", "u2", "u3"], "converted_user_ids": ["u1"], "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-06T10:00:30", "signup_timestamp": "2026-10-06T10:01:00"}]}
```

逐字段对应源码：

- `visit_users = 3`：分母 `COUNT(DISTINCT user_id)`（`:281-284`），u1/u2/u3 各有
  visit 行。
- `converted_users = 1`：分子 `_converted_sql`（`:285-287`）只命中 u1。
- `conversion_rate = 0.3333333333333333`：`1 / 3` 真除法（`:337`）的双精度值经
  `json.dumps` 序列化。
- `visit_user_ids = ["u1","u2","u3"]`、`converted_user_ids = ["u1"]`：与汇总相同的
  筛选条件，Python 侧按码点排序（`:288-305`）。
- `conversion_pairs`：唯一对象的时间戳保留完整日期与 `T` 分隔符
  （`YYYY-MM-DDTHH:MM:SS`，`:323-328` 原样取库中字符串），即 u1 的
  **10:00:30 visit 与 10:01:00 signup**。

## 6. 逐人复核：九个事件如何归约成一个配对

记 u1 的三次访问为 V0=10:00:00、V1=10:00:30、V2=10:03:00，两次注册为
S1=10:01:00、S2=10:02:00。不带时限时，SQL 枚举的有效配对要求
`s.timestamp > v.timestamp`（`:197`）：

| 行对 | 间隔 | 是否有效 | 依据 |
|---|---|---|---|
| V0 → S1 | 60 秒 | 是 | 10:01:00 > 10:00:00 |
| V1 → S1 | 30 秒 | 是 | 10:01:00 > 10:00:30 |
| V2 → S1 | −120 秒 | **否** | 10:01:00 > 10:03:00 不成立（注册后的访问） |
| V0 → S2 | 120 秒 | 是 | 10:02:00 > 10:00:00 |
| V1 → S2 | 90 秒 | 是 | 10:02:00 > 10:00:30 |
| V2 → S2 | −60 秒 | **否** | 10:02:00 > 10:03:00 不成立（注册后的访问） |

归约过程（`:312-321`，与行序无关，任选一种返回顺序说明）：

1. u1 的候选 signup 最小值是 **S1=10:01:00**；S2=10:02:00 不小于它，
   `signup_ts < current[0]` 为假且不等于它，`elif` 也不成立——**后一次注册不会替代
   最早有效注册**，S2 的两行对归约没有任何贡献。
2. 能与 S1 配对的访问只有 V0、V1（V2 与 S1 严格晚于条件不成立，从未进入候选集）。
   两者 signup 相同，`elif` 取较大 visit，故留下 **V1=10:00:30**。
3. 最终配对：`(V1, S1)` = `(2026-10-06T10:00:30, 2026-10-06T10:01:00)`。

其余三人情形（实际只有两人）：

- **u2（同刻）。** visit 与 signup 都是 10:00:00，`s.timestamp > v.timestamp` 用的
  是严格大于，`10:00:00 > 10:00:00` 不成立（`:197`），自连接不产生任何行对，u2 不
  进入分子也不出现在配对数组中；它有 visit，故仍在分母与 `visit_user_ids` 中。
- **u3（逆序）。** signup 09:59:00 早于唯一 visit 10:00:00，
  `09:59:00 > 10:00:00` 不成立；注册在访问之前，任何时限下都不构成转化，u3 不计
  转化、无配对，但计入访问人数。
- **u1 的 10:03:00 访问（注册后的访问）。** 它严格晚于 S1 与 S2 两次注册，对两次
  注册都满足不了 `s.timestamp > v.timestamp`，因此不是任何有效配对的 visit 端；
  归约的"最晚 visit"只在**能与最早注册 S1 配对**的 V0/V1 之间比较，V2 连候选都不是。
  这排除了"取全局最晚 visit 得到 (10:03:00, ?)"的可能。

## 7. 开关不变量：汇总、数量、编号集合、行序与重复

以下每条均可在源码与回归测试中核对：

1. **明细开关不改变汇总。** 分母/分子查询在 `:281-287` 无条件执行；两个开关只决定
   是否多查编号（`:288-305`）、是否多查明细（`:306-330`）以及 `payload` 是否追加键
   （`:343-347`）。且三类结果共用 `_converted_sql`，仅 select 不同。回归：
   `tests/test_report_include_pairs.py` 的 `test_metrics_identical_between_modes`
   （无窗口、60 秒、45 秒三种条件下开/关 `--include-pairs` 汇总逐项相等）与
   `tests/test_report_include_users.py` 的 `test_metrics_identical_between_modes`。
2. **配对数量等于转化人数。** `best_by_user` 每个键恰产出一个对象
   （`:323-330`），键集合等于 `_converted_sql` 以 `DISTINCT v.user_id` 枚举到的
   用户集合，也就是分子的计数对象。本例 `len(conversion_pairs) = 1 =
   converted_users`；无人转化时两边同为 0、数组为空（29 秒样例与
   `test_signup_only_db_reports_zero_with_empty_pairs`）。测试侧断言见
   `assertPairsReport` 中的 `self.assertEqual(len(pairs), converted_users)`。
3. **配对编号集合与 `converted_user_ids` 一致。** 两者由同一条 SQL、同一筛选条件
   产生（`:299-305` 与 `:313-316`），各自在 Python 侧排序，故
   `sorted(p["user_id"] for p in conversion_pairs) == converted_user_ids`。本例两边
   都是 `["u1"]`；回归 `test_acceptance_pairs_with_include_users`。
4. **行序不改变选择结果。** 配对由 SQL 集合语义加 Python 端 min/max 归约得出，不
   依赖 JSONL 行序或 SQL 返回序（第 3 节第 5 条）。回归用固定打乱顺序的同九条事件：
   `test_shuffled_and_duplicate_events_give_same_pairs`
   （打乱序并再追加一份重复事件，配对与汇总不变）。
5. **重复事件与重复导入不改变选择结果。** 表上无唯一约束，导入是纯 `INSERT` 追加
   （`:144-148`），重复行只让候选行翻倍；严格 `<`/`>` 归约对相同时间戳幂等，
   `DISTINCT` 计数也不受影响。回归 `test_reimport_same_batch_keeps_pairs`
   （同一文件连续导入两次，配对不变）。
6. **输出顺序确定。** `conversion_pairs` 按 `sorted(best_by_user)` 排列
   （`:329`），即 `user_id` 原值的 Unicode 码点字典序（区分大小写、保留空白与中文、
   不按数字大小）；多用户排序的回归为 `test_pairs_sorted_by_code_point`。
7. **报告只读。** `cmd_report` 对库仅执行 `CREATE TABLE IF NOT EXISTS` 与 `SELECT`；
   连续报告（含带时限与两个开关）后事件表逐行不变，回归
   `test_report_keeps_events_unchanged`。

## 8. 同一输入下的 30 秒与 29 秒时限

时限在有效配对条件上追加 `秒差 <= N`（上界含等值，`:199-204`），只改变分子，不改变
分母（分母查询不含该条件）。仍同时开启两个明细开关。

### 8.1 `--within-seconds 30`：配对保持

```sh
python -m funnel report --db events.sqlite --within-seconds 30 \
    --include-users --include-pairs
```

第 6 节的六个行对按间隔重新过滤：V0→S1 为 60 秒、V0→S2 为 120 秒、V1→S2 为 90 秒，
均 `> 30` 被排除；**V1→S1 恰为 30 秒，`<= 30` 成立（上界含等值）**，是唯一候选。
最早 signup 仍是 S1，可配对 visit 只剩 V1，配对内容与第 5 节完全一致。源码推导的预期
标准输出（退出码 0）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "visit_user_ids": ["u1", "u2", "u3"], "converted_user_ids": ["u1"], "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-06T10:00:30", "signup_timestamp": "2026-10-06T10:01:00"}]}
```

### 8.2 `--within-seconds 29`：转化人数为零，数组为空

```sh
python -m funnel report --db events.sqlite --within-seconds 29 \
    --include-users --include-pairs
```

u1 最短的有效间隔是 V1→S1 的 30 秒，`30 <= 29` 不成立；其余行对间隔更长或本就无效，
u2 同刻、u3 逆序依旧不成立。候选行为空：分子 0、`converted_user_ids` 与
`conversion_pairs` 均为空数组；分母不涉及时限，访问人数仍为 3；比例走
`0 / 3` 真除法分支，文本是 `0.0`（不是整数 `0`，`:337`）。源码推导的预期标准输出
（退出码 0）：

```json
{"visit_users": 3, "converted_users": 0, "conversion_rate": 0.0, "visit_user_ids": ["u1", "u2", "u3"], "converted_user_ids": [], "conversion_pairs": []}
```

## 9. 两个错误示例：退出码 2、标准输出为空

### 9.1 时限为 0：指出参数与"大于零"，在访问数据库前拒绝

```sh
python -m funnel report --db events.sqlite --within-seconds 0 \
    --include-users --include-pairs
```

`"0"` 形态上全为 ASCII 数字，通过 `WITHIN_SECONDS_RE`（`:166`），但去前导零后为空，
`parse_within_seconds` 抛出 `argparse.ArgumentTypeError("必须大于零，得到 '0'")`
（`:170-172`）。该校验在 `main` 的 `parse_args`（`:402`）阶段执行，**先于
`cmd_report`**，因此先于 `os.path.exists`（`:263`）与一切数据库访问。argparse 统一以
退出码 2 结束、不输出异常堆栈。源码推导：**退出码 2、标准输出为空**，标准错误包含
usage 两行与下面的错误行（错误行同时指出 `--within-seconds` 与大于零的要求）：

```text
usage: python -m funnel report [-h] --db DB [--within-seconds N]
                               [--visit-from YYYY-MM-DDTHH:MM:SS]
                               [--visit-before YYYY-MM-DDTHH:MM:SS]
                               [--include-users] [--include-pairs]
python -m funnel report: error: argument --within-seconds: 必须大于零，得到 '0'
```

即使 `--db` 指向不存在的路径，结论也不变：参数校验先发生，该库文件不会被创建，也不
产生 journal/wal/shm 等附属文件（与
`tests/test_within_seconds_validation.py` 的
`test_invalid_param_with_missing_db_creates_nothing` 同源；
`tests/test_report_include_pairs.py` 的
`test_invalid_within_seconds_still_rejected` 断言退出码 2、标准输出为空且标准错误
含 `--within-seconds`）。`00`、`000` 等全零值同理（回显原值）；含非数字字符的值则走
"只含 ASCII 数字的正整数"原因（`:166-169`），本说明不再展开。

### 9.2 报告数据库不存在：指出路径与原因，不创建文件

参数全部合法（带上两个明细开关）而库路径不存在时：

```sh
python -m funnel report --db /path/to/missing.sqlite \
    --include-users --include-pairs
```

`cmd_report` 的 `os.path.exists(args.db)`（`:263`）为假，立即打印
`"%s: 数据库不存在" % args.db`（`:264`）并 `return 2`（`:265`）。源码推导：**退出码
2、标准输出为空**，标准错误为：

```text
/path/to/missing.sqlite: 数据库不存在
```

其中包含数据库路径与"数据库不存在"原因；判断发生在 `sqlite3.connect`（`:268`）
之前，故**不会顺手创建该文件**（路径按命令行传入原样回显）。回归：
`tests/test_report_include_pairs.py` 的 `test_missing_db_still_rejected`
（断言退出码 2、标准输出为空、标准错误含路径、文件仍不存在）与
`tests/test_report_include_users.py` 的 `test_missing_db_still_rejected`。

## 10. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 4 节九条 JSONL 逐字节）、**确定输出**
  （第 4–5 节导入与双开关报告 JSON、第 8 节 30/29 秒完整 JSON、第 9 节退出码与标准
  错误形态）、**源码对应**（每条结论标注的 `funnel/__main__.py` 行号与条件）。
- 本文数值为源码静态推导，非实际执行观测；复核时若实际观察与推导冲突，以实际观察
  为准并修正本文。
- 本次仅交付本说明。README、程序源码、公开命令与数据格式、导入的追加语义、
  `--within-seconds` 与 `--visit-from`/`--visit-before` 的已有时间筛选语义均保持
  现状；报告对事件记录只读，未新增或修改任何功能。
