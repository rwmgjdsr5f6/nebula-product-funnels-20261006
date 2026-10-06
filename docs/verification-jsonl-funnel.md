# 合成 JSONL 导入 → visit/signup 两步漏斗：源码行为核对说明

核对日期：2026-10-06
核对基线：仓库当前工作树。本说明全部结论均从 `funnel/__main__.py` 的源码逐行推导，**未实际执行任何命令**；文中所有数值均为源码推导结果，而非运行记录。程序、README、公开命令与数据格式以仓库现状为准，本说明不引入任何新功能。

## 1. 核对依据与范围

本说明只依据仓库中的实际文件：

| 文件 | 角色 |
|---|---|
| `funnel/__main__.py` | 唯一源码文件：CLI 入口、JSONL 解析校验、SQLite 写入、漏斗统计 |
| `funnel/__init__.py` | 包标记 |
| `README.md` | 公开命令、事件格式、输出与退出码、统计规则的说明 |
| `sample.jsonl` | 2026-10-06 的固定合成样例（5 行，见第 4 节） |
| `tests/` | 公开行为回归测试（导入原子性、UTF-8、报告窗口语义） |

公开入口为两条命令（`funnel/__main__.py:205-229` 的 `main()` 注册）：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> [--within-seconds N]
```

## 2. 流程逐环节核对

| 环节 | 状态 | 源码位置 |
|---|---|---|
| 公开入口（import / report 子命令） | 已实现 | `main()`（`funnel/__main__.py:205`），`argparse` 子命令 |
| 输入解析（JSONL 逐物理行读取、严格 UTF-8） | 已实现 | `load_events()`（`funnel/__main__.py:81`） |
| 单行校验（JSON、必填字段、类型、取值、时间格式） | 已实现 | `parse_line()`（`funnel/__main__.py:40`） |
| 本地保存（SQLite 追加、整批事务） | 已实现 | `cmd_import()`（`funnel/__main__.py:105`），`SCHEMA`（`funnel/__main__.py:22-28`） |
| 用户去重与两步人数计算 | 已实现 | `cmd_report()` 中的 SQL（`funnel/__main__.py:155-185`） |
| 报告输出（单行 JSON、退出码） | 已实现 | `cmd_report()`（`funnel/__main__.py:192-202`） |

各环节衔接：`import` 先由 `load_events()` 读完全部行并逐行交给 `parse_line()` 校验，**全部通过后才连接数据库**（`funnel/__main__.py:106-116`），在单个事务里建表（`CREATE TABLE IF NOT EXISTS`）并 `executemany` 追加全部记录（`funnel/__main__.py:118-123`），成功时标准输出仅 `{"imported": N}`。`report` 对已入库的 `events` 表执行两条 `COUNT(DISTINCT user_id)` 查询，组装单行 JSON 输出。导入只追加、不清理；报告只查询、不写入事件行。

## 3. 事件格式与校验规则（`parse_line()`）

每个非空行必须是一个 JSON 对象，且：

- `user_id`：非空字符串，按原值区分用户（`funnel/__main__.py:49-53`）
- `event`：仅接受 `visit` 或 `signup`（`funnel/__main__.py:55-61`，`VALID_EVENTS`）
- `timestamp`：匹配 `YYYY-MM-DDTHH:MM:SS` 且为有效时间，不接受时区后缀或小数秒（`funnel/__main__.py:63-76`，`TIMESTAMP_RE` 与 `datetime.strptime` 双重检查）

额外字段被忽略（`parse_line` 只取三个字段）。`load_events()` 以二进制逐物理行读取并严格按 UTF-8 解码：任一行字节不是合法 UTF-8 即报错；空白行（含纯空白）被忽略但物理行号照常递增（`funnel/__main__.py:93-101`）；LF 与 CRLF 一致处理，末行无换行符仍计入。

## 4. 固定样例与预期结果（源码推导）

### 4.1 样例输入

`sample.jsonl` 的 5 条完整 UTF-8 JSONL 记录，日期固定为 2026-10-06：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T11:00:00"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:00:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
```

即：u1 在 10:00:00 visit、11:00:00 signup；u2 仅在 10:00:00 visit；u3 在 09:00:00 signup、10:00:00 visit。

### 4.2 导入

```sh
python -m funnel import sample.jsonl --db events.sqlite
```

5 行均通过 `parse_line()` 校验，整批追加进 `events` 表。预期（源码推导）：退出码 0，标准错误为空，标准输出仅一行：

```json
{"imported": 5}
```

### 4.3 报告（不限窗口）

```sh
python -m funnel report --db events.sqlite
```

预期（源码推导）：退出码 0，标准错误为空，标准输出仅一行：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

### 4.4 分母与分子的逐人来源

**分母 `visit_users`** 来自 `SELECT COUNT(DISTINCT user_id) ... WHERE event = 'visit'`（`funnel/__main__.py:155-157`）：

- u1 有一次 visit → 计入
- u2 有一次 visit → 计入
- u3 有一次 visit → 计入

合计 **3**。

**分子 `converted_users`** 来自 `events v JOIN events s`，条件为同用户、`s.event = 'signup'` 且 `s.timestamp > v.timestamp`（严格晚于，`funnel/__main__.py:159-169`）：

- u1：signup（11:00:00）严格晚于 visit（10:00:00）→ **转化**
- u2：没有任何 signup 行，JOIN 无结果 → 不转化
- u3：signup（09:00:00）早于 visit（10:00:00），不满足 `>` → 不转化

合计 **1**。

