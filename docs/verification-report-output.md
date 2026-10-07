# report --output 本地报告导出：源码行为核对说明

核对日期：2026-10-08
核对基线：当前工作树，HEAD `6facd43`，工作树干净。

## 1. 核对依据与范围

本说明只核对现有流程——`python -m funnel report` 的 `--output` 参数把本次报告
保存为本地 JSON 文件。结论全部指向当前仓库的真实文件与函数，并给出实际调用顺序；
程序、README、导入格式与既有统计行为均以现状为准，本说明不要求也不描述任何功能
变更。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、参数校验、漏斗 SQL、报告序列化、文件保存 |
| `funnel/__init__.py` | 包说明，无逻辑 |
| `sample.jsonl` | 2026-10-06 的五条虚构事件，与本说明第 3 节样例逐字节一致 |
| `README.md` | 公开命令、`--output` 规则、输出与退出码、样例预期 |
| `tests/test_report_output.py` | `report --output` 成功保存、错误边界与保存失败保护的回归测试 |

`funnel/__main__.py` 中与导出直接相关的位置（行号对应当前文件）：

| 函数 / 位置 | 行 | 职责 |
|---|---|---|
| `cmd_import` 成功打印 | 160 | `json.dumps({"imported": len(events)})` |
| `parse_output_path` | 270–281 | `--output` 取值校验：拒绝空字符串 |
| `_output_error` | 284–288 | 导出错误统一出口：标准错误写"路径: 原因"，返回 2 |
| `_prepare_output` | 291–322 | 数据库访问前的路径校验：同路径、父目录、目标类型 |
| `_write_report_file` | 325–357 | 同目录临时文件 + `os.replace` 原子保存，`OSError` 保护 |
| `cmd_report` | 519–698 | report 子命令：参数校验 → 查询 → 序列化 → 存文件 → 标准输出 |
| `main` 中 argparse 装配 | 701–803 | `--output` 定义在 790–799；`parse_args` 在 802，分发在 803 |
| `__main__` 入口 | 806–807 | `python -m funnel` → `sys.exit(main())` |

`cmd_report` 内部与导出相关的先后次序（行号对应当前文件）：

| 步骤 | 行 | 内容 |
|---|---|---|
| ① 时间段成对/先后校验 | 522–541 | 缺配对参数或起点不严格早于终点 → 退出 2 |
| ② 分组明细开关组合校验 | 546–562 | `--include-group-*` 未配 `--group-by visit-date` → 退出 2 |
| ③ `--output` 路径校验 | 567–570 | `_prepare_output`，先于一切数据库访问 |
| ④ 数据库存在性检查 | 572–574 | `os.path.exists(args.db)` 为假 → 退出 2 |
| ⑤ 连接、建表、报告查询 | 576–664 | 仅 `CREATE TABLE IF NOT EXISTS` 与 `SELECT`；`sqlite3.Error` → 退出 2 |
| ⑥ 装配 payload 并序列化一次 | 666–686 | `line = json.dumps(payload)`，文件与标准输出共用 |
| ⑦ 文件保存（先于打印） | 688–695 | `_write_report_file(args.output, line + "\n")`，失败即返回 2 |
| ⑧ 标准输出单行报告 | 697–698 | `print(line)`，返回 0 |

**数值来源声明：** 下文所有退出码、标准输出/标准错误文本、人数、比例与文件内容，
均为对 `funnel/__main__.py` 当前源码以及 CPython 标准库
`json`/`argparse`/`sqlite3`/`os`/`tempfile` 行为的静态推导，**不是**在本机实际执行
命令的观测结果。每条结论后附源码或测试位置，可逐项复核。

## 2. 调用顺序：参数校验 → 报告查询 → 文件保存 → 标准输出

带 `--output` 的一次成功报告，实际先后关系如下（`cmd_report`，
`funnel/__main__.py:519-698`）：

