# 组内转化耗时（--include-group-latency）：源码行为核对说明

核对日期：2026-10-08
核对基线：在 HEAD `60ab036` 之上新增 `--include-group-latency` 开关的工作树。

## 1. 核对依据与范围

本说明覆盖**新增**的组内转化耗时流程，即从 JSONL 导入到分组报告中每个
日期组内 `conversion_latency` 对象的完整统计链路：

```sh
python -m funnel import <events.jsonl> --db <events.sqlite>
python -m funnel report --db <events.sqlite --group-by visit-date
       --include-group-latency [--include-latency] [--within-seconds N]
```

目的是让复核者用任务给定的七条合成事件，逐人核对"谁归到哪一天、谁算
转化、每个转化用户贡献多少秒、各组 min/max/mean 如何得到、无转化组为何
三项均为 null"。结论全部指向当前仓库的真实文件、函数与判断条件；新开关
只做加法：不传时命令入口、输出字段、SQLite 表结构、追加导入协议、访问
时段筛选语义、顶层耗时口径与报告不改写事件记录的行为均保持现状。

| 文件 | 内容 |
|---|---|
| `funnel/__main__.py` | 全部实现：CLI 入口、JSONL 校验、SQLite 读写、漏斗 SQL、归组、配对归约、耗时与组内耗时装配、输出 |
| `tests/test_report_include_group_latency.py` | 组内耗时开关的 20 个回归测试（验收样例、开关独立性、组内耗时语义、不变量、错误协议） |
| `docs/verification-conversion-latency.md` | 顶层耗时的核对说明（姊妹篇，口径完全相同） |
| `docs/verification-group-pairs.md` | 组内配对明细的核对说明（姊妹篇，依赖检查与装配位置一一对应） |
| `docs/verification-visit-date.md` | 按访问日期分组的核对说明（姊妹篇） |

`funnel/__main__.py` 中本说明引用的关键位置（行号对应当前文件）：

| 函数 / 常量 | 行 | 职责 |
|---|---|---|
| `parse_line` / `load_events` | 56–130 | 单行校验与整文件严格 UTF-8 校验 |
| `cmd_import` | 133–159 | `import` 子命令：先整文件校验，再事务追加 |
| `_visit_filter` | 184–201 | 段内 visit 过滤片段（含起点、不含终点），汇总/归组/配对共用 |
| `_converted_sql` / `_converted_params` | 204–232 | 有效配对枚举 SQL 与参数装配（汇总、编号、配对、耗时共用） |
| `_qualifying_visit_date_by_user` | 268–284 | 归组查询：每人最早合格 visit 的 UTC 日期 |
| `_reduce_pairs` | 287–302 | 配对归约：每人最早有效 signup 配对最晚可配对 visit |
| `_latency_payload` | 324–352 | 由同一份归约结果装配耗时对象（顶层全量、组内子集共用） |
| `_build_visit_date_groups` | 357–419 | 分组结果唯一装配点：人数、比例、组内编号、配对与组内耗时 |
| `cmd_report` | 422–579 | `report` 子命令：参数检查、查询、归约、归组、JSON 输出 |
| `cmd_report` 中开关依赖检查 | 457–466 | `--include-group-latency` 必须与 `--group-by visit-date` 合用 |
| `cmd_report` 中配对归约块 | 506–526 | 顶层与组内共用的一次性归约（新开关也触发它） |
| `cmd_report` 中 `--group-by` 块 | 527–555 | 归组查询、转化集合、装配调用（传入组内耗时开关与归约结果） |
| `main` | 583–674 | argparse 子命令与参数装配（`--include-group-latency` 在 649–657） |

**数值来源声明（两种标记）：**

- 标 **【实测】** 的命令输出、退出码与标准错误文本，是 2026-10-08 在本机
  （Linux/WSL2，CPython 3.14.4）以当前工作树源码实际执行的观测结果；
  `funnel` 包经项目根目录解析（`PYTHONPATH` 指向项目根），样例数据放在
  临时目录，与"在项目根目录执行下文命令"等价。
- 其余结论标 **【源码推导】**，依据为 `funnel/__main__.py` 当前源码（含其
  SQL 与 Python 表达式）及 CPython 标准库 `json`/`argparse`/`sqlite3` 的
  现行行为。

## 2. 从公开入口到组内 `conversion_latency`

【源码推导】成功路径的数据全部来自同一份配对归约，组内耗时不另写 SQL：

