# 访问时段筛选与转化时限报告：源码行为核对说明

核对日期：2026-10-07
核对基线：当前工作树，HEAD `5ea8ab3`，工作树干净。

## 1. 核对依据与范围

本说明只覆盖一条**已有**报告流程：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite>
       --visit-from YYYY-MM-DDTHH:MM:SS --visit-before YYYY-MM-DDTHH:MM:SS
       [--within-seconds N]
```

目的是让复核者用一份小型合成输入，逐人核对"谁计入访问人数、谁计入转化人数"。
结论全部指向当前仓库的真实文件、函数与条件；本说明不要求也不描述任何功能变更，
现有命令、输出字段、追加导入、用户原值去重、报告不改写事件记录的行为均保持现状。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、输出 |
| `tests/test_visit_window_filter.py` | 时段筛选、边界、参数拒绝先于数据库访问的回归测试 |
| `tests/test_within_seconds_validation.py` | `--within-seconds` 取值边界的回归测试 |
| `docs/verification-jsonl-funnel.md` | 导入与无时段报告的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `TIMESTAMP_RE` / `VALID_EVENTS` | 19–24 | 导入侧时间格式与合法事件 |
| `VISIT_BOUND_RE` / `WITHIN_SECONDS_RE` | 23 / 27 | 报告参数专用严格形态（`\A...\Z` 锚定） |
| `parse_line` / `load_events` | 52–114 | 单行校验与整文件严格 UTF-8 校验 |
| `cmd_import` | 117–143 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 146–165 | `--within-seconds` 取值校验 |
| `_visit_window_clause` | 168–172 | 段内 visit 过滤片段 `>= ? AND < ?` |
| `_converted_sql` / `_converted_params` | 175–203 | 转化人数查询与参数装配 |
| `parse_visit_bound` | 206–223 | `--visit-from` / `--visit-before` 取值校验 |
| `cmd_report` | 226–291 | `report` 子命令：参数检查、查询、JSON 输出 |
| `main` | 294–332 | argparse 子命令装配 |

**数值来源声明：** 下文所有退出码、标准输出/标准错误文本、人数与比例，均为对
`funnel/__main__.py` 当前源码（含其 SQL 与 Python 表达式）以及 CPython 标准库
`json`/`argparse`/`sqlite3` 行为的**静态推导**，不是在本机实际执行命令的观测结果。
验收时应以"完整输入 + 确定输出 + 源码对应关系"三者一致为准；若实际观察与本文推导
不符，以实际观察为准并据此修正本文，而不是反过来。

## 2. 统计语义：时段、UTC 与配对规则

以下每条均可直接在源码中核对：

1. **时间统一视为 UTC。** 时间戳是不含时区后缀的定宽 ISO 文本
   `YYYY-MM-DDTHH:MM:SS`（导入侧 `TIMESTAMP_RE`，`funnel/__main__.py:19`；报告参数
   侧 `VISIT_BOUND_RE`，`:23`），比较与差值计算不做任何时区换算，即全部按 UTC
   解释。定宽文本的字典序与时间先后等价（`:242`、`:263-264` 的注释与用法）。
2. **访问区间含起点、不含终点。** 段内 visit 的过滤条件是
   `timestamp >= ? AND timestamp < ?`（`_visit_window_clause`，`:168-172`；分母查询
   本体在 `:265-271`）。恰在起点的 visit 计入，恰在终点的 visit 不计入。
3. **注册可以晚于终点。** 转化查询只对 visit 一侧加时段条件；signup 一侧的唯一时间
   条件是 `s.timestamp > v.timestamp`（`_converted_sql`，`:175-194`，时段片段由
   `_visit_window_clause` 拼在 `v` 上）。因此 signup 晚于段终点不影响配对。
4. **任意一次段内访问满足配对即可转化。** 自连接枚举该用户全部
   `(段内 visit, signup)` 行对，只要任意一对满足 `s.timestamp > v.timestamp`
   （以及时限条件），该用户就进入 `COUNT(DISTINCT v.user_id)`；外层 `DISTINCT`
   保证一个用户至多计一次。段外 visit 因 `WHERE` 子句被排除，既不进入分母，也不
   参与配对。
5. **相等时刻不转化。** 配对条件是严格大于 `>`（`:184`），signup 与 visit 同一时刻
   不成立。
6. **恰好达到时限上界可计入。** 带 `--within-seconds N` 时追加秒差条件
   `... <= ?`（`:188-191`，秒值由 `strftime('%s', ...)` 得出，SQLite 对该格式按
   UTC 解释）：间隔**小于或等于** N 秒成立，间隔恰为 N 秒计入；同时严格大于条件
   保证间隔 0 秒（同一时刻）仍排除。
7. **时限只改变分子。** `--within-seconds` 只出现在 `_converted_sql` 里；分母查询
   （`:258-271`）完全不引用它。加不加时限，访问人数不变。
8. **按用户原值去重、报告只读。** 人数一律 `COUNT(DISTINCT user_id)`，`user_id`
   按导入原值落库、无归一化；`cmd_report` 对数据库只执行
   `CREATE TABLE IF NOT EXISTS` 与 `SELECT`，正常报告不改写任何事件记录
   （`:254-279`；`tests/test_visit_window_filter.py` 的
   `test_windowed_report_keeps_events_unchanged`）。

## 3. 固定合成样例（2026-10-06，八条事件）

以下八条完整 UTF-8 JSONL 记录（字段沿用现有格式：`user_id` / `event` /
`timestamp`，与 `parse_line` 的校验一一对应），保存为 `acceptance.jsonl`：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T11:00:00"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:30:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T11:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T11:00:30"}
{"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T09:59:30"}
{"user_id": "u4", "event": "signup", "timestamp": "2026-10-06T10:00:15"}
{"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:30"}
```

