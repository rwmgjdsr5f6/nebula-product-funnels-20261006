# `report --output` 本地 JSON 报告导出：源码行为核对说明

核对日期：2026-10-08
核对基线：当前工作树，HEAD `6facd43`，工作树干净。

## 1. 核对依据与范围

本说明只核对一条**现有**流程：`python -m funnel report --db ... --output ...`
如何把已经能输出到标准输出的漏斗报告另存为本地 JSON 文件。结论全部指向当前
仓库的真实文件与函数；程序源码、README、JSONL 导入格式与既有统计行为本次均
不变更。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 装配、参数校验、报告查询、报告文件保存 |
| `sample.jsonl` | 2026-10-06 的五条虚构事件，与本说明第 3 节样例逐字节一致 |
| `README.md` | 公开命令、`--output` 规则（第 70–78 行）、输出与退出码、样例（第 113–134 行） |
| `tests/test_report_output.py` | `--output` 导出全部保护的回归测试，本说明第 4–6 节逐条引用 |

`funnel/__main__.py` 中与导出直接相关的位置（行号对应当前文件）：

| 函数 / 位置 | 行 | 职责 |
|---|---|---|
| `parse_output_path` | 270–281 | `--output` 取值校验：拒绝空字符串 |
| `_output_error` | 284–288 | 导出错误统一出口：退出码 2、标准输出为空、标准错误含路径与原因 |
| `_prepare_output` | 291–322 | 数据库访问前的路径校验：同路径、父目录、目标类型 |
| `_write_report_file` | 325–357 | 同目录临时文件 + `os.replace` 原子保存，OSError 保护 |
| `cmd_report` | 519–698 | 报告子命令：参数校验 → 查询 → 序列化 → 先存文件 → 再打印 |
| `main` 中 `import` 子命令装配 | 708–711 | `import` 不定义 `--output` |
| `main` 中 `report` 的 `--output` 定义 | 790–799 | 参数声明，`type=parse_output_path`，默认 `None` |
| `main` 解析与分发 | 802–803 | `parse_args` 后调用 `cmd_report` |
| `__main__` 入口 | 806–807 | `python -m funnel` → `sys.exit(main())` |

**数值来源声明：** 下文所有退出码、标准输出/标准错误文本、文件字节内容、人数
与比例，均为对 `funnel/__main__.py` 当前源码（含其 Python 表达式与调用顺序）
以及 CPython 标准库 `json`/`argparse` 行为的静态推导，**不是**在本机实际执行
命令的观测结果。样例预期标为"源码推导"，可按第 3 节命令原样复核。

## 2. 调用顺序总览：校验、查询、保存、打印谁先谁后

公开命令（`funnel/__main__.py:790-799`，与 `README.md:70-74` 一致）：

```sh
python -m funnel report --db events.sqlite --output report.json
```

带 `--output` 时，`cmd_report`（`funnel/__main__.py:519-698`）的实际调用顺序
为 **参数校验 → 报告查询 → 文件保存 → 标准输出**，四者严格先后相扣：

1. **argparse 解析阶段（先于 `cmd_report` 内一切逻辑）。** `main` 调用
   `parser.parse_args(argv)`（`funnel/__main__.py:802`）。`--output` 声明为
   `type=parse_output_path`（`funnel/__main__.py:792`），因此取值在解析期就经过
   `parse_output_path`（`funnel/__main__.py:270-281`）：空字符串抛
   `ArgumentTypeError`，由 argparse 以退出码 2 终止；`--output` 后缺少取值同样由
   argparse 在解析期以退出码 2 拒绝。这两类失败发生在 `args.func(args)`
   分发（`funnel/__main__.py:803`）之前，数据库连接不可能发生。
2. **报告参数校验（数据库访问前）。** 进入 `cmd_report` 后依次检查：
   `--visit-from/--visit-before` 成对且起点严格早于终点
   （`funnel/__main__.py:522-541`）、`--include-group-pairs` 必须搭配
   `--group-by visit-date`（`:546-552`）、`--include-group-latency` 同理
   （`:556-562`），任一不通过即 `return 2`。
