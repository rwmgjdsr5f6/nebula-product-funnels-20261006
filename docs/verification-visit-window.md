# 访问时段筛选与转化时限报告：源码行为核对说明

核对日期：2026-10-07
核对基线：当前工作树，HEAD `5ea8ab3`，工作树干净。

## 1. 核对依据与范围

本说明只覆盖 `python -m funnel report` 的**访问时段筛选**（`--visit-from` /
`--visit-before`）与**转化时限**（`--within-seconds`）这条已有报告流程：用一份
小型合成输入逐人复核每个用户是否计入分母（访问人数）与分子（转化人数），以及
各类非法参数如何在触及数据库之前被拒绝。结论全部指向当前仓库的真实文件、函数
与相关条件；程序、README、公开命令与数据格式均以现状为准，本说明不要求也不
描述任何功能变更。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、参数校验、JSONL 校验、SQLite 读写、漏斗 SQL、输出 |
| `README.md` | 公开命令、参数规则、统计规则、输出与退出码 |
| `tests/test_visit_window_filter.py` | 时段筛选的验收样例与边界回归测试（本说明第 3 节样例与其 `ACCEPTANCE_EVENTS` 一致） |
| `tests/test_within_seconds_validation.py` | `--within-seconds` 取值边界（含"恰好 60 秒计入"）的回归测试 |
| `docs/verification-jsonl-funnel.md` | 导入链路与无时段基础报告的核对说明（姐妹篇） |

`funnel/__main__.py` 中的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `TIMESTAMP_RE` / `VISIT_BOUND_RE` | 19–23 | 事件时间戳与时段边界参数的格式正则（后者用 `\A`/`\Z` 严格锚定） |
| `WITHIN_SECONDS_RE` / `MAX_SQLITE_INTEGER` | 27–32 | 时限参数的纯数字正则与钳制上限 |
| `parse_line` / `load_events` | 52–114 | JSONL 单行校验与整文件读取（导入侧，本说明仅复用其结论） |
| `cmd_import` | 117–143 | `import` 子命令：整文件校验后事务追加 |
| `parse_within_seconds` | 146–165 | `--within-seconds` 取值校验（argparse `type=`） |
| `_visit_window_clause` | 168–172 | 段内 visit 过滤片段：`AND v.timestamp >= ? AND v.timestamp < ?` |
| `_converted_sql` / `_converted_params` | 175–203 | 转化人数查询：自连接、严格晚于、可选秒差上界、可选时段过滤 |
| `parse_visit_bound` | 206–223 | `--visit-from` / `--visit-before` 取值校验（argparse `type=`） |
| `cmd_report` | 226–291 | `report` 子命令：成对与顺序检查、数据库存在性、去重计数与 JSON 输出 |
| `main` | 294–332 | argparse 子命令装配；`parse_args`（331 行）先于一切子命令逻辑执行 |

**数值来源声明：** 下文所有退出码、标准输出/标准错误文本、人数与比例，均为对
`funnel/__main__.py` 当前源码（含其 SQL 与 Python 表达式）以及 CPython 标准库
`json`/`argparse`/`sqlite3` 行为的**静态推导**，不是在本机实际执行命令的观测
结果。推导结果与实际观察的区别见第 8 节。

## 2. 报告流程总览：参数校验、数据库访问、统计、输出如何衔接

公开命令（`funnel/__main__.py:294-332`，与 `README.md:25-32` 一致）：

```sh
python -m funnel report --db events.sqlite \
  --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-06T11:00:00 \
  [--within-seconds N]
```

`cmd_report`（`funnel/__main__.py:226-291`）的执行次序决定了后文全部错误行为：

1. **argparse 取值校验最先发生。** `main` 在 331 行 `parse_args` 时即调用
   `--within-seconds` 的 `type=parse_within_seconds`（`funnel/__main__.py:310`）
   与 `--visit-from`/`--visit-before` 的 `type=parse_visit_bound`
   （`funnel/__main__.py:316,323`）。取值非法时 argparse 直接以退出码 2 结束，
   此时 `cmd_report` 尚未被调用，更不会有任何数据库访问。
2. **成对与顺序检查先于数据库。** `cmd_report` 开头先检查两个时段参数必须成对
   出现（`funnel/__main__.py:229-237`），再检查起点严格早于终点
   （`funnel/__main__.py:238-248`）；两者失败都直接 `return 2`。
3. **数据库存在性检查。** `os.path.exists(args.db)` 为假即退出码 2
   （`funnel/__main__.py:250-252`），连接尚未发生，不会顺手创建该文件。