人物设定：u1 在 10:00:00 访问、11:00:00 注册；u2 仅在 10:30:00 访问；u3 在
11:00:00 访问、11:00:30 注册；u4 在 09:59:30 访问、10:00:15 注册、10:00:30
再访问。

导入新库（数据库文件不存在时导入会新建它并建表，`funnel/__main__.py:127-135`）：

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

源码推导的预期标准输出（一行，退出码 0）：

```json
{"imported": 8}
```

八条记录全部通过 `parse_line`，`len(events)` 即 8（`:142`）。注意 `json.dumps`
默认分隔符会在冒号后输出一个空格，逐字节文本是 `{"imported": 8}`；本文其余 JSON
预期同理，按 JSON 值比较时与紧凑写法等价。

## 4. 时段报告：[10:00:00, 11:00:00)

```sh
python -m funnel report --db events.sqlite \
    --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-06T11:00:00
```

源码推导的预期标准输出（一行，退出码 0）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

**分母 `visit_users = 3` 的逐人来源**（`:265-271`，`>= 起点 AND < 终点`）：

- u1：visit 10:00:00 恰在起点，含起点，计入。
- u2：visit 10:30:00 在段内，计入。
- u3：**不进入分母。** 唯一 visit 在 11:00:00，恰在终点；终点不含
  （`timestamp < ?` 不成立），该 visit 被排除，u3 在段内没有任何 visit。
  其 11:00:30 的 signup 与分母无关。
- u4：09:59:30 的 visit 早于起点被排除，但 10:00:30 的 visit 在段内，计入。

**分子 `converted_users = 1` 的逐人来源**（`_converted_sql`，`:175-194`）：

- u1：段内 visit 10:00:00，signup 11:00:00，`11:00:00 > 10:00:00` 成立。signup
  恰在段终点不影响配对——时段条件只约束 visit 一侧（第 2 节第 3 条）。计入。
- u2：没有任何 signup 行，连接找不到 `s` 行，不计入。
- u3：段内无 visit，连接枚举不到任何行对，不计入。
- u4：**不能借段外访问转化。** 若用 09:59:30 的 visit 与 10:00:15 的 signup 配对，
  时间条件本来成立，但该 visit 在段外，被 `_visit_window_clause` 的
  `v.timestamp >= ?` 排除，不参与配对；段内唯一的 visit 是 10:00:30，而 signup
  10:00:15 严格早于它，`s.timestamp > v.timestamp` 不成立。两个方向都失败，
  不计入。

**比例 `0.3333333333333333`**：`conversion_rate = converted_users / visit_users`
真除法（`:281`），`1 / 3` 的双精度浮点值经 `json.dumps` 序列化即该文本。

## 5. 同一区间加 `--within-seconds 60`

```sh
python -m funnel report --db events.sqlite \
    --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-06T11:00:00 \
    --within-seconds 60
```

源码推导的预期标准输出：

```json
{"visit_users": 3, "converted_users": 0, "conversion_rate": 0.0}
```

- **分母仍是 3**：时限只追加在转化查询的 signup 一侧（`:186-191`），分母查询不变
  ——时限只改变分子。
- **分子为 0**：u1 的 visit→signup 间隔为整 3600 秒，`3600 <= 60` 不成立；u2 无
  signup；u3 段内无 visit；u4 的段内 visit 晚于其 signup，严格大于条件本就不
  成立。
- **比例文本是 `0.0` 而非 `0`**：此处走 `0 / 3` 真除法分支得浮点 `0.0`；整数 `0`
  只在零访问的 `else` 分支出现（`:281`）。