**比例** `conversion_rate = converted_users / visit_users = 1 / 3`（`funnel/__main__.py:192`）。Python 浮点 `1/3` 经 `json.dumps` 序列化为 `0.3333333333333333`。

## 5. `--within-seconds` 窗口语义

传入窗口时，JOIN 追加条件：两事件 epoch 秒之差 `<= N`（`funnel/__main__.py:171-185`，`strftime('%s', ...)` 转整数后比较）。`--within-seconds` 只接受全为 ASCII 数字且大于零的整数，允许前导零（`parse_within_seconds()`，`funnel/__main__.py:134-143`）。

对同一样例（u1 的 visit→signup 间隔恰好 3600 秒）：

- `--within-seconds 60`：u1 间隔 3600 秒 > 60，不满足；u2、u3 本就不转化。预期（源码推导）`{"visit_users": 3, "converted_users": 0, "conversion_rate": 0}`——分母不受窗口影响，仍为 3；分子为 0 时比例为整数 `0`。
- `--within-seconds 3600`：u1 间隔恰好等于窗口上限。源码比较运算符是 `<=`（`funnel/__main__.py:180-181`），**恰好等于 N 秒的转化计入**，因此恢复为 `{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}`。

窗口参数只影响本次报告的查询条件，不改写数据库中的任何记录。

## 6. 统计规则细则（均有源码对应）

- **严格晚于**：signup 时刻必须严格大于 visit 时刻（`s.timestamp > v.timestamp`，`funnel/__main__.py:166` 与 `:179`）；同一时刻不算转化。
- **任意一对即可**：JOIN 对同一用户的所有 (visit, signup) 组合逐一匹配，多次访问中任意一次满足条件即计入，且 `COUNT(DISTINCT v.user_id)` 保证该用户只算一次（`funnel/__main__.py:161`、`:174`）。
- **按用户原值去重**：两条 SQL 均为 `COUNT(DISTINCT user_id)`，`user_id` 按导入时的原始字符串区分，不做归一化。
- **行序无关、重复不增人数**：统计基于集合去重计数，与物理行序无关；重复事件行、重复导入同一文件只会追加重复行（导入无去重，`funnel/__main__.py:120-123`），但 `DISTINCT` 使用户人数与比例不变。
- **零访问**：`visit_users` 为零时分子必为零（无 visit 行则 JOIN 为空），比例走 `if visit_users else 0` 分支得到整数 `0`（`funnel/__main__.py:192`），三项均为 0。

## 7. 导入失败：首错定位与原子性

### 7.1 首错定位示例

设输入文件 `bad.jsonl` 内容为：第 1 行合法记录、第 2 行空白、第 3 行只有一个 `{`。`load_events()` 按物理行顺序处理：第 1 行通过校验，第 2 行空白被忽略但占行号，第 3 行在 `parse_line()` 的 `json.loads` 处失败，抛出携带行号 3 的 `LineError`（`funnel/__main__.py:44-45`）。`cmd_import()` 捕获后按 `"%s: 第 %d 行: %s"` 格式写标准错误（`funnel/__main__.py:108-110`）。

预期（源码推导）：

- 退出码 **2**
- 标准输出为**空**
- 标准错误包含输入路径、物理行号与非法 JSON 原因，形如：
  `bad.jsonl: 第 3 行: 非法 JSON: ...`
- 只报告首个错误：不会继续报告后续行，也不输出异常堆栈。

### 7.2 合法前缀不留存、不创建新库

`load_events()` 任一行失败即抛异常、不返回部分结果（`funnel/__main__.py:82-88`），而数据库连接发生在校验全部通过之后（`funnel/__main__.py:106-116`）。因此：

- 第 1 行的合法记录**不会**被保存——整批不写入；
- 若 `--db` 指向的数据库原本不存在，失败的导入**不会**创建该文件（根本未执行 `sqlite3.connect`）；
- 若数据库已存在，其中已有记录保持不变。

写入阶段的另一层保障：所有 INSERT 在单个 `with conn:` 事务中执行，任一条失败即整体回滚（`funnel/__main__.py:118-123`）。

## 8. 报告失败：数据库不存在或无法访问

`cmd_report()` 先用 `os.path.exists` 检查路径（`funnel/__main__.py:147-149`），再尝试连接并查询（`funnel/__main__.py:151-190`）。两种失败均满足：

- 退出码 **2**
- 标准输出为**空**
- 标准错误包含数据库路径与原因：不存在时为 `<db>: 数据库不存在`；连接或查询抛出 `sqlite3.Error` 时为 `<db>: 数据库无法访问: <原因>`（`funnel/__main__.py:188-190`）。

## 9. 报告的只读性

正常报告对已有事件记录零改动：`cmd_report()` 对 `events` 表只执行 `SELECT` 查询；唯一的写类语句是 `CREATE TABLE IF NOT EXISTS`（`funnel/__main__.py:154`），对已存在的表是空操作，不增删改任何事件行。`--within-seconds` 仅作为查询参数传入，不落库。回归测试 `tests/test_report_window.py` 中的 `test_report_keeps_events_unchanged` 以失败前后逐行快照比对固定了这一行为。

## 10. 验收对照

- 本说明每一环节均指向 `funnel/__main__.py` 的具体函数与行号，未引用不存在的源码。
- 所有数值（`imported=5`、3/1/0.3333333333333333、窗口 60 与 3600 的结果、退出码与输出通道）均为源码推导，未宣称已经执行。
- 早期版本中"功能未实现"的判断已随源码落地而失效，本说明已整体替换为与当前源码逐项核对的流程解释。
- 本次交付仅修改本说明文档，未改动程序、README、公开命令、数据格式或任何其他文件。