1. **段内 visit 资格**只有一个维护点 `_visit_filter`（`:184-201`）：
   `event = 'visit'`，提供时段时追加 `timestamp >= ? AND timestamp < ?`
   （含起点、不含终点）。访问人数、归组、配对枚举都经此取资格。
2. **有效配对枚举** `_converted_sql`（`:204-223`）：`events v` 自连接
   `events s`，条件为同用户、`s.event = 'signup'`、`s.timestamp >
   v.timestamp`（注册**严格**晚于访问，相等不转化），有窗口时追加
   `strftime('%s', s) - strftime('%s', v) <= ?`（上界**含**等值）；
   visit 侧再套上第 1 步的段内过滤，signup 自身不做时段过滤——**注册可以
   晚于时段终点**。
3. **配对归约** `_reduce_pairs`（`:287-302`）：枚举行对在 Python 侧归约，
   每人先选时间最早的有效 signup，再在能与它配对的 visit 中选最晚一次。
   耗时取这一对的秒差，**不是**该用户全部配对的最短间隔。
4. **耗时装配** `_latency_payload`（`:324-352`）：`user_ids=None` 时遍历
   归约结果的全部用户（顶层口径），否则只遍历给定子集（组内口径）；
   `min/max` 用 `int(...)` 落成整数秒，`mean` 为 `total / len` 真除。
   子集为空时返回三项 `None`。
5. **归组** `_qualifying_visit_date_by_user`（`:268-284`）：每人取
   `MIN(timestamp)` 的前 10 个字符（UTC 日期）。**只按最早合格 visit
   归组，配对 visit 落在哪一天完全不参与归组。**
6. **分组装配** `_build_visit_date_groups`（`:357-419`）：组内转化成员 =
   本组访问成员 ∩ 转化集合；`group_latency` 为真时对每个组调用
   `_latency_payload(latency_by_user, converted_ids)`（`:412-417`），即
   组内耗时只是同一归约结果按组的分组统计。每个转化用户只归一个日期组，
   故各组耗时成员互不重叠，且与顶层耗时对象同源。

## 3. 耗时与归组语义（逐条可在源码核对）

1. **每人只贡献一个耗时。** 归约以 user_id 为键（`:294-301`），最早
   signup 相同才在其中取最晚 visit；重复事件、重复导入产生的相同行对不
   改变 min/max/sum。
2. **取最早有效 signup 配最晚可配对 visit，不取最短间隔。** 更早的
   signup 若在当前筛选（窗口、时段）下没有任何合格 visit，就不是"有效
   signup"；收紧 `--within-seconds` 时被选中的 signup 可能改变（与顶层
   `--include-latency` 同一行为，见 `docs/verification-conversion-latency.md`
   第 3 节）。
3. **归组与配对解耦。** u1 的配对 visit 在归组日期的后一天，耗时仍计入
   原日期组；不会按配对时间重新归组，也不会在两个组里重复。
4. **窗口上界含等值、注册严格晚于访问。** SQL 分别是 `<= ?`（`:217-219`）
   与 `s.timestamp > v.timestamp`（`:213`）。
5. **均值不取整。** `total / len(durations)` 为 Python 真除（`:352`），
   JSON 序列化按 CPython `repr(float)` 最短表示输出（如 `45.0`、`45.5`）。
6. **无转化组三项均为 null；无合格访问时没有组。** 空子集走
   `:346-347` 返回三个 `None`；没有任何合格 visit 的用户不产生日期键
   （`:376-380` 只按 `date_by_user` 建组）。

## 4. 固定合成样例（2026 年 10 月，七条事件，UTC）

以下七条完整 UTF-8 JSONL 记录，按任务给定顺序保存为 `group.jsonl`：

```jsonl
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:00"}
{"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:30"}
{"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T10:01:00"}
{"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T12:00:00"}
{"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T12:01:00"}
{"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T11:00:00"}
```

人物设定：

- **u1**：6 日 23:59:00 visit（最早合格 visit，决定归 6 日组）；7 日
  10:00:00、10:00:30 两次 visit；7 日 10:01:00 signup。唯一（也即最早
  有效）signup 与两次 7 日 visit 的间隔分别为 60、30 秒，能配对它的最晚
  visit 是 **10:00:30**，故 u1 贡献 **30 秒**；配对 visit 在 7 日，归组
  仍是 6 日。
- **u2**：6 日 12:00:00 visit、12:01:00 signup，间隔 **60 秒**，归 6 日组。
- **u3**：仅 7 日 11:00:00 visit，无 signup，归 7 日组且不转化。

## 5. 一次导入