**边界规则复述**（第 2 节第 5、6 条）：相等时刻不转化（`>` 严格）；间隔恰好等于
时限上界可计入（`<=`）。本样例在 60 秒窗口下无人恰好落在 60 秒边界上；该边界行为
的回归证据见 `tests/test_within_seconds_validation.py` 的
`test_acceptance_60_seconds_window`（间隔恰 60 秒计入）与
`test_acceptance_59_seconds_window_excludes_boundary`（窗口 59 秒则排除）。

## 6. 参数拒绝：退出码 2、标准输出为空、先于数据库访问

以下每一类非法输入，行为统一为：**退出码 2、标准输出为空、标准错误指出相关参数及
原因、不输出异常堆栈，且拒绝发生在任何数据库访问之前**——不创建数据库文件，已存在
的库记录不变（`tests/test_visit_window_filter.py` 的
`test_param_errors_do_not_create_database`、
`tests/test_within_seconds_validation.py` 的
`test_invalid_param_with_missing_db_creates_nothing` 与
`test_invalid_params_keep_existing_events_unchanged`）。

1. **访问边界缺少配对。** 只传 `--visit-from` 或只传 `--visit-before`：
   `cmd_report` 开头的成对检查（`:229-237`）直接返回 2，标准错误为
   `--visit-from 与 --visit-before 必须成对使用：已提供 <已给>，缺少 <缺失>`，
   同时指出两个参数名。该检查在 `os.path.exists`（`:250`）之前。
2. **访问边界缺少参数值。** 例如命令以 `--visit-before` 结尾而没有取值：argparse
   在 `parse_args`（`:331`）阶段以退出码 2 拒绝，标准错误指出
   `--visit-before` 缺少一个参数值；此时 `cmd_report` 尚未运行。
3. **起终点相等或逆序。** `--visit-from >= --visit-before` 时（`:238-248`）返回 2，
   标准错误为 `--visit-from 必须严格早于 --visit-before：得到 --visit-from
   '<值>'、--visit-before '<值>'`，同时指出两个参数名与两个原值。比较是定宽
   ISO 文本的字典序，与时间先后等价（`:242` 注释）。
4. **边界格式错误或日期无效。** `parse_visit_bound`（`:206-223`）先以
   `VISIT_BOUND_RE`（`\A...\Z` 严格锚定，`:23`）拒绝一切非
   `YYYY-MM-DDTHH:MM:SS` 形态——前后空白、缺秒、缺 `T`、时区后缀、小数秒均在
   此列；形态合法但日历无效（如 `2026-02-30T10:00:00`）再由 `strptime` 拒绝。
   校验失败经 argparse 以退出码 2 结束，标准错误指出对应参数
   （`--visit-from` 或 `--visit-before`）及原因（形态不符或"不是有效时间"），
   发生在 `cmd_report` 运行之前，更在任何数据库访问之前。
5. **时限为零或并非全由 ASCII 数字组成。** `parse_within_seconds`（`:146-165`）：
   `WITHIN_SECONDS_RE`（`\A[0-9]+\Z`，`:27`）只接受完整参数值全为 ASCII 数字；
   空串、空白、正负号、小数、单位后缀、全角数字、末尾换行一律拒绝，原因是"只含
   ASCII 数字的正整数"。全由零组成（`0`、`000` 等）单独拒绝，原因是"必须大于零"。
   同样经 argparse 以退出码 2 拒绝，标准错误指出 `--within-seconds` 及原因，
   先于数据库访问。

以上 1–5 的标准输出均为空：参数错误分支不执行 `:282-290` 的成功打印。

## 7. 有效参数遇到不存在的数据库

参数全部合法但 `--db` 路径不存在时，`cmd_report` 的 `os.path.exists` 检查
（`:250-252`）返回 **退出码 2、标准输出为空**，标准错误为：

```text
<数据库路径>: 数据库不存在
```

包含数据库路径与不存在原因；因为判断发生在 `sqlite3.connect` 之前，**不会顺手创建
该库**（`tests/test_visit_window_filter.py` 的
`test_valid_params_missing_db_follows_path_protocol`）。

```sh
python -m funnel report --db /path/to/missing.sqlite \
    --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-06T11:00:00
# 推导的 stderr：/path/to/missing.sqlite: 数据库不存在
```

## 8. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 3 节八条 JSONL 逐字节）、**确定输出**
  （第 3–5 节的导入与报告 JSON、第 6–7 节的退出码与标准错误形态）、**源码对应**
  （每条结论标注的 `funnel/__main__.py` 行号与条件）。
- 本文数值为源码静态推导，非实际执行观测；复核时若实际观察与推导冲突，以实际
  观察为准并修正本文。
- 本次仅交付本说明。现有命令与参数、输出字段（`imported` 与
  `visit_users`/`converted_users`/`conversion_rate`）、导入的追加语义、按
  `user_id` 原值去重、报告不改写事件记录的行为均保持现状，未新增任何功能。