1. **argparse 解析先于分发。** `main` 在 `funnel/__main__.py:802` 调用
   `parser.parse_args(argv)`，803 行才 `args.func(args)` 进入 `cmd_report`。
   `--output` 缺值由 argparse 直接拒绝；取空字符串时，注册在 790–799 行的
   `type=parse_output_path` 被调用，`parse_output_path` 抛出
   `ArgumentTypeError`（`funnel/__main__.py:277-280`），由 argparse 转换为退出码 2。
   两者都发生在 `cmd_report` 执行之前，因此不可能连接或创建数据库。
2. **`cmd_report` 内的参数校验全部先于数据库访问。** 时间段配对与先后
   （522–541）、分组明细组合（546–562）之后，567–570 行调用 `_prepare_output`
   做路径校验；再往后才有 572 行的数据库存在性检查与 577 行的
   `sqlite3.connect`。任何一项校验失败都直接 `return 2`，不进入查询，也不创建或
   改写输出文件。
3. **报告查询与既有实现完全相同。** 579 行 `CREATE TABLE IF NOT EXISTS events`
   保证空库可查，随后只有各条 `SELECT`（584 行起）；`--output` 不参与任何 SQL。
   查询阶段抛 `sqlite3.Error` 时由 662–664 行捕获，退出码 2、标准输出为空、标准
   错误为 `"<数据库路径>: 数据库无法访问: <原因>"`——此时尚未序列化、尚未触碰
   输出文件。
4. **序列化只做一次，文件与标准输出共用同一条字符串。** 666–682 行装配 `payload`
   （保存哪些字段完全由既有明细/筛选开关决定，见第 7 节），686 行
   `line = json.dumps(payload)`。`args.output` 自始至终不进入 `payload`，导出不
   添加导出时间、路径或任何其他字段。
5. **文件保存先于标准输出。** 688–695 行：传了 `--output` 时先调用
   `_write_report_file(args.output, line + "\n")`；只有返回 `None`（成功）才继续
   到 697 行 `print(line)`。因此保存阶段抛 `OSError` 时退出码 2、**标准输出为
   空**——成功打印在物理上不可达；保存成功时退出码 0、标准错误为空、标准输出仍
   只有原来的单行报告 JSON（697 行的 `print` 与不传 `--output` 时是同一条语句）。

## 3. 固定合成样例（2026-10-06，五条虚构事件）