```sh
python -m funnel import group.jsonl --db events.sqlite
```

**【实测】** 标准输出（一行，退出码 0，标准错误为空）：

```json
{"imported": 7}
```

七条记录全部通过 `parse_line`，`len(events)` 即 7（`:158`）；数据库不
存在时新建并建表，事务内追加（`:143-151`）。

## 6. 验收报告：分组 + 组内耗时 + 60 秒窗口

任务指定的验收命令：

```sh
python -m funnel report --db events.sqlite --group-by visit-date \
    --include-group-latency --within-seconds 60
```

**【实测】** 完整标准输出（一行，退出码 0，标准错误为空；为便于阅读
下方折行，实际输出为单行）：

```json
{"visit_users": 3, "converted_users": 2, "conversion_rate": 0.6666666666666666,
 "visit_date_groups": [
   {"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 2,
    "conversion_rate": 1.0,
    "conversion_latency": {"min_seconds": 30, "max_seconds": 60,
                           "mean_seconds": 45.0}},
   {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0,
    "conversion_rate": 0.0,
    "conversion_latency": {"min_seconds": null, "max_seconds": null,
                           "mean_seconds": null}}]}
```

交叉核对：`test_acceptance_group_latency_within_60`。字段顺序即
`payload` 装配顺序（`:563-577`）：三个汇总字段、`visit_date_groups`；
顶层**没有** `conversion_latency`（只由 `--include-latency` 控制，
`:571-573`）。组对象内四个基础字段在前、`conversion_latency` 在最后
（`:397-417`），耗时对象恰含三个字段。

### 6.1 归组（每人取最早合格 visit 的 UTC 日期）

| 用户 | 合格 visit | `MIN(timestamp)` | 归入日期 |
|---|---|---|---|
| u1 | 10-06 23:59:00、10-07 10:00:00、10-07 10:00:30 | `2026-10-06T23:59:00` | `2026-10-06` |
| u2 | 10-06 12:00:00 | `2026-10-06T12:00:00` | `2026-10-06` |
| u3 | 10-07 11:00:00 | `2026-10-07T11:00:00` | `2026-10-07` |

【源码推导】u1 的归组只由 6 日 23:59:00 决定；配对用的 7 日 visit 不
参与（`:268-284`，第 2 节第 5 步）。

### 6.2 转化与逐人秒差（窗口 60，上界含等值）

- **u1：转化，耗时 30 秒，计入 6 日组。** signup 10:01:00 严格晚于两次
  7 日 visit；与 10:00:00 间隔 60 秒（`<= 60` 成立），与 10:00:30 间隔
  30 秒。归约选最早 signup（只有一个）配最晚可配对 visit，即 10:00:30
  → 30 秒（`:287-302`）。6 日 23:59:00 visit 与 signup 间隔 86520 秒，
  超出窗口，但不影响 u1 的转化身份与所选配对。
- **u2：转化，耗时 60 秒，计入 6 日组。** 12:00:00 → 12:01:00 恰为
  60 秒，`<= 60` 成立（上界含等值）。
- **u3：不转化。** 无 signup 行，自连接枚举不到行对；计入 7 日组访问
  人数，该组转化成员为空。
- **汇总**：`visit_users = 3`、`converted_users = 2`、比例
  `2/3 = 0.6666666666666666`（`:562` 真除）。

### 6.3 两组耗时

| visit_date | 转化成员 | 各人秒差 | min | max | mean |
|---|---|---|---|---|---|
| 2026-10-06 | u1、u2 | 30、60 | 30（int） | 60（int） | (30+60)/2 = **45.0** |
| 2026-10-07 | （空） | — | null | null | null |

【源码推导】6 日组 `_latency_payload(best, {"u1","u2"})`：
`min([30,60])=30`、`max=60`、`90/2=45.0`（`:344-350`）。7 日组传入空
集合，durations 为空，直接返回三个 `None`（`:346-347`），JSON 序列化
为 `null`。

## 7. 开关独立性与未开启行为（实测对照）

- **组内耗时不自动开启任何顶层明细。**
  **【实测】** `--group-by visit-date --include-group-latency`（无窗口）
  的输出中顶层只有三个汇总字段加 `visit_date_groups`，无
  `conversion_latency`、`conversion_pairs`、编号数组；两组耗时与第 6 节
  相同（本样例无窗口结论不变）。交叉核对：
  `test_group_latency_does_not_auto_enable_top_level_latency`。
