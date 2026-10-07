# 组内转化配对明细（--group-by visit-date --include-group-pairs）：源码行为核对说明

核对日期：2026-10-07
核对基线：当前工作树，HEAD `e315640`（`e3156404d7b30016cacfa51ef3dd30485d44f35f`），
撰写前工作树干净；本说明是在该基线之上新增的唯一改动，撰写时尚未提交。

## 1. 核对依据与范围

本说明覆盖**从 JSONL 导入到按访问日期分组的组内 `conversion_pairs`** 这一条
完整统计链路，命令形态为：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite> --group-by visit-date
       --include-group-pairs [--include-pairs] [--within-seconds N]
       [--visit-from YYYY-MM-DDTHH:MM:SS --visit-before YYYY-MM-DDTHH:MM:SS]
```

目的是让复核者能用一份六条事件的小型合成输入，逐人核对"谁归哪个日期组、
组内配对为什么是这一组时间戳、组内数组与顶层配对如何勾稽"。结论全部指向
当前仓库的真实文件、函数与判断条件。**本次只交付本说明**：命令入口、
SQLite 表结构、导入的追加协议（无去重、整批事务追加）、报告不改写事件记录
的只读行为，以及所有现有输出字段，均保持现状。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、归组、配对归约与输出 |
| `tests/test_report_include_group_pairs.py` | 组内配对开关的既有回归测试（断言，非实测，见下） |
| `tests/test_group_by_visit_date.py` | 分组报告本身的既有回归测试（断言，非实测） |
| `docs/verification-visit-date.md` | 按访问日期分组的核对说明（姊妹篇） |
| `docs/verification-conversion-pairs.md` | 顶层配对明细的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `TIMESTAMP_RE` / `VALID_EVENTS` | 21 / 26 | 导入侧时间格式与合法事件（`visit`、`signup`） |
| `parse_line` / `load_events` | 54–104 / 107–128 | 单行校验与整文件严格 UTF-8 校验 |
| `cmd_import` | 131–157 | `import` 子命令：先整文件校验，再事务追加 |
| `parse_within_seconds` | 160–179 | `--within-seconds` 取值校验（正整数，上界钳制） |
| `_visit_window_clause` | 182–186 | 段内 visit 过滤片段 `AND v.timestamp >= ? AND v.timestamp < ?` |
| `_converted_sql` / `_converted_params` | 189–208 / 211–217 | 有效配对枚举 SQL 与参数装配（汇总、顶层配对、组内转化共用） |
| `parse_group_by` / `parse_visit_bound` | 220–230 / 233–250 | `--group-by` 与时段端点取值校验 |
| `_qualifying_visit_date_by_user` | 253–269 | 归组查询：每人最早一次合格 visit 的 UTC 日期 |
| `_reduce_pairs` | 272–287 | 配对归约：每人先取最早有效 signup，再取最晚可配对 visit |
| `_pairs_payload` | 290–306 | 由归约结果装配 `conversion_pairs` 数组（Python 侧按码点排序） |
| `_build_visit_date_groups` | 309–355 | 日期组唯一装配点；组内配对在 350–353 追加 |
| `cmd_report` | 358–495 | `report` 子命令：参数检查、查询、归组、JSON 输出 |
| `cmd_report` 依赖检查（组内配对开关） | 385–391 | 单独使用 `--include-group-pairs` 的退出码 2 分支 |
| `cmd_report` 配对归约块 | 436–448 | 归约只执行一次，顶层与各组共用 |
| `cmd_report` 分组块 | 449–474 | 归组查询、转化集合、组对象装配（473 传入配对子集） |
| `main` 中三个开关定义 | 533–565 | `--include-users` / `--include-pairs` / `--include-group-pairs` / `--group-by` |

**数值来源声明（三类，严格区分）：**

- 标 **【实测】** 的命令输出、退出码与标准错误文本，是 2026-10-07 在本机
  （Linux/WSL2，CPython 3.14.4）以当前工作树源码实际执行的观测结果：
  `python3 -m funnel` 在项目根目录解析到本仓库的 `funnel` 包，样例 JSONL 与
  SQLite 放在临时目录，与"在项目根目录执行下文命令"等价。
- 标 **【源码推导】** 的结论，依据为 `funnel/__main__.py` 当前源码（含其
  SQL 与 Python 表达式）及 CPython 标准库 `json`/`sqlite3` 的行为。
- `tests/*.py` 中的用例是**既有的回归断言**，本文仅把具名测试当作"该行为
  在源码层面被固定在何处"的交叉索引引用，**不把断言文本当作实测结果**；
  本文所有【实测】均来自我本人执行公开命令的观测。若实际观察与本文推导
  冲突，以实际观察为准并据此修正本文。

## 2. 从 JSONL 到组内 `conversion_pairs` 的完整链路

每一步均可直接在源码中核对：

1. **整文件校验后事务追加。** `cmd_import`（`:131-157`）先调
   `load_events`（`:107-128`）按物理行逐行严格 UTF-8 解码并由 `parse_line`
   校验（`:54-104`：非空字符串 `user_id`、事件仅限 `visit`/`signup`、
   时间戳为不含时区与小数秒的 `YYYY-MM-DDTHH:MM:SS` 且日历有效）；任一行
   非法即报即停、不写任何行。全部合法后在一个事务里 `executemany` 纯
   `INSERT`（`:144-149`）——**追加协议不去重**，重复导入会追加相同行。
   成功后打印 `{"imported": N}`（`:156`）。
2. **报告先做参数检查，再碰数据库。** `cmd_report`（`:358`）依次检查时段
   端点成对且严格先后（`:361-380`）、`--include-group-pairs` 与
   `--group-by visit-date` 的依赖关系（`:385-391`），然后才做
   `os.path.exists`（`:393-395`）与 `sqlite3.connect`（`:398`）。连接后只
   执行 `CREATE TABLE IF NOT EXISTS` 与 `SELECT`，报告不改写任何事件行。
3. **一次性确定"合格访问"条件。** `visit_where`（`:401-409`）无时段时为
   `WHERE event = 'visit'`（全库）；有时段时追加
   `timestamp >= ? AND timestamp < ?`——**含起点、不含终点**。同一条件
   同时用于访问人数（`:411-414`）、编号明细、归组查询与配对枚举的 visit
   一侧，口径完全相同。
4. **枚举全部有效 (visit, signup) 行对。** `_converted_sql`（`:189-208`）
   把 `events` 自连接，条件为：
   - `s.event = 'signup'` 且 **`s.timestamp > v.timestamp`（严格大于，
     `:198`）**——同刻与逆序都不成立；
   - 提供 `--within-seconds N` 时追加秒差 **`<= N`（`:203-205`，上界含
     等值）**；
   - visit 一侧套用上一步的时段片段（`:207`）——**段外 visit 连行对都枚举
     不到**；signup 一侧没有时段条件，**注册允许晚于段终点**。
5. **每人归约成一组时间戳。** `_reduce_pairs`（`:272-287`）遍历行对：
   `signup_ts < current[0]` 时整组替换（`:283-284`），先锁定**最早的有效
   注册**；注册时刻相等而 `visit_ts > current[1]` 时只更新访问
   （`:285-286`），即在能匹配该注册的 visit 中取**最晚的一次**；更晚的
   signup 两个分支都不命中。
6. **每人独立归组。** `_qualifying_visit_date_by_user`（`:253-269`）执行
   `SELECT user_id, substr(MIN(timestamp), 1, 10) FROM events <visit_where>
   GROUP BY user_id`：先按时段过滤、再按人取**最早一次合格 visit** 的日期。
   此查询与第 4–5 步的配对枚举互不引用——**配对用的是哪天的 visit，不影响
   归组**。
7. **组内配对是同一份归约结果的子集。** 归约在 `:441-446` 只执行一次；
   `_build_visit_date_groups`（`:309-355`）先算本组访问成员与转化集合的
   交集 `converted_ids = visit_ids & converted_members`（`:338`），再在开关
   启用时调 `_pairs_payload(best_by_user, converted_ids)`（`:353`）按本组
   转化成员过滤，排序由 `sorted` 按 Unicode 码点完成（`:305`）。
8. **输出装配。** 三个汇总字段在前（`:482-486`），随后按开关追加编号数组
   （`:487-489`）、顶层 `conversion_pairs`（`:490-491`），最后在分组时
   追加 `visit_date_groups`（`:492-493`），`json.dumps` 单行打印
   （`:494`）。

## 3. 归组与配对为什么是两条独立规则

- **归组只看"最早一次合格 visit"。** `MIN(timestamp) ... GROUP BY user_id`
  （`:264-266`）决定每人只有一个日期；该用户其他日期的 visit 不产生第二个
  组，段外 visit 因 `<visit_where>` 先过滤而不参与 `MIN`。
- **配对只看"有效行对上的最早注册与最晚可配对访问"。** 行对来自独立的自
  连接（`:189-208`），归约规则在 `:283-286`；配对选取与 `MIN` 归组没有
  任何 SQL 或 Python 层面的相互约束。
- **装配阶段按成员身份过滤，而不是按配对时间重新归组。** 组内配对取的是
  "本组转化成员"在全局归约字典中的条目（`:338`、`:353`），配对对象里的
  `visit_timestamp` 可以落在另一个日期——用户仍只在其归组日期下出现一次。
  这解释了验收样例中 u1 的配对访问发生在 7 日、u1 却归 6 日组且其配对只在
  6 日组出现（见第 6 节）。

## 4. 固定合成样例：完整六条 JSONL

以下六条完整 UTF-8 JSONL 记录，保存为 `acceptance.jsonl`（字段与
`parse_line` 的校验一一对应）：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:00"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T10:00:30"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T11:00:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T12:00:00"}
{"user_id": "u3", "event": "signup", "timestamp": "2026-10-07T12:00:00"}
```

人物设定：

- **u1**：6 日 10:00:00 与 7 日 10:00:00 各访问一次，7 日 10:00:30
  注册；
- **u2**：仅在 6 日 11:00:00 访问一次，没有注册；
- **u3**：7 日 12:00:00 同一时刻访问并注册。

## 5. 一次导入

```sh
python -m funnel import acceptance.jsonl --db events.sqlite
```

**【实测】** 标准输出（一行，退出码 0，标准错误为空）：

```json
{"imported": 6}
```

六条记录全部通过 `parse_line`，`len(events)` 即 6（`:156`）。数据库文件
不存在时导入会新建它并建表（`:142-149`，表结构为三列 `user_id` /
`event` / `timestamp`，见 `SCHEMA` `:36-42`）。`json.dumps` 默认分隔符在
冒号后输出一个空格，逐字节文本即 `{"imported": 6}`；本文其余 JSON 预期
同理，按 JSON 值比较时与紧凑写法等价。

## 6. 一次报告：三个开关同开，窗口 60 秒

```sh
python -m funnel report --db events.sqlite \
    --group-by visit-date --include-group-pairs --include-pairs \
    --within-seconds 60
```

**【实测】** 完整标准输出（一行，退出码 0，标准错误为空）：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333, "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-07T10:00:00", "signup_timestamp": "2026-10-07T10:00:30"}], "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 1, "conversion_rate": 0.5, "conversion_pairs": [{"user_id": "u1", "visit_timestamp": "2026-10-07T10:00:00", "signup_timestamp": "2026-10-07T10:00:30"}]}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0, "conversion_pairs": []}]}
```

字段顺序即 `payload` 装配顺序（`:482-493`）：三个汇总字段、顶层
`conversion_pairs`（`--include-pairs` 追加）、`visit_date_groups`；每个组
对象在四个基础字段后追加组内 `conversion_pairs`（`:339-354`）。

### 6.1 逐人复核

- **u1：转化；归 6 日组；配对是 7 日 10:00:00 访问 + 10:00:30 注册。**
  归组侧：两次合格 visit 经 `MIN`（`:264`）取到 `2026-10-06T10:00:00`，
  日期为 `2026-10-06`，故 u1 归 **6 日组**。配对侧：自连接枚举两行候选——
  (6 日 10:00:00, 7 日 10:00:30) 的秒差为 86430，`86430 <= 60` 不成立，
  在 `:203-205` 被排除；(7 日 10:00:00, 7 日 10:00:30) 严格晚于
  （`:198`）且秒差恰为 30、`30 <= 60` 成立，是唯一有效行对。归约
  （`:283-286`）自然得到最早注册 10:00:30、可配对的最晚（也是唯一）访问
  7 日 10:00:00。**配对访问发生在 7 日不会把 u1 改归 7 日组**：第 3 节
  说明的两条独立规则在此直接可见——u1 的编号只出现在 6 日组，7 日组没有
  u1；同一条配对对象挂在 6 日组下，其 `visit_timestamp` 仍如实写 7 日。
- **u2：不转化；在 6 日组计访问。** 全库没有 u2 的 `signup` 行，自连接
  枚举不到任何行对，`best_by_user` 中无 u2，组内转化交集
  （`:338`）不含 u2。
- **u3：不转化；在 7 日组计访问。** visit 与 signup 同为
  `2026-10-07T12:00:00`，配对条件是**严格**大于（`:198`），相等不成立；
  秒差为 0，即便窗口放宽也一样被排除。自连接无行对，故 7 日组
  `converted_users = 0`、`conversion_pairs = []`（空数组而非缺键，
  `:350-353` 对无转化组同样追加）。

### 6.2 汇总与两组数值

- 顶层：访问三人、转化一人，`1 / 3` 真除法经 `json.dumps` 序列化为
  `0.3333333333333333`（`:481`）。
- 6 日组：u1、u2 共 2 人访问，u1 一人转化，`1 / 2 = 0.5`。
- 7 日组：u3 一人访问，0 人转化；`0 / 1` 走真除法分支，JSON 文本是
  **`0.0`** 而非整数 `0`（整数 `0` 只在零访问的顶层 `else` 分支出现，
  `:481`）。
- 人数勾稽：`2 + 1 = 3`、`1 + 0 = 1`；日期按定宽文本升序，只列有合格
  访问用户的日期（`:336`）。

### 6.3 组内数组与顶层配对的勾稽

以下四条均可由源码推出，并已用第 6 节及多用户样例实测核对：

1. **组内数组长度等于本组转化人数。** `_pairs_payload` 对
   `converted_ids` 中每个编号产出恰好一个对象（`:298-306`），而
   `converted_users` 就是同一集合的 `len`（`:342`）；转化集合本就是有效
   行对用户的集合，与归约字典的键同源，故每个编号都能在 `best_by_user`
   中取到条目。本例：6 日组长度 1 = 转化 1 人；7 日组长度 0。
2. **各组合并后按用户编号的 Unicode 码点顺序排列，与顶层数组逐元素
   一致。** 顶层与组内都调同一个 `_pairs_payload`，排序都是 Python
   `sorted` 对 `str` 的比较，即 Unicode 码点字典序（`:305`；区分大小写、
   保留空白与中文、不按数字大小）。因每用户只归一组，把各组配对拼接后再
   按 `user_id` 排序必然等于顶层 `conversion_pairs`。
   **【实测】** 用另一个多用户样例（6 日组 `u10`、`u2` 转化，另有仅访问
   的 `" 空格"`；7 日组 `张三` 转化）核对：顶层输出顺序为
   `["u10", "u2", "张三"]`（`"u10" < "u2"` 因第二位 `'1'(U+0031) <
   '2'(U+0032)`），与两组合并排序结果完全相同；`张三` 这类非 ASCII 编号
   在标准输出中以 `json.dumps` 默认的 `反斜杠 + uXXXX` 转义呈现
   （`张` 为 U+5F20、`三` 为 U+4E09，即六个 ASCII 字符 `u5f20`、`u4e09`
   前加反斜杠），按 JSON 值解析后仍是原编号。
3. **组间没有重复用户。** 归组查询 `GROUP BY user_id` 每人一行
   （`:264-267`），`date_by_user` 每用户唯一日期；u1 虽在 7 日也有
   visit，其配对只在 6 日组出现一次。
4. **无转化组给空数组、不缺键。** 7 日组 `conversion_pairs` 为 `[]`；
   没有任何合格 visit 时整个 `visit_date_groups` 为 `[]`（`date_by_user`
   为空，`:331-336`）。

## 7. 开关组合：追加规则与独立性

以下均为 **【实测】**（同一 `events.sqlite`，窗口 60 秒）：

1. **未开启 `--include-group-pairs` 时组内不追加该字段。** 即使同开
   `--include-pairs`，组对象也只有基础四键：

   ```sh
   python -m funnel report --db events.sqlite --group-by visit-date \
       --include-pairs --within-seconds 60
   ```

   输出中顶层有 `conversion_pairs`，但两个组对象均只有 `visit_date`、
   `visit_users`、`converted_users`、`conversion_rate` 四个键
   （分支在 `:350`，`pairs_by_user is None` 时不追加）。对应既有断言
   `test_without_flag_groups_have_no_pair_field`。
2. **`--include-group-pairs` 不自动开启顶层配对或编号明细。**

   ```sh
   python -m funnel report --db events.sqlite --group-by visit-date \
       --include-group-pairs --within-seconds 60
   ```

   实测顶层恰为四个键（三个汇总字段加 `visit_date_groups`），没有
   `conversion_pairs`、没有 `visit_user_ids`/`converted_user_ids`；每个组
   对象恰为五个键（基础四键加组内 `conversion_pairs`）。归约虽然在
   `:436-446` 因组内开关而执行，顶层字段只在 `args.include_pairs` 为真时
   装配（`:490-491`），编号数组只在 `args.include_users` 为真时装配
   （`:487-489`、`:345-349`）。对应既有断言
   `test_group_pairs_do_not_auto_enable_other_details`。
3. **三个开关可任意叠加且互不改变数值。** 例如再加 `--include-users`，
   实测组对象变为"基础四键 + 两个编号数组 + `conversion_pairs`"的七键
   顺序（编号数组在配对之前，`:345-353`），所有人数、比例、配对时间戳与
   第 6 节完全一致——开关只追加字段，不改变查询条件。对应既有断言
   `test_metrics_identical_with_and_without_flag`。

## 8. 边界语义与不变性（实测）

1. **转化窗口包含上界。** 同一库分别报告：`--within-seconds 30` 时 u1 的
   配对秒差恰为 30，`30 <= 30` 成立（`:203-205`），输出与第 6 节相同；
   **【实测】** `--within-seconds 29` 时输出为访问 3 人、转化 0 人、比例
   `0.0`，顶层与两个组的 `conversion_pairs` 全部为 `[]`。严格大于条件
   始终同时生效，秒差 0 不可能转化（u3 即此情形）。
2. **访问时段含起点、不含终点；段外访问不参与归组或配对。**
   **【实测】** 对一个含 `u1 visit 2026-10-06T23:59:00`、
   `u1 signup 2026-10-08T00:00:00`、`u2 visit 2026-10-07T00:00:00` 的库，
   以 `--visit-from 2026-10-06T00:00:00 --visit-before 2026-10-07T00:00:00`
   报告：u2 的 visit 恰在终点上，被 `<` 排除，访问只有 1 人；u1 的 visit
   恰在起点上（或把起点收紧到 `23:59:00`）被 `>=` 保留。段外 visit 同时
   从归组 `MIN`（经 `:265` 拼入的 `visit_where`）与配对枚举 visit 一侧
   （`:207` 的 `_visit_window_clause`）消失。
3. **注册允许晚于时段终点。** 上例 u1 的 signup 晚于终点整整一天，实测仍
   计入转化，组内配对为 `2026-10-06T23:59:00` 访问 +
   `2026-10-08T00:00:00` 注册——signup 一侧唯一的时间条件是严格晚于
   visit（`:198`）。
4. **行序不改变结果。** **【实测】** 把六条 JSONL 按固定置换
   （原第 5、1、6、3、4、2 行）乱序导入后报告，输出与第 6 节逐字节相同：
   归组是 `MIN` 聚合、配对是时间戳 min/max（`:283-286`）、成员关系是
   集合运算（`:338`），均与物理行序无关。
5. **重复导入不改变结果。** **【实测】** 对同一 SQLite 连续两次
   `import` 同一文件（第二次按追加协议写入另外 6 行重复事件），报告输出
   仍与第 6 节相同：重复行只产生重复行对，不改变 `MIN`、`DISTINCT`、
   集合成员与 min/max 归约。
6. **报告只读。** 第 6–8 节的全部报告命令前后，`events` 表按
   `(user_id, event, timestamp)` 排序的快照逐行一致；报告路径上没有
   `INSERT`/`UPDATE`/`DELETE`（对应既有断言
   `test_report_keeps_events_unchanged`）。

## 9. 单独使用 `--include-group-pairs`：退出码 2 协议

```sh
python -m funnel report --db events.sqlite --include-group-pairs
```

**【实测】** 退出码 2、**标准输出为空**，标准错误逐字为单行：

```text
--include-group-pairs 必须与 --group-by visit-date 合用：已提供 --include-group-pairs，缺少 --group-by visit-date
```

该输出来自 `cmd_report` 的显式检查（`:385-391`，不是 argparse 的用法
错误，故没有 usage 行），检查点位于时段校验之后、`os.path.exists`
（`:393`）与 `sqlite3.connect`（`:398`）之前，因此：

- **不创建数据库。** **【实测】** 对尚不存在的路径
  `missing.sqlite` 执行 `report --db missing.sqlite --include-group-pairs`
  （不带 `--group-by`），同样退出码 2、标准输出为空、标准错误同为上面一
  行，命令结束后该文件仍不存在——先命中依赖检查，根本走不到建库分支。
- **不改变已有记录。** **【实测】** 对第 5 节导入的库执行该命令前后分别
  快照 `events` 全部六行，`diff` 无差异。
- 标准错误同时指出两个相关参数名：开关本身 `--include-group-pairs` 与
  缺失的依赖 `--group-by visit-date`。

对应既有断言（仅作源码定位，非实测来源）：
`test_flag_without_group_by_rejected`、
`test_flag_without_group_by_does_not_create_db`。

## 10. 验收要点与不变行为

- 验收以三者的对应关系为准：**完整输入**（第 4 节六条 JSONL 逐字节）、
  **确定输出**（第 5 节导入 JSON、第 6 节完整报告 JSON、第 7–9 节的开关
  组合与退出码 2 协议）、**源码对应**（每条结论标注的
  `funnel/__main__.py` 行号与判断条件）。
- 建议的逐人复核顺序：先按 `_qualifying_visit_date_by_user`（`:253-269`）
  确认 u1/u2 归 6 日、u3 归 7 日；再按 `_converted_sql` 的三个条件
  （严格晚于 `:198`、秒差上界 `:203-205`、visit 时段 `:182-186`）逐人
  枚举有效行对；然后按 `_reduce_pairs`（`:272-287`）核对"最早注册 +
  最晚可配对访问"；最后用第 6.3 节四条勾稽（数组长度、合并后码点排序与
  顶层一致、组间无重复用户、无转化组为空数组）对账。
- 本文数值分两类标记：**【实测】** 为 2026-10-07 在本机（Linux/WSL2，
  CPython 3.14.4）用当前工作树实际执行公开命令的观测；**【源码推导】** 为
  对当前源码的静态推导。`tests/` 中的用例仅作为行为被固定位置的索引被
  引用，其断言不计入实测。复核时若实际观察与推导冲突，以实际观察为准并
  修正本文。
- 本次仅新增本说明：命令入口、SQLite 表结构、追加导入协议（无去重、整批
  事务）、报告只读不改写事件记录、未启用开关时的输出字段，全部维持现状。