4. **只读统计。** 连接后执行 `CREATE TABLE IF NOT EXISTS events`
   （`funnel/__main__.py:257`，保证空库可查），随后只有 `SELECT`：分母查询
   （`funnel/__main__.py:258-271`）与分子查询（`_converted_sql`，
   `funnel/__main__.py:272-274`）。全文不存在 `INSERT`/`UPDATE`/`DELETE`——
   正常报告（含带时段、带时限）不改写任何事件记录
   （`tests/test_visit_window_filter.py` 的
   `test_windowed_report_keeps_events_unchanged`）。
5. **比例与输出。** `conversion_rate = converted_users / visit_users if
   visit_users else 0`（`funnel/__main__.py:281`），`json.dumps` 输出三键对象
   （`funnel/__main__.py:282-290`），退出码 0。

三条贯穿全程的口径（与 `README.md:32,40,55` 一致）：

- **时间统一视为 UTC。** 时间戳与时段参数都是无时区后缀的
  `YYYY-MM-DDTHH:MM:SS` 定宽文本，程序不做任何时区换算；比较直接按文本字典序
  （定宽 ISO 文本下与时间先后等价，`funnel/__main__.py:242,263-264` 的注释），
  时限分支则显式用 `strftime('%s', ...)` 秒值（`funnel/__main__.py:189-190`）。
- **访问区间含起点而不含终点。** 过滤条件是 `timestamp >= ? AND
  timestamp < ?`（`_visit_window_clause`，`funnel/__main__.py:172`；分母查询内联
  同一条件，`funnel/__main__.py:268`）。
- **注册可以晚于终点。** 时段条件只加在 visit（`v`）一侧；signup（`s`）一侧只有
  "严格晚于该次 visit"与可选的秒差上界，没有任何与终点比较的条件
  （`_converted_sql`，`funnel/__main__.py:175-194`）。

## 3. 固定合成样例（2026-10-06，八条事件）

以下八条完整 UTF-8 JSONL 记录可直接复制保存为 `acceptance.jsonl`（与
`tests/test_visit_window_filter.py` 的 `ACCEPTANCE_EVENTS` 逐条一致）：

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

人物设定（全部为 2026-10-06 的 UTC 时间）：

- u1：10:00:00 访问，11:00:00 注册。
- u2：仅 10:30:00 访问，没有注册。
- u3：11:00:00 访问（恰在时段终点），11:00:30 注册。
- u4：09:59:30 访问（时段外）、10:00:15 注册、10:00:30 再次访问（时段内）。

导入一个全新的数据库（数据库文件不存在时导入会新建它并建表，
`funnel/__main__.py:128-131`）：

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

源码推导的预期标准输出（一行；`json.dumps` 默认分隔符在冒号后带一个空格）：

```json
{"imported": 8}
```

导入数为 8：八条记录全部通过 `parse_line` 的字段与时间校验
（`funnel/__main__.py:52-90`），`len(events)` 即 8，由
`funnel/__main__.py:142` 打印。字段沿用现有格式：非空字符串 `user_id` 按原值
落库、`event` 仅 `visit`/`signup`、`timestamp` 为 `YYYY-MM-DDTHH:MM:SS`
有效时间（`README.md:34-42`）。

## 4. 访问区间 [10:00:00, 11:00:00)：逐人复核

```sh
python -m funnel report --db events.sqlite \
  --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-06T11:00:00
```

源码推导的预期标准输出：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

**分母 `visit_users = 3`**（`funnel/__main__.py:265-271`：`COUNT(DISTINCT
user_id)`，条件 `event = 'visit' AND timestamp >= '2026-10-06T10:00:00' AND
timestamp < '2026-10-06T11:00:00'`）：

- **u1 计入**：10:00:00 的 visit 恰在起点，`>=` 含起点。
- **u2 计入**：10:30:00 的 visit 在段内。
- **u3 不进入分母**：它唯一的 visit 在 11:00:00，恰等于终点；条件是严格小于
  （`< ?`），终点不含，故 u3 在分母查询中一行都匹配不到。它 11:00:30 的 signup
  与分母无关——分母只看 visit。
- **u4 计入**：10:00:30 的 visit 在段内；`COUNT(DISTINCT user_id)` 按用户原值
  去重，一人只计一次，与段内 visit 次数无关。它 09:59:30 的 visit 在段外，
  不参与本次任何统计。

**分子 `converted_users = 1`**（`_converted_sql`，`funnel/__main__.py:175-194`：
`events v JOIN events s ON s.user_id = v.user_id AND s.event = 'signup' AND
s.timestamp > v.timestamp`，再叠加 `_visit_window_clause` 的段内 visit 条件，
`COUNT(DISTINCT v.user_id)`）：

- **u1 转化**：段内 visit 10:00:00 与 signup 11:00:00 配成一对，
  `11:00:00 > 10:00:00` 成立。signup 晚于时段终点不影响配对——终点条件只约束
  visit 一侧（见第 2 节），这正是"注册可以晚于终点"。