3. **导出路径校验（仍在数据库访问前）。** `args.output is not None` 时调用
   `_prepare_output(args.output, args.db)`（`funnel/__main__.py:567-570`，
   实现见 `:291-322`）：规范化后与 `--db` 同路径、父目录不存在、目标是目录（或
   其他已存在的非常规文件）均在此处 `return 2`。
4. **数据库存在性检查。** `os.path.exists(args.db)` 为假才报"数据库不存在"
   （`funnel/__main__.py:572-574`）——它排在第 3 步之后，所以路径类导出错误
   先于它被拒绝，不会顺手创建缺失的数据库。
5. **报告查询（只读）。** 连接后先 `CREATE TABLE IF NOT EXISTS events`
   （`:579`，保证空库可查），其后只有 `SELECT`（分母 `:585-588`、分子
   `:589-591` 及各明细/归组查询）；查询阶段抛 `sqlite3.Error` 时退出码 2、标准
   输出为空（`:662-664`）。整个报告路径没有 `INSERT`/`UPDATE`/`DELETE`。
6. **序列化一次。** 由查询结果装配 `payload`（`:666-682`），再
   `line = json.dumps(payload)`（`:686`）。标准输出与文件共用这同一个字符串，
   文件不另做第二次序列化。
7. **先存文件。** 带 `--output` 时先调用 `_write_report_file(args.output,
   line + "\n")`（`:693`，实现 `:325-357`）；返回非 `None`（退出码 2）则直接
   `return`，**不执行**随后的打印——这是保存失败时标准输出必须为空的直接原因。
8. **再打印标准输出。** 文件保存成功后才 `print(line)`（`:697`），返回 0
   （`:698`）。即文件先落盘、标准输出后出现；成功时两者内容一致，失败时标准
   输出没有任何字节。

顺序结论：任何参数或路径错误都在数据库访问前被拒绝；任何查询失败都在文件保存
前返回；保存失败则在标准输出打印前返回。

## 3. 固定虚构样例：五条 UTF-8 JSONL 事件

