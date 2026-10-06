# 合成 JSONL 导入 → visit/signup 两步漏斗：源码行为核对说明

核对日期：2026-10-06
核对基线：当前工作树，HEAD `806bb35`，工作树干净。

## 1. 核对依据与范围

本说明逐项核对"JSONL 导入 → 校验 → SQLite 保存 → 漏斗统计 → JSON 输出"这条链路，
结论全部指向当前仓库的真实文件与函数。程序、README、公开命令与数据格式均以现状为准，
本说明不要求也不描述任何功能变更。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、输出 |
| `funnel/__init__.py` | 包说明，无逻辑 |
| `sample.jsonl` | 2026-10-06 的五条合成事件，与本说明第 3 节样例一致 |
| `README.md` | 公开命令、事件格式、输出与退出码、统计规则、样例预期 |
| `tests/test_import_atomicity.py` | 首错定位与失败原子性的回归测试 |
| `tests/test_import_utf8.py` | 严格 UTF-8、物理行号、中文用户编号的回归测试 |
| `tests/test_report_window.py` | 窗口语义、去重、行序、重复导入、只读报告的回归测试 |

`funnel/__main__.py` 中的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `TIMESTAMP_RE` / `VALID_EVENTS` / `SCHEMA` | 17–28 | 时间格式、合法事件、建表语句 |
| `LineError` | 31–37 | 携带从 1 开始的物理行号与原因 |
| `parse_line` | 40–78 | 单行 JSON 与字段校验 |
| `load_events` | 81–102 | 二进制逐行读取、严格 UTF-8 解码、整文件校验 |
| `cmd_import` | 105–131 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 134–143 | `--within-seconds` 取值校验 |
| `cmd_report` | 146–202 | `report` 子命令：去重计数与 JSON 输出 |
| `main` | 205–229 | argparse 子命令装配 |
| `__main__` 入口 | 232–233 | `python -m funnel` → `sys.exit(main())` |

**数值来源声明：** 下文所有退出码、标准输出/标准错误文本、人数与比例，均为对
`funnel/__main__.py` 当前源码（含其 SQL 与 Python 表达式）以及 CPython 标准库
`json`/`argparse`/`sqlite3` 行为的静态推导，**不是**在本机实际执行命令的观测结果。

## 2. 流程总览：导入、校验、保存、统计、输出如何衔接

公开命令（`funnel/__main__.py:205-229`，与 `README.md:9-23` 一致）：

```sh
python -m funnel import sample.jsonl --db events.sqlite
python -m funnel report --db events.sqlite [--within-seconds N]
```

导入链路（`cmd_import`，`funnel/__main__.py:105-131`）：

1. **整文件校验先于一切数据库操作。** `cmd_import` 先调用 `load_events(args.file)`
   （`funnel/__main__.py:107`）；只有它正常返回全部记录后，才执行
   `sqlite3.connect(args.db)`（`funnel/__main__.py:116`）。因此任一行非法时，
   数据库连接根本不会创建——这是第 6 节"合法前缀不留存、不建新库"的直接原因。
2. **读取与解码。** `load_events` 以二进制逐物理行读取，`enumerate(..., start=1)`
   产生从 1 开始的物理行号（`funnel/__main__.py:93`）；每行严格 `decode("utf-8")`
   （`funnel/__main__.py:95`），随后 `strip()`，空白行跳过但行号照增
   （`funnel/__main__.py:98-100`）；非空行交给 `parse_line`
   （`funnel/__main__.py:101`）。
3. **单行校验。** `parse_line` 依次校验 JSON 可解析、顶层为对象、`user_id` 为非空
   字符串、`event` 属于 `visit`/`signup`、`timestamp` 匹配
   `YYYY-MM-DDTHH:MM:SS` 且为有效日历时间（`funnel/__main__.py:40-78`）。额外字段
   不参与校验、被忽略。任一不符抛出携带物理行号的 `LineError`
   （`funnel/__main__.py:31-37`），按行序首个错误即报即停，不返回部分结果。