以下五条完整 UTF-8 JSONL 记录即仓库中的 `sample.jsonl`（逐字节一致；含完成、
未完成与逆序注册三种情形）：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T11:00:00"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:00:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
```

人物设定：u1 在 10:00:00 visit、11:00:00 signup（完成）；u2 仅 10:00:00 visit
（未完成）；u3 在 09:00:00 signup、10:00:00 visit（注册严格早于访问，逆序不
转化）。

依次执行：

```sh
python -m funnel import sample.jsonl --db events.sqlite
python -m funnel report --db events.sqlite --output report.json
```

以下结果为**源码推导**，未宣称已实际执行。

第一条命令成功时（`cmd_import`，`funnel/__main__.py:135-161`）：退出码 0，
标准错误为空，标准输出为一行

```json
{"imported": 5}
```

五条记录均非空且通过 `parse_line`，`len(events)` 即 5，160 行
`print(json.dumps({"imported": len(events)}))`。该文本的空格来自 `json.dumps`
默认分隔符（`", "`、`": "`），与 `README.md:118` 一致。

第二条命令成功时：退出码 0，标准错误为空，标准输出仍只有单行报告 JSON（697 行
`print(line)`，与不传 `--output` 完全相同）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

数值来源（查询在 `funnel/__main__.py:584-591`，比例在 666 行）：

- `visit_users = 3`：u1、u2、u3 各有一条 10:00:00 的 `visit`，
  `COUNT(DISTINCT user_id)` 去重后为 3。
- `converted_users = 1`：只有 u1 满足自连接条件 `s.timestamp > v.timestamp`
  （`_converted_sql`，`funnel/__main__.py:206-225`）；u2 无 signup；u3 的
  signup 09:00:00 严格早于 visit 10:00:00，不转化。
- `conversion_rate = 0.3333333333333333`：666 行 `/` 真除法，`1 / 3` 的双精度
  浮点值由 `json.dumps` 原样序列化。

## 4. 成功保存的文件语义

`report.json` 的形态由 `_write_report_file`（`funnel/__main__.py:325-357`）与
调用处 693 行共同决定：

1. **UTF-8 编码。** 344 行 `tmp.write(line.encode("utf-8"))`，以二进制写入，不
   依赖系统区域代码页。
2. **一个完整 JSON 对象，以恰好一个 LF 结束。** 693 行传入的是 `line + "\n"`：
   `line` 本身是 `json.dumps(payload)` 的单行输出（不含换行），文件尾部有且仅有
   一个 `\n`，不是两个。
3. **解析后与标准输出相同，字节也相同。** 文件内容 = `line + "\n"`；标准输出为
   697 行 `print(line)` = `line + "\n"`。同一份 `line`（686 行序列化一次），故
   文件原始字节与标准输出字节一致，JSON 解析后的对象必然相等。导出不追加导出
   时间、路径或其他字段——写入的就是 `payload` 本身。
4. **目标不存在时创建。** 347 行 `os.replace(tmp_path, output_path)` 在目标缺失
   时即把同目录临时文件重命名为目标。
5. **已有普通文件整体覆盖，不追加。** 目标只在 347 行的原子替换那一刻被触碰：
   新内容先完整落在临时文件，替换后旧字节整体消失，不存在"截断后写一半"或向旧
   报告追加的窗口。重复执行同一命令得到一份全新报告。
6. **相对路径按当前工作目录解释。** 303 行 `os.path.abspath(output_path)` 只补全
   当前 cwd，不规范化 `..` 或符号链接；335 行临时文件也建在
   `os.path.dirname(os.path.abspath(output_path))` 即目标所在目录——同目录才能
   保证 `os.replace` 在同一文件系统上原子生效。
7. **父目录由使用者事先准备。** 实现中只有 314 行 `os.path.isdir(parent)` 检查，
   全文没有任何 `makedirs`/`mkdir` 调用；父目录缺失属于第 5 节的错误，程序不会
   代为创建。

以上每一点都有 `tests/test_report_output.py` 的对应用例：文件等于标准输出且仅
一个 LF 结尾见 `test_output_file_equals_stdout_and_ends_with_single_lf`；父目录
缺失先拒绝、补齐后正常创建见 `test_output_creates_missing_file_in_existing_parent`；
覆盖不追加见 `test_output_overwrites_existing_file_without_appending`；相对路径
按 cwd 解释见 `test_relative_path_resolved_from_cwd`。空报告（零访问）同样可保存，
零值沿用 `conversion_rate` 的整数 `0` 规则（666 行 `else` 分支），见
`test_empty_report_is_saved_with_zero_rules`。

## 5. 导出错误边界：均在数据库访问前拒绝，退出码 2、标准输出为空

下列四类情形的共同口径：退出码 2、标准输出为空、标准错误指出相关参数或路径及
原因，且都发生在 572 行数据库存在性检查与 577 行 `sqlite3.connect` **之前**——
不连接、不创建数据库，不创建或改写输出文件，也不改变事件记录。统一前提由调用
次序保证：`_prepare_output`（567–570 行）排在所有数据库访问之前。

### 5.1 `--output` 缺值或取空字符串

- **缺值**（命令行上 `--output` 后没有参数）：argparse 在
  `funnel/__main__.py:802` 的 `parse_args` 内直接报错退出，标准错误自带
  `--output` 参数名前缀（"expected one argument"类文本），退出码 2，不进入
  `cmd_report`。
- **空字符串**（`--output ""`）：argparse 调用 `parse_output_path`，该函数不做
  空白裁剪，277 行以精确相等判定 `text == ""`——只有恰好空字符串才在 278–280 行
  抛 `ArgumentTypeError("必须给出非空的本地文件路径，得到空字符串")`，argparse
  据此退出码 2，标准错误带 `--output` 参数名前缀。当前实现的拒绝边界就是精确空
  字符串，本说明不就此以外的取值下结论。

两种情况下标准输出均为空，且即便 `--db` 指向一个不存在的数据库，该数据库文件也
不会被创建。依据：`tests/test_report_output.py` 的
`test_missing_value_rejected_before_db_access` 与
`test_empty_string_value_rejected_before_db_access`（两者都断言退出码 2、标准
输出为空、标准错误含 `--output`，且 `ghost.sqlite` 不存在）。

### 5.2 规范化后与 `--db` 指向同一路径

`_prepare_output` 在 303–304 行分别对两个路径做 `os.path.normcase(os.path.abspath(...))`
（补全 cwd、按当前系统归一大小写），305 行比较相等即拒绝。标准错误为 306–310 行
的固定文本：

```text
--output 与 --db 不能指向同一路径：两者规范化后均为 <归一化绝对路径>
```

信息同时指出 `--output` 与 `--db` 两个参数及归一化后的路径。`./events.sqlite`
与 `events.sqlite` 这类不同拼写在归一后相等，同样被拒；`--db` 与 `--output`
指向同一个尚不存在的路径时，拒绝先于数据库访问，该文件不会被创建。依据：
`test_output_same_path_as_db_rejected_with_both_params`、
`test_output_same_path_as_db_different_spellings`、
`test_output_same_path_rejected_before_missing_db_is_created`。

### 5.3 父目录不存在

313–315 行取输出路径的父目录，`os.path.isdir(parent)` 为假即经 `_output_error`
返回 2，标准错误为

```text
<输出路径原样回显>: 父目录不存在: <父目录绝对路径>
```

包含输出路径、缺失父目录与原因；程序不创建该目录，也不创建目标文件。依据：
`test_missing_parent_reports_path_and_reason`，以及
`test_output_creates_missing_file_in_existing_parent` 的前半段（缺失父目录先
拒绝、目录本身未被创建）。

### 5.4 目标已存在且是目录

316–317 行 `os.path.isdir(output_abs)` 为真即经 `_output_error` 返回 2，标准
错误为

```text
<输出路径>: 目标已存在且是目录，不是普通文件
```

避免把目录当作报告文件覆盖。依据：`test_target_is_directory_rejected`。

此外，320–321 行还覆盖一个同源边界：目标已存在、既不是目录也不是普通文件（如
FIFO、设备文件；指向目录的符号链接已被 316 行的 `isdir` 提前拦截）时，标准错误
为"目标已存在但不是普通文件"，同样退出 2 且先于数据库访问。现有测试未为该分支
单列用例，本说明只指出源码当前覆盖的这一事实，不扩大结论。

四类失败发生时数据库事件记录保持不变，依据 `test_failures_keep_events_unchanged`
（同路径、父目录缺失、目标为目录三种场景前后 `events` 表行数一致）。

## 6. 保存阶段 `OSError` 的保护

路径类问题已在第 5 节（数据库访问前）拦截；进入 `_write_report_file` 后，剩下
的失败面是写临时文件或原子替换时的 `OSError`（如磁盘错误、I/O 错误）。337–357
行的处理给出以下保证：

1. **退出码 2、标准输出为空、无 Traceback。** 348–356 行捕获 `OSError` 后经
   `_output_error` 返回 2，标准错误为
   `"<输出路径>: 报告文件无法写入: <strerror 或异常>"`，包含输出路径与原因；
   调用处 693–695 行收到非 `None` 立即 `return`，697 行的 `print` 不执行，故
   标准输出为空。
2. **已有普通文件保留原字节。** 目标文件只可能被 347 行 `os.replace` 触碰；替换
   失败（或此前写临时文件失败）时旧文件从头到尾未被打开或截断，字节与保存前完全
   一致。
3. **原本不存在的目标不留下不完整文件。** 内容只写入 340–342 行创建的同目录临时
   文件（前缀 `.funnel-report-`）；异常处理在 349–353 行 `os.remove(tmp_path)`
   清理临时文件（清理本身再失败也被忽略），目标路径从未被创建，输出目录不残留
   本次保存的中间文件。
4. **数据库记录不变。** 保存阶段在查询关闭连接之后（连接在 660–661 行
   `finally` 中关闭），且报告链路本身只有 `SELECT`；失败不回写任何事件。
5. **恢复后可正常导出。** 失败原因消除后，以同一数据库与同一目标重新执行，走
   完整的成功路径：缺失文件正常创建，已有旧文件被新报告整体覆盖。

测试不依赖目录权限或管理员身份：`tests/test_report_output.py` 用
`unittest.mock` 让 `os.replace` 确定抛出 `OSError(EIO, "simulated save
failure")`，恰好命中 347 行这一替换点。已有目标字节不变、临时文件不残留、记录
不变、恢复后成功覆盖，见
`test_save_failure_preserves_existing_file_bytes`；目标原本不存在时失败不留文件、
目录为空、恢复后成功创建，见
`test_save_failure_with_missing_target_leaves_no_file`。两个用例都用
`replace_mock.assert_called_once()` 与调用参数确认失败确实发生在保存阶段。

## 7. 校验或查询失败不触碰输出文件；开关与不传时的既有行为

- **参数校验失败不改已有输出文件。** 时间段配对等校验（522–541 行）先于
  567–570 行的输出路径处理，更先于文件写入。
  `test_param_validation_failure_does_not_touch_existing_file` 先成功导出一份
  `report.json`，再以"只给 `--visit-from`、缺 `--visit-before`"触发退出码 2，
  断言文件字节与失败前逐字节相同。
- **报告查询失败不创建输出文件。** 查询抛错从 662–664 行返回，686 行的序列化与
  693 行的写文件均不可达。`test_query_failure_does_not_create_output` 用损坏的
  SQLite 文件触发查询失败，断言退出码 2、标准输出为空、标准错误含数据库路径，
  且目标 `never.json` 始终不存在。
- **导出不添加字段、不改变事件记录。** 666–682 行 `payload` 的字段完全由既有
  开关决定（`--within-seconds`、`--visit-from/--visit-before`、`--include-users`、
  `--include-pairs`、`--include-latency`、`--group-by visit-date`、
  `--include-group-pairs`、`--include-group-latency`），与是否传 `--output`
  无关；文件只是持久化同一个 `payload`。全部明细与筛选开关合用、文件解析结果与
  标准输出相等，见 `test_output_with_all_detail_flags_matches_stdout`。报告对
  数据库仅执行 `CREATE TABLE IF NOT EXISTS` 与 `SELECT`（579–659 行）。
- **不传 `--output` 时维持现有输出。** `args.output is None` 时 688 行条件不
  成立，不做任何文件操作，697 行 `print(line)` 的行为与该参数引入前一致：退出码
  0、单行三键报告、标准错误为空。依据
  `test_without_output_behavior_unchanged`。
- **`import` 子命令不接受 `--output`。** import 子解析器（708–711 行）未注册该
  参数，argparse 以"无法识别的参数"退出码 2，且不会产生任何输出文件，依据
  `test_import_does_not_accept_output`。

## 8. 与 README、源码的一致性

- 命令 `python -m funnel report --db events.sqlite --output report.json` 与
  `README.md:70-74` 一致；`--output` 的帮助文本（UTF-8、单对象换行结束、相对
  路径按 cwd、不存在则创建、普通文件整体覆盖、父目录须事先存在、不得与 `--db`
  同路径）对应 `funnel/__main__.py:790-799`。
- 成功行为（退出码 0、标准错误为空、标准输出仍只有单行报告、文件解析后与标准
  输出一致）与 `README.md:76`、`README.md:92-95` 相符。
- 错误行为（缺值/空字符串、与 `--db` 同路径、父目录不存在、目标是目录、保存
  失败）的退出码、空标准输出、标准错误内容、先于数据库访问、失败不改文件与记录
  等口径，与 `README.md:78`、`README.md:95` 相符。
- 第 3 节五条事件与 `sample.jsonl` 逐字节一致，导入与报告预期与
  `README.md:115-132` 的样例一致；空报告可保存、重复执行整体覆盖与
  `README.md:134` 一致。

本次交付仅新增本说明文档；产品源码、README、JSONL 导入格式与既有统计行为均保持
现状，未新增或修改任何功能。全部行为结论以当前 `funnel/__main__.py` 源码与
`tests/test_report_output.py` 为依据，数值与文本均为源码推导，未宣称已实际执行。