- **u2 不转化**：没有任何 signup 行，自连接找不到 `s` 行。
- **u3 不转化**：它没有任何段内 visit，`v` 一侧为空，连接无从谈起。
- **u4 不转化，且不能借段外访问转化**：u4 有两条 visit。段外的 09:59:30 那条
  被 `_visit_window_clause` 排除，不参与配对——即使它早于 signup 10:00:15 也
  不能借用。段内的 10:00:30 那条与 signup 10:00:15 配对时，
  `10:00:15 > 10:00:30` 不成立（signup 早于该次 visit）。两对都不满足，u4 只进
  分母不进分子。对照：`tests/test_visit_window_filter.py` 的
  `test_outside_visit_excluded_even_when_signup_looks_later` 把时段改成
  [09:30:00, 10:00:00) 后，同一批数据里 u4 的 09:59:30 visit 成为段内 visit，
  signup 虽晚于终点仍可配对，u4 计访问且计转化（1/1）——可见"能否转化"完全由
  配对用的 visit 是否在段内决定。

**"任意一次段内访问满足配对就可转化"的依据**：自连接枚举该用户全部
`(visit, signup)` 行对，只要任意一对满足 `s.timestamp > v.timestamp`（及可选
时限），该用户就进入 `COUNT(DISTINCT v.user_id)`；外层 `DISTINCT` 保证至多计
一次，与满足条件的行对数量无关（`funnel/__main__.py:179-184,192-193`）。

**比例 `0.3333333333333333`**：`funnel/__main__.py:281` 用 `/` 做真除法，
`1 / 3` 即该双精度浮点值，`json.dumps` 照此序列化。

## 5. 同时加 `--within-seconds 60`：时限只改变分子

```sh
python -m funnel report --db events.sqlite \
  --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-06T11:00:00 \
  --within-seconds 60
```

源码推导的预期标准输出：

```json
{"visit_users": 3, "converted_users": 0, "conversion_rate": 0.0}
```

- **分母仍是 3**：`--within-seconds` 只在 `_converted_sql` 里向 signup 一侧追加
  秒差条件（`funnel/__main__.py:186-191`）；分母查询（`funnel/__main__.py:265-271`）
  不引用 `args.within_seconds`，逐字不变。这就是"时限只改变分子"。
- **分子为 0**：追加的条件是
  `CAST(strftime('%s', s.timestamp) AS INTEGER) -
  CAST(strftime('%s', v.timestamp) AS INTEGER) <= ?`（`funnel/__main__.py:188-191`）。
  u1 的 visit→signup 间隔为整 3600 秒，`3600 <= 60` 不成立；u2 无 signup；u3 无
  段内 visit；u4 唯一的段内配对本就不满足严格晚于。无人转化。
- **比例文本是 `0.0` 而非 `0`**：此处走 `converted_users / visit_users` 真除法
  分支，`0 / 3` 得浮点 `0.0`；整数 `0` 只在零访问的 `else` 分支出现
  （`funnel/__main__.py:281`）。

两条边界规则（本样例未直接覆盖，由 SQL 条件与回归测试共同固定）：

- **相等时刻不转化**：配对条件是 `s.timestamp > v.timestamp`
  （`funnel/__main__.py:184`），用 `>` 而非 `>=`；signup 与 visit 同一时刻不算
  转化。时限分支只是在此之上追加 `<= N`，不会放宽严格晚于的要求
  （`README.md:53`）。
- **恰好达到时限上界可计入**：秒差条件是 `<= ?`（`funnel/__main__.py:190`），
  间隔恰好 N 秒仍满足。对应 `tests/test_within_seconds_validation.py` 的
  `test_acceptance_60_seconds_window`：间隔 60 秒在 60 秒窗口内计入，间隔 61 秒
  不计；以及 `test_acceptance_59_seconds_window_excludes_boundary`：同一对事件
  在 59 秒窗口内不计。即上界包含、下限（间隔 0 秒的相等时刻）排除。

## 6. 参数拒绝：退出码 2、标准输出为空、先于数据库访问

以下每一类都以退出码 **2** 结束，**标准输出为空**，标准错误指出相关参数及原因，
且拒绝发生在任何数据库访问（含 `os.path.exists` 检查与 `sqlite3.connect`）之前，
不创建数据库、不改动记录（`tests/test_visit_window_filter.py` 的
`test_param_errors_do_not_create_database`、`tests/test_within_seconds_validation.py`
的 `test_invalid_param_with_missing_db_creates_nothing` 与
`test_invalid_params_keep_existing_events_unchanged`）。