4. **事务追加保存。** 校验通过后，在 `with conn:` 事务内先
   `CREATE TABLE IF NOT EXISTS events`（`SCHEMA`，`funnel/__main__.py:22-28`），再
   `executemany` 插入三列 `user_id, event, timestamp`（`funnel/__main__.py:118-123`）。
   表上没有唯一约束，写入是纯 `INSERT`，所以导入是**追加**语义，重复导入会产生重复
   行；写入阶段若抛 `sqlite3.Error`，事务回滚，整批不保存
   （`funnel/__main__.py:126-128`）。
5. **导入输出。** 成功时标准输出仅一行 `json.dumps({"imported": len(events)})`
   （`funnel/__main__.py:130`），退出码 0；JSON 错误/字段错误/UTF-8 错误退出码 2，
   标准错误为 `"<输入路径>: 第 <行号> 行: <原因>"`（`funnel/__main__.py:109`）；
   输入文件本身无法读取（`OSError`）退出码 2，标准错误含输入路径与
   "无法读取输入文件"（`funnel/__main__.py:111-113`）。

报告链路（`cmd_report`，`funnel/__main__.py:146-202`）：

1. **数据库存在性先行。** `os.path.exists(args.db)` 为假即退出码 2，标准输出为空，
   标准错误 `"<数据库路径>: 数据库不存在"`（`funnel/__main__.py:147-149`）。
2. **只读打开。** 连接后执行 `CREATE TABLE IF NOT EXISTS events`
   （`funnel/__main__.py:154`，保证空库可查），随后只有 `SELECT`，全文不存在
   `INSERT`/`UPDATE`/`DELETE`——正常报告不改变任何已有事件记录。
3. **分母（访问人数）。** `SELECT COUNT(DISTINCT user_id) FROM events WHERE
   event = 'visit'`（`funnel/__main__.py:155-157`）。
4. **分子（转化人数）。** visit 与 signup 在同表自连接，条件
   `s.user_id = v.user_id AND s.event = 'signup' AND s.timestamp > v.timestamp`，
   `COUNT(DISTINCT v.user_id)`（`funnel/__main__.py:159-169`）。带窗口时追加
   `strftime('%s', ...)` 秒值之差 `<= N`（`funnel/__main__.py:172-185`）。
5. **比例与输出。** `conversion_rate = converted_users / visit_users if
   visit_users else 0`（`funnel/__main__.py:192`），再 `json.dumps` 输出三键对象
   （`funnel/__main__.py:193-201`），退出码 0。查询阶段抛 `sqlite3.Error` 时退出码
   2、标准输出为空、标准错误 `"<数据库路径>: 数据库无法访问: <原因>"`
   （`funnel/__main__.py:188-190`）。

## 3. 固定合成样例（2026-10-06）

以下五条完整 UTF-8 JSONL 记录即仓库中的 `sample.jsonl`（逐字节一致）：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T11:00:00"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:00:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
```

人物设定：u1 在 10:00:00 visit、11:00:00 signup；u2 仅在 10:00:00 visit；u3 在
09:00:00 signup、10:00:00 visit。

依次执行（数据库文件不存在时，导入会新建它并建表，见 `funnel/__main__.py:116-119`）：

```sh
python -m funnel import sample.jsonl --db events.sqlite
python -m funnel report --db events.sqlite
```

源码推导的预期标准输出（各一行）：

```json
{"imported": 5}
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

导入数为 5：五条记录全部非空且通过 `parse_line`，`len(events)` 即 5
（`funnel/__main__.py:130`）。

分母 `visit_users = 3` 的逐人来源（`funnel/__main__.py:155-157`）：

- u1：存在 10:00:00 的 `visit` 行，计入。
- u2：存在 10:00:00 的 `visit` 行，计入。
- u3：存在 10:00:00 的 `visit` 行，计入（它另有一条更早的 signup，不影响分母）。