以下五条完整 UTF-8 JSONL 记录即仓库中的 `sample.jsonl`（逐字节一致），也是
README 样例（`README.md:113-120`）使用的同一批数据——含完成、未完成与逆序
注册三种情形：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T11:00:00"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:00:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
```

人物设定：u1 在 10:00:00 visit、11:00:00 signup（完成漏斗）；u2 仅 10:00:00
visit（未完成）；u3 在 09:00:00 signup、10:00:00 visit（注册严格早于访问，
逆序不转化）。

第一步，导入（数据库文件不存在时由 `sqlite3.connect` 新建并建表，
`funnel/__main__.py:146-149`）：

```sh
python -m funnel import sample.jsonl --db events.sqlite
```

源码推导的预期结果：退出码 0，标准错误为空，标准输出仅一行：

```json
{"imported": 5}
```

五条记录全部非空且通过 `parse_line` 校验，导入输出为
`json.dumps({"imported": len(events)})`（`funnel/__main__.py:160`），故
`imported` 为 5。

第二步，导出报告：

```sh
python -m funnel report --db events.sqlite --output report.json
```

源码推导的预期结果：退出码 0，标准错误为空，标准输出仍只有单行报告 JSON：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

三个数值的逐人来源（与 README 第 99–111 行统计规则一致）：

- `visit_users = 3`：`SELECT COUNT(DISTINCT user_id) ... WHERE event = 'visit'`
  （`funnel/__main__.py:585-588`，过滤条件由 `_visit_filter` 统一给出，
  `:186-203`）。u1、u2、u3 各有一条 10:00:00 的 visit，去重后 3 人；u3 更早的
  signup 不影响分母。
- `converted_users = 1`：转化查询自连接条件 `s.event = 'signup' AND
  s.timestamp > v.timestamp`（`_converted_sql`，`:206-225`）。u1 的
  signup 11:00:00 严格晚于 visit 10:00:00，计入；u2 无 signup，不计；u3 的
  signup 09:00:00 早于 visit，不满足严格晚于，不计。
- `conversion_rate = 0.3333333333333333`：`converted_users / visit_users if
  visit_users else 0`（`:666`）做真除法，`1 / 3` 的双精度结果经
  `json.dumps` 原样序列化。

**文件侧的预期结果（源码推导）：** 当前工作目录下生成 `report.json`，其字节
内容与标准输出那一行完全相同，即

```text
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}\n
```

（末尾 `\n` 表示一个 LF 换行，文件本身不含该转义文本。）依据：写入函数收到
的是 `line + "\n"`（`:693`），以 UTF-8 编码写入（`:344`）；标准输出由
`print(line)` 产生，`print` 同样补一个 LF（`:697`）。因此文件原始字节等于
标准输出按 UTF-8 编码的字节，文件解析后的 JSON 对象与标准输出解析后的对象
相等。`tests/test_report_output.py` 的
`test_output_file_equals_stdout_and_ends_with_single_lf`（第 160–172 行）
固定了这一关系：文件以恰好一个 LF 结束、`raw == stdout.encode("utf-8")`、
双向 `json.loads` 后与标准输出相等。

## 4. 成功保存时的文件语义

以下每条均可在 `_prepare_output`（`funnel/__main__.py:291-322`）与
`_write_report_file`（`:325-357`）中核对，并由现有导出测试固定。

1. **UTF-8 编码、单个完整 JSON 对象、以一个 LF 结束。** 临时文件以二进制模式
   写入 `line.encode("utf-8")`（`:340-344`），`line` 是 `json.dumps(payload)`
   的单个 JSON 对象文本（`:686`），调用方追加恰好一个 LF（`:693`）。不追加
   导出时间、导出路径或其他任何字段。对应测试
   `test_output_file_equals_stdout_and_ends_with_single_lf`
   （`tests/test_report_output.py:160-172`，断言以 LF 结束、不是两个 LF、解析
   结果等于固定报告）。
2. **解析后与标准输出相同。** 文件与标准输出共用同一条 `line`，仅末尾各有一个
   LF（写文件显式拼接、`print` 隐式补加）；测试同时断言
   `json.loads(文件) == json.loads(标准输出)`（`tests/test_report_output.py:170-172`，
   进程内辅助断言在 `:149-156`）。
3. **目标不存在时创建（父目录已存在的前提下）。** 临时文件写在目标的**同一
   目录**（`directory = dirname(abspath(output_path))`，`:335`；
   `NamedTemporaryFile(..., dir=directory)`，`:340-342`），随后
   `os.replace(tmp_path, output_path)`（`:347`）把完整内容原子地放到目标路径，
   目标原本不存在即表现为新建。对应
   `test_output_creates_missing_file_in_existing_parent`
   （`tests/test_report_output.py:174-188`）的后半段。
4. **已有普通文件整体覆盖，不追加。** `os.replace` 以新文件整体替换旧路径
   （`:347`），旧内容无论长短都不再保留。测试先写入比新报告更长的旧内容
   `PREVIOUS CONTENT LONGER THAN THE NEW REPORT`，导出后断言旧字节消失、
   `visit_users` 在文件中只出现一次、解析结果等于新报告
   （`test_output_overwrites_existing_file_without_appending`，
   `tests/test_report_output.py:190-199`）。
5. **相对路径按当前工作目录解释。** 校验与保存都以
   `os.path.abspath(output_path)` 补全当前工作目录（`:303`、`:335`），不依赖
   数据库位置或包安装位置。测试以子进程把 `cwd` 设为临时目录、传相对文件名
   `rel-report.json`，断言文件落在该 `cwd` 且内容与标准输出一致
   （`test_relative_path_resolved_from_cwd`，
   `tests/test_report_output.py:201-215`）。
6. **父目录由使用者事先准备，程序不创建。** `_prepare_output` 只做
   `os.path.isdir(parent)` 判断（`:313-315`），全流程没有 `mkdir`。同一测试
   先证明父目录缺失时被拒绝且目录不被创建，再由测试自己创建父目录后导出成功
   （`tests/test_report_output.py:174-188`）。
7. **空报告也按同一规则保存。** 零访问时比例走 `else` 分支为整数 `0`
   （`:666`），文件内容为
   `{"visit_users": 0, "converted_users": 0, "conversion_rate": 0}`，由
   `test_empty_report_is_saved_with_zero_rules` 固定
   （`tests/test_report_output.py:242-257`）。

## 5. 错误边界（仅围绕导出）

下列边界只覆盖 `--output` 导出本身。每种失败的统一可观察行为是：退出码 2、
标准输出为空、标准错误指出相关参数或输出路径及原因、不出现 Traceback；路径类
与取值类错误都在数据库访问前拒绝。

### 5.1 `--output` 缺少取值

```sh
python -m funnel report --db ghost.sqlite --output
```

`--output` 声明为带一个取值的参数（`funnel/__main__.py:790-799`），命令行末尾
缺值由 argparse 在 `parse_args`（`:802`）期间直接报错并以退出码 2 退出，
`cmd_report` 不会被调用。源码推导：标准输出为空；标准错误由 argparse 生成，
指出 `--output` 参数（具体英文措辞如 "expected one argument" 随 CPython 版本
而定，本说明不固定该后缀）。对应
`test_missing_value_rejected_before_db_access`
（`tests/test_report_output.py:261-265`）：退出码 2、标准输出为空、标准错误含
`--output`、无 Traceback，并断言原本不存在的 `ghost.sqlite` 事后仍不存在。

### 5.2 `--output` 取空字符串

```sh
python -m funnel report --db ghost.sqlite --output ""
```

取值在解析期交给 `parse_output_path`（`funnel/__main__.py:270-281`）：函数不
做空白裁剪，`text == ""` 即抛 `ArgumentTypeError("必须给出非空的本地文件路径，
得到空字符串")`（`:277-280`），argparse 捕获后以退出码 2 终止，同样先于
`cmd_report` 与一切数据库访问。标准错误指出 `--output` 与源码固定的原因文本。
对应 `test_empty_string_value_rejected_before_db_access`
（`tests/test_report_output.py:267-271`），并断言 `ghost.sqlite` 不被创建。

### 5.3 规范化后与 `--db` 同路径

```sh
python -m funnel report --db events.sqlite --output events.sqlite
```

`_prepare_output` 分别对两个参数做 `os.path.normcase(os.path.abspath(...))`
（`funnel/__main__.py:303-304`）：`abspath` 只按当前工作目录补全并归一并
`.`/`..` 文本形态，`normcase` 按当前系统统一大小写与分隔符；两者相等时打印

```text
--output 与 --db 不能指向同一路径：两者规范化后均为 <规范化绝对路径>
```

（固定文本见 `:306-310`）并返回 2。标准错误同时指出 `--output` 与 `--db`；
此检查只比较路径字符串、不打开任何一个文件。对应测试：

- 直接同路径：`test_output_same_path_as_db_rejected_with_both_params`
  （`tests/test_report_output.py:273-275`）；
- 不同拼写（一侧带 `./`）规范化后相同：
  `test_output_same_path_as_db_different_spellings`（`:277-285`）；
- 数据库与输出同为一个尚不存在的路径时，拒绝发生在数据库存在性检查
  （`funnel/__main__.py:572-574`）之前，文件不被创建：
  `test_output_same_path_rejected_before_missing_db_is_created`（`:287-291`）。

### 5.4 父目录不存在

```sh
python -m funnel report --db events.sqlite --output no-such-dir/r.json
```

`parent = os.path.dirname(output_abs)`，`os.path.isdir(parent)` 为假时经
`_output_error` 返回 2（`funnel/__main__.py:313-315`）。标准错误格式为
`"<输出路径>: 父目录不存在: <父目录绝对路径>"`（`_output_error` 的
`"%s: %s"` 格式，`:284-288`），即同时包含传入的输出路径与缺失的父目录；程序
不创建该目录，也不创建目标文件。对应
`test_missing_parent_reports_path_and_reason`
（`tests/test_report_output.py:295-299`，断言退出码 2、标准输出为空、标准错误
含输出路径、目标不存在）。

### 5.5 目标已存在且是目录

```sh
python -m funnel report --db events.sqlite --output adir   # adir 是已存在的目录
```

`os.path.isdir(output_abs)` 为真时经 `_output_error` 返回 2
（`funnel/__main__.py:316-317`），标准错误为
`"<输出路径>: 目标已存在且是目录，不是普通文件"`，目录内容不被触碰。对应
`test_target_is_directory_rejected`（`tests/test_report_output.py:301-305`）。

补充一个同一检查块覆盖的边界：目标路径既非目录也非普通文件（已存在的 FIFO、
设备文件等；指向目录的符号链接已被 `isdir` 拦截）时，以
`"目标已存在但不是普通文件"` 同样拒绝（`funnel/__main__.py:320-321`），即程序
只整体覆盖普通文件。本说明只陈述源码当前覆盖的这一判断，不引申其他文件类型的
具体行为。

### 5.6 保存阶段发生 OSError

通过第 5.3–5.5 节的路径检查、报告也已查询并序列化之后，保存本身仍可能因磁盘
I/O 等原因失败。`_write_report_file` 的保存方式是"同目录临时文件 +
`os.replace`"（`funnel/__main__.py:325-357`）：

1. 在目标所在目录创建前缀 `.funnel-report-` 的临时文件（`delete=False`，
   `:340-342`）；
2. 写入完整 UTF-8 内容（含结尾 LF），`flush` 后 `os.fsync`（`:344-346`）；
3. `os.replace(tmp_path, output_path)` 原子替换（`:347`）。

任一步抛 `OSError`（`:348`）：先尽力 `os.remove(tmp_path)` 删除临时文件并
忽略清理自身的 `OSError`（`:349-353`），再经 `_output_error` 报
`"<输出路径>: 报告文件无法写入: <系统错误原因>"`（`:354-356`），返回 2。由于
调用处在 `print(line)` 之前（`:693-695` 先于 `:697`），此时标准输出为空。

由此得到两条字节级保护：

- **已有目标保留原字节。** 替换未发生，旧文件从未被截断或部分写入。
- **原本不存在的目标不留下不完整文件。** 失败前内容只存在于临时文件，而临时
  文件在异常分支被删除；目标路径自始至终不存在。

测试不依赖目录权限或特殊身份，而是在进程内经同一入口 `funnel.__main__.main`
处理请求、用 `unittest.mock` 让 `os.replace` 确定抛出 `OSError(EIO,
"simulated save failure")`（测试方法与桩见 `tests/test_report_output.py:113-124`、
`:314-317`）：

- `test_save_failure_preserves_existing_file_bytes`（`:319-345`）：断言
  `os.replace` 恰好以目标路径调用一次、退出码 2、标准输出为空、标准错误含输出
  路径与原因、无 Traceback；旧文件字节逐字节不变；输出目录列表与保存前一致
  （无临时文件残留）；数据库事件行不变。恢复（撤掉 mock）后同一数据库与目标
  可正常导出并整体覆盖旧文件。
- `test_save_failure_with_missing_target_leaves_no_file`（`:347-370`）：目标
  原本不存在时，失败后目标仍不存在、输出目录为空、事件行不变；恢复后正常创建。

### 5.7 参数校验或报告查询失败：不创建、不改写输出文件

- **其他参数校验失败。** 第 2 节第 2 步的成对/逆序时间窗、组内开关依赖等检查
  全部在 `_write_report_file` 之前 `return 2`，保存函数不会被调用。
  `test_param_validation_failure_does_not_touch_existing_file`
  （`tests/test_report_output.py:374-383`）先成功导出一次，再只传
  `--visit-from`（不成对）触发退出码 2，断言已有报告文件字节与失败前完全
  相同。
- **报告查询失败。** 查询在序列化与保存之前
  （`funnel/__main__.py:576-664` 先于 `:686-695`）；对损坏的数据库文件，
  `sqlite3.Error` 分支返回 2（`:662-664`），标准输出为空、标准错误含数据库
  路径，输出文件从未被创建：`test_query_failure_does_not_create_output`
  （`tests/test_report_output.py:385-394`）。
- **失败均不改事件记录。** 报告路径对数据库只有 `CREATE TABLE IF NOT EXISTS`
  与 `SELECT`；`test_failures_keep_events_unchanged`
  （`tests/test_report_output.py:396-411`）对"与 `--db` 同路径、父目录缺失、
  目标是目录"三种导出失败各执行一次，断言 `events` 表行数前后不变。

## 6. 与既有行为的关系：导出不改变统计、字段与无开关时的输出

- **各明细与筛选开关照常控制报告内容。** `payload` 的字段完全由现有开关装配
  （`funnel/__main__.py:666-682`）：`--within-seconds`、
  `--visit-from/--visit-before` 影响统计口径；`--include-users`、
  `--include-pairs`、`--include-latency`、`--group-by visit-date` 及
  `--include-group-pairs`、`--include-group-latency` 决定追加哪些明细。
  `--output` 不在该装配过程中添加任何字段。全部开关与 `--output` 合用时，
  文件解析结果仍与标准输出一致，由
  `test_output_with_all_detail_flags_matches_stdout`
  （`tests/test_report_output.py:217-240`）固定。
- **导出不改变事件记录。** 如第 5.7 节所述，报告全程只读；保存动作只触及
  `--output` 目标及其同目录临时文件。
- **不传 `--output` 时行为维持现状。** 默认值为 `None`（`funnel/__main__.py:793`），
  `:567` 与 `:688` 两个条件分支都不进入，流程退化为原来的"查询 → 打印单行
  JSON"。`test_without_output_behavior_unchanged`
  （`tests/test_report_output.py:415-419`）断言退出码 0、标准错误为空、标准
  输出就是报告三键 JSON 加一个 LF。
- **`import` 不接受 `--output`。** `--output` 只定义在 `report` 子解析器上
  （`:790-799`），`import` 子解析器（`:708-711`）没有该参数；在导入命令上传
  `--output` 由 argparse 以退出码 2 拒绝，且不产生文件：
  `test_import_does_not_accept_output`
  （`tests/test_report_output.py:421-428`）。

## 7. 与 README 的一致性及交付范围

- 第 3 节命令与 `README.md:70-74`、样例段落 `README.md:113-134` 一致；
  `{"imported": 5}` 与报告三键预期即 README 第 118–119、131 行给出的数值。
- 文件规则（UTF-8、单个 JSON 对象、以换行结束、解析后与标准输出一致、不存在
  则创建、普通文件整体覆盖、相对路径按当前工作目录、父目录须事先存在、不追加
  额外字段）与 `README.md:76` 逐条对应 `_write_report_file`
  （`funnel/__main__.py:325-357`）。
- 错误规则（缺值/空字符串、与 `--db` 同路径、父目录缺失、目标是目录/无法
  写入，均退出码 2、标准输出为空、标准错误含参数或路径与原因、路径错误先于
  数据库访问、查询失败不碰文件、保存失败保留旧文件且不残留不完整文件）与
  `README.md:78` 及第 95 行的退出码条目一致，对应本说明第 5 节引用的测试。

本次交付仅新增本说明文档；产品源码、README、JSONL 导入格式与既有统计行为均
保持现状，未新增或修改任何功能。全部行为结论以当前 `funnel/__main__.py` 源码
与 `tests/test_report_output.py` 现有测试为依据，样例数值为源码推导，未宣称
已实际执行；复核口径为：按第 3 节样例与命令可复现推导结果，每项结论可在
所列源码行与测试用例中核对。