1. **缺少配对参数**：只给 `--visit-from` 或只给 `--visit-before`。
   `cmd_report` 的成对检查（`funnel/__main__.py:229-237`）发现两者恰有一个为
   `None`，标准错误形如
   `--visit-from 与 --visit-before 必须成对使用：已提供 --visit-from，缺少 --visit-before`，
   同时指出两个参数名。
2. **缺少参数值**：如 `--visit-before` 后没有跟值。argparse 在 `parse_args`
   阶段（`funnel/__main__.py:331`）以退出码 2 拒绝，标准错误指出
   `--visit-before` 需要参数值。
3. **起点与终点相等或逆序**：`--visit-from >= --visit-before` 时
   （`funnel/__main__.py:238-248`；定宽 ISO 文本按字典序比较与时间比较等价），
   标准错误形如
   `--visit-from 必须严格早于 --visit-before：得到 --visit-from '...'、--visit-before '...'`，
   指出两个参数及各自取值。
4. **边界格式错误或日期无效**：`parse_visit_bound`
   （`funnel/__main__.py:206-223`）先用 `VISIT_BOUND_RE`（`\A`/`\Z` 严格锚定，
   前后空白、时区后缀、小数秒、缺位一律拒绝）检查形态，再用 `strptime` 排除
   2026-02-30 这类形态合法但日历无效的日期。失败抛出
   `argparse.ArgumentTypeError`，argparse 以退出码 2 结束，标准错误自带
   `--visit-from` 或 `--visit-before` 的参数名前缀与原因（"必须是
   YYYY-MM-DDTHH:MM:SS 格式（UTC，不含空白、时区后缀或小数秒）"或"不是有效
   时间"）。非法值清单见 `tests/test_visit_window_filter.py` 的
   `test_invalid_bound_values_rejected`。
5. **时限为零或并非全由 ASCII 数字组成**：`parse_within_seconds`
   （`funnel/__main__.py:146-165`）先用 `WITHIN_SECONDS_RE`（`\A[0-9]+\Z`，
   空串、空白、换行、正负号、小数、单位后缀、全角数字一律拒绝）检查字符，
   再排除仅由零组成的值（`"0"`、`"000"` 等）。前者标准错误说明"必须是只含
   ASCII 数字的正整数"，后者说明"必须大于零"，均经 argparse 以退出码 2 结束并
   指出 `--within-seconds`。合法值不设数值或位数上限：允许前导零（`00060` 与
   `60` 等价），超过 SQLite INTEGER 上限的值钳制到 `MAX_SQLITE_INTEGER`
   （`funnel/__main__.py:160-164`），不影响任何统计结果。

以上第 1、3 类在 `cmd_report` 开头、`os.path.exists`（250 行）之前返回；第 2、
4、5 类在 `parse_args` 阶段即终止，`cmd_report` 根本不会被调用。因此五类拒绝
都严格先于数据库访问。

## 7. 有效参数遇到不存在的数据库

参数全部合法、但 `--db` 指向的路径不存在时：

```sh
python -m funnel report --db /path/to/missing.sqlite \
  --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-06T11:00:00
```

源码推导的行为（`funnel/__main__.py:250-252`）：

- 退出码 **2**，标准输出为**空**；
- 标准错误为 `"<数据库路径>: 数据库不存在"`——包含数据库路径与不存在的原因；
- 因 `sqlite3.connect` 尚未执行，**不会创建该数据库文件**，目录中不会出现主
  文件或 journal/-wal/-shm 附属文件（`tests/test_visit_window_filter.py` 的
  `test_valid_params_missing_db_follows_path_protocol`）。

## 8. 验收准则与一致性说明

- **验收依据**：以第 3 节的完整输入（八条 JSONL 原样）、第 3–5 节的确定输出
  （`{"imported": 8}`、`{"visit_users": 3, "converted_users": 1,
  "conversion_rate": 0.3333333333333333}`、`{"visit_users": 3,
  "converted_users": 0, "conversion_rate": 0.0}`）以及第 2–7 节给出的源码对应
  关系（文件、函数、行号、条件表达式）为准。
- **推导与观察的区别**：本说明全部数值与文本为对当前源码的静态推导，未宣称已
  实际执行；实际复核时若观察到任何出入，以实际观察为准并据此修正说明。标准
  输出中 JSON 的空格（`json.dumps` 默认分隔符）、标准错误中 argparse 生成的
  usage 行等细节，以本机 CPython 版本的实际输出为准。
- **行为保持现状**：本次仅交付本说明文档；现有命令、输出字段、导入的追加语义
  （纯 `INSERT`，重复导入产生重复行）、按 `user_id` 原值去重、以及报告不改写
  事件记录的只读行为均保持不变，未新增、修改或删除任何程序功能。