分子 `converted_users = 1` 的逐人来源（自连接条件
`s.timestamp > v.timestamp`，`funnel/__main__.py:159-169`）：

- u1：唯一 visit 10:00:00，signup 11:00:00，`11:00:00 > 10:00:00` 成立，连接命中，
  经 `DISTINCT` 计为 1 人。
- u2：只有 visit 行、没有同用户的 signup 行，连接找不到 `s` 行，不计入。
- u3：signup 09:00:00、visit 10:00:00，`09:00:00 > 10:00:00` 不成立；signup 严格
  **早于** visit，不构成转化，不计入。

比例 `0.3333333333333333`：`funnel/__main__.py:192` 用 `/` 做真除法，`1 / 3` 即该
双精度浮点值，`json.dumps` 照此序列化。时间戳为定宽 ISO 文本，按字典序比较与按
时间先后比较在此格式下等价；窗口分支则显式使用 `strftime('%s', ...)` 秒值
（`funnel/__main__.py:180-181`）。

## 4. `--within-seconds` 窗口：60 秒与 3600 秒

带窗口时，分子在"signup 严格晚于 visit"之外追加秒差上界
（`funnel/__main__.py:172-185`）：

```sql
CAST(strftime('%s', s.timestamp) AS INTEGER)
  - CAST(strftime('%s', v.timestamp) AS INTEGER) <= ?
```

参数由 `parse_within_seconds` 校验：仅接受全为 ASCII 数字且大于零的整数，允许前导
零（`funnel/__main__.py:134-143`）；非法值经 argparse 以退出码 2 拒绝，标准输出为空，
标准错误指出 `--within-seconds` 与原因，不触及数据库。

同一数据使用 60 秒窗口：

```sh
python -m funnel report --db events.sqlite --within-seconds 60
```

源码推导的预期输出：

```json
{"visit_users": 3, "converted_users": 0, "conversion_rate": 0.0}
```

- 分母仍是 3：窗口只追加 signup 一侧的条件，visit 计数查询不变。
- 分子为 0：u1 的 visit→signup 间隔为整 3600 秒，`3600 <= 60` 不成立；u2 无
  signup；u3 的 signup 早于 visit，严格晚于条件本就不成立。
- 比例数值为 0。文本上是 `0.0` 而非 `0`：此处走的是 `converted_users /
  visit_users` 真除法分支（`0 / 3` 得浮点 `0.0`）；整数 `0` 只在下一节的零访问
  `else` 分支出现。这一区别同样为源码推导。

改用 3600 秒窗口：

```sh
python -m funnel report --db events.sqlite --within-seconds 3600
```

预期恢复为无窗口结果：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

**恰好等于窗口上限为何计入：** u1 的间隔恰好 3600 秒，SQL 用的是 `<= ?`
（`funnel/__main__.py:181`），`3600 <= 3600` 成立；同时 `s.timestamp >
v.timestamp`（`funnel/__main__.py:179`）保证相等时刻仍不算转化。因此边界规则是：
严格晚于访问，且间隔**小于或等于** N 秒——上限包含，下限（间隔 0 秒）排除。

## 5. 统计规则（去重、配对、行序与零访问）

以下每条均可直接在报告 SQL 与 Python 表达式中核对：

1. **signup 必须严格晚于某次 visit。** 条件是 `s.timestamp > v.timestamp`
   （`funnel/__main__.py:166` 与 `:179`），用的是 `>` 而非 `>=`；signup 与 visit
   同一时刻不计转化，signup 早于 visit 也不计。
2. **多次访问中任意一对满足即可。** 自连接枚举该用户全部 `(visit, signup)` 行对，
   只要任意一对满足时间（及窗口）条件，该用户就进入 `COUNT(DISTINCT v.user_id)`；
   外层 `DISTINCT` 保证一个用户至多计一次，与满足条件的行对数量无关。