- **顶层耗时仍只由 `--include-latency` 控制。**
  **【实测】** 两个开关同开时，顶层与 6 日组各有一个
  `conversion_latency`，数值相同（两个转化用户都在 6 日组）；只开
  `--include-latency` 时组内不出现任何耗时字段。交叉核对：
  `test_with_top_level_latency_both_appear`、
  `test_top_level_latency_alone_adds_nothing_inside_groups`。
- **不传新开关时输出逐字节不变。**
  **【实测】** `--group-by visit-date --within-seconds 60` 的组对象只有
  四个基础字段：

  ```json
  {"visit_users": 3, "converted_users": 2, "conversion_rate": 0.6666666666666666, "visit_date_groups": [{"visit_date": "2026-10-06", "visit_users": 2, "converted_users": 2, "conversion_rate": 1.0}, {"visit_date": "2026-10-07", "visit_users": 1, "converted_users": 0, "conversion_rate": 0.0}]}
  ```

  交叉核对：`test_without_flag_groups_have_no_latency_field`、
  `test_metrics_identical_with_and_without_flag`（各基础数值逐字段相等）。
- **与 `--include-group-pairs`/`--include-users` 合用**：组对象按装配顺序
  带基础四字段、两个编号数组（若开）、配对数组（若开）、耗时对象（若开），
  互不影响；配对与耗时源自同一份归约，天然勾稽。交叉核对：
  `tests/test_report_include_group_latency.py` 中
  `test_report_keeps_events_unchanged` 的多开关组合。
- **均值不取整**：两个转化用户耗时 30、61 秒时组内 mean 输出 `45.5`
  （`test_group_latency_unrounded_mean`）；min/max 序列化为 JSON 整数、
  mean 为 JSON 小数。
- **乱序、重复、重复导入不变**：固定乱序行序、混入重复行、整批二次导入
  三种情况下第 6 节数值完全一致（`test_shuffled_and_duplicate_events_give_same_group_latency`、
  `test_reimport_same_batch_keeps_group_latency`）。
- **无合格访问时分组为空**：只有 signup 事件的库输出
  `"visit_date_groups": []`（`test_no_qualifying_visits_gives_empty_groups`）。

## 8. 错误协议：单独使用 `--include-group-latency`

依赖检查位于一切数据库访问之前（`:457-466`），与
`--include-group-pairs` 的检查（`:444-453`）同构。

**【实测】** 无论库是否存在，执行

```sh
python -m funnel report --db <db> --include-group-latency
```

均为退出码 2、标准输出为空，标准错误：

```
--include-group-latency 必须与 --group-by visit-date 合用：已提供 --include-group-latency，缺少 --group-by visit-date
```

- 指向不存在的路径时，文件不会被创建（检查先于 `os.path.exists` 与
  `sqlite3.connect`，`:468-471`）；交叉核对
  `test_flag_without_group_by_does_not_create_db`。
- 数据库不存在但参数组合合法时，仍走既有路径错误协议：退出码 2、
  标准输出为空、标准错误含路径与"数据库不存在"
  （`test_missing_db_still_follows_path_error_protocol`）；库不可访问
  （如路径是目录）时标准错误含路径与 sqlite3 原因。
- 报告全程只读：错误与成功路径都不改动 events 记录
  （`test_report_keeps_events_unchanged`）。

## 9. 验收要点与不变行为

1. 七条事件 + `--group-by visit-date --include-group-latency
   --within-seconds 60`：3 人访问、2 人转化；6 日组 min 30、max 60、
   mean 45.0；7 日组三项均为 null。**【实测，见第 6 节】**
2. 归组只看最早合格 visit 的 UTC 日期；配对 visit 在后一天不移动所属组。
3. 每人一个耗时：最早有效 signup 配最晚可配对 visit，不取最短间隔；
   min/max 整数秒，mean 真除不取整。
4. 时段含起点不含终点、段外 visit 不参与、signup 可晚于终点；signup
   严格晚于 visit，窗口上界含等值。
5. 无转化组三项 null；无合格访问分组为空。
6. 新开关不依赖、不自动开启其他明细；顶层耗时只由 `--include-latency`
   控制；不传新开关既有输出不变。
7. 单独使用新开关：退出码 2、stdout 空、stderr 指出开关与依赖，先于
   数据库访问，不建库、不改记录；库不存在/不可访问同为退出码 2 且
   stderr 含路径与原因。
8. 全套回归：`python3 -m unittest discover -s tests` 共 204 个测试全部
   通过（其中新增 `tests/test_report_include_group_latency.py` 20 个）。