3. **按用户原值去重。** 人数一律 `COUNT(DISTINCT user_id)`
   （`funnel/__main__.py:156,161,174`）。`user_id` 在导入时仅要求是非空字符串、按
   原值落库（`funnel/__main__.py:49-53,120-123`，无归一化、无大小写折叠；中文等
   UTF-8 内容同样按原值处理，见 `tests/test_import_utf8.py` 中的"用户甲"用例）。
4. **不依赖行序。** 人数完全由 SQL 的集合语义得出，查询没有 `ORDER BY` 依赖；打乱
   JSONL 行序导入后报告不变（`tests/test_report_window.py` 的
   `test_shuffled_rows_give_same_metrics`）。
5. **重复事件与重复导入不改变人数。** 重复行会被追加保存（纯 `INSERT`），但
   `DISTINCT` 去重使人数不变；对同一数据库重复执行同一份导入，报告保持一致
   （`tests/test_report_window.py` 的 `test_duplicate_events_give_same_metrics` 与
   `test_reimport_same_batch_appends_without_changing_metrics`）。
6. **零访问时三项均为 0。** 全库无 `visit` 行时 `visit_users = 0`；比例表达式
   `converted_users / visit_users if visit_users else 0`（`funnel/__main__.py:192`）
   走 `else` 分支，避免 `0 / 0`，输出整数 `0`：

   ```json
   {"visit_users": 0, "converted_users": 0, "conversion_rate": 0}
   ```

   该情形对应 `tests/test_report_window.py` 的 `test_signup_only_db_reports_all_zero`
   （只有 signup 的库：有窗口与无窗口均为三个 0）。
7. **报告只读。** `cmd_report` 对数据库仅执行 `CREATE TABLE IF NOT EXISTS` 与
   `SELECT`，不插入、不更新、不删除；正常报告（含带窗口与非法参数被拒）后，事件
   表逐行不变（`funnel/__main__.py:154-185`；`tests/test_report_window.py` 的
   `test_report_keeps_events_unchanged`）。`--within-seconds` 只影响本次统计，不
   改写任何记录（与 `README.md:19-23` 一致）。

## 6. 首错定位与失败原子性

构造一个三行文件（例如 `bad.jsonl`）：第一行为合法记录，第二行为空白行，第三行只有
一个 `{`：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}

{
```

执行：

```sh
python -m funnel import bad.jsonl --db events.sqlite
```

源码推导的行为（`load_events` + `cmd_import`）：

- 退出码 **2**（`funnel/__main__.py:110`）。
- 标准输出为**空**：错误分支直接 `return 2`，不执行 `funnel/__main__.py:130` 的
  成功打印。
- 标准错误**包含输入路径、第 3 行与非法 JSON 原因**。格式串为
  `"%s: 第 %d 行: %s" % (args.file, exc.line_no, exc.reason)`
  （`funnel/__main__.py:109`），其中原因来自
  `"非法 JSON: %s" % exc.msg`（`funnel/__main__.py:45`）。对单独的 `{`，CPython
  JSON 解析器给出的 `exc.msg` 为 "Expecting property name enclosed in double
  quotes"，故推导的标准错误行为：

  ```text
  bad.jsonl: 第 3 行: 非法 JSON: Expecting property name enclosed in double quotes
  ```

  （路径按命令行传入原样回显；英文后缀为 CPython 版本的解析器消息，中文前缀为源码
  固定文本。）
- **行号是物理行号。** 第二行空白虽被跳过不导入，仍占据一个行号
  （`funnel/__main__.py:93-100` 的 `enumerate` 从 1 计数、空白行只 `continue`），
  所以错误定位在第 3 行而非"第 2 条记录"。按行序首个错误即报即停，不报告更后面的
  行，也不输出异常堆栈（与 `tests/test_import_atomicity.py` 中第 4 行场景的约定
  同源）。
- 若第三行字节本身不是合法 UTF-8，`raw.decode("utf-8")`
  （`funnel/__main__.py:95`）会先抛 "UTF-8 编码无效" 的 `LineError`
  （`funnel/__main__.py:96-97`）；更早行的 JSON/字段原因不会被后续行的编码错误覆盖
  （按 `enumerate` 顺序处理，见 `tests/test_import_utf8.py`）。

**合法前缀不能留下：** 第一行虽合法，但 `load_events` 只有在整文件全部通过后才
返回（`funnel/__main__.py:89-102` 累积到列表、遇错抛出），而
`sqlite3.connect`/`INSERT` 都在它之后（`funnel/__main__.py:107-123`）。因此失败时
第一行从未进入数据库：对已存在的库，失败前后 `events` 表内容与行数逐行一致；写入
阶段自身失败时则由 `with conn:` 事务回滚（`funnel/__main__.py:118`）。

**目标数据库原本不存在时不能创建：** 校验失败发生在 `sqlite3.connect`
（`funnel/__main__.py:116`）之前，SQLite 文件连同 journal/-wal/-shm 等附属文件都
不会出现（即临时目录中只剩输入 JSONL）。对应
`tests/test_import_atomicity.py` 与 `tests/test_import_utf8.py` 中各自的
`test_failed_import_does_not_create_database`。

## 7. 报告数据库不存在或无法访问

- **路径不存在：** `cmd_report` 先用 `os.path.exists(args.db)` 判断
  （`funnel/__main__.py:147`），不存在即退出码 **2**、标准输出为**空**、标准错误
  `"<数据库路径>: 数据库不存在"`（`funnel/__main__.py:148`）——包含数据库路径与原因，
  且因连接尚未发生，不会顺手创建该文件。

  ```sh
  python -m funnel report --db /path/to/missing.sqlite
  # 推导的 stderr：/path/to/missing.sqlite: 数据库不存在
  ```

- **路径存在但无法访问：** 连接、建表或查询阶段抛出任何 `sqlite3.Error`
  （如文件损坏、不是有效 SQLite 数据库、权限导致的打开失败等），被
  `funnel/__main__.py:188-190` 捕获：退出码 **2**、标准输出为**空**、标准错误
  `"<数据库路径>: 数据库无法访问: <异常信息>"`，包含数据库路径与具体原因。

- **输入侧对称约定：** 导入时输入文件无法读取（`OSError`）同样退出码 2、标准输出
  为空、标准错误含输入路径与"无法读取输入文件"原因
  （`funnel/__main__.py:111-113`）；导入目标库在写入阶段不可达时，标准错误为
  `"<数据库路径>: 数据库无法访问: <异常信息>"`（`funnel/__main__.py:126-128`）。

## 8. 与 README、公开命令、数据格式的一致性

- 公开命令、`--db`/`--within-seconds` 参数与本说明第 2–4 节完全一致
  （`README.md:9-23` 对 `funnel/__main__.py:205-229`）。
- 事件格式（UTF-8 JSONL、非空字符串 `user_id`、仅 `visit`/`signup`、
  `YYYY-MM-DDTHH:MM:SS`、额外字段忽略、空白行计物理行号、整批失败语义）与
  `README.md:27-33` 对 `parse_line`/`load_events` 逐条相符。
- 输出与退出码（`{"imported": N}`、报告三键 JSON、失败时退出码 2 且标准输出为空）
  与 `README.md:37-39` 相符。
- 统计规则（严格晚于、窗口上界包含、多次访问任一配对、按原值去重、行序/重复导入
  不影响人数、零访问三个 0）与 `README.md:43-47` 相符。
- 第 3 节的 `{"imported": 5}` 与报告三键预期与 `README.md:51-56` 给出的样例预期
  一致。

本次交付仅更新本说明文档；程序源码、README、公开命令与数据格式均保持现状，未新增
任何功能。全部行为结论以当前 `funnel/__main__.py` 源码为依据，数值为源码推导，未
宣称已实际执行。
