# 本地产品事件漏斗台

整理产品事件，分析转化漏斗及用户分组。面向本地单机使用，仅依赖 Python 3 标准库与 SQLite。

## 使用说明

导入 JSONL 事件（向数据库追加，可重复执行）：

```sh
python -m funnel import sample.jsonl --db events.sqlite
```

统计全库 visit → signup 两步漏斗：

```sh
python -m funnel report --db events.sqlite
```

只统计访问后 N 秒内完成注册的转化（可选，仅影响本次报告，不改写数据库）：

```sh
python -m funnel report --db events.sqlite --within-seconds 60
```

只统计指定访问时间段内有访问的用户（可选，与 `--within-seconds` 可同时使用，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite \
  --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-06T11:00:00
```

`--visit-from` 与 `--visit-before` 必须成对使用，均为 `YYYY-MM-DDTHH:MM:SS` 格式并视为 UTC；范围含起点、不含终点，起点须严格早于终点。

在汇总之外列出计入统计的用户编号（可选，与 `--within-seconds`、`--visit-from/--visit-before` 可同时使用，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite --include-users
```

在汇总之外列出转化用户的配对明细（可选，不带取值，可单独使用，也可与 `--include-users` 及上述筛选项合用，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite --include-pairs
```

## 事件格式

UTF-8 JSONL，每个非空行是一个 JSON 对象，必填字段：

- `user_id`：非空字符串，按原值区分用户
- `event`：仅接受 `visit` 或 `signup`
- `timestamp`：`YYYY-MM-DDTHH:MM:SS` 有效时间，统一视为 UTC，不接受时区后缀或小数秒

额外字段被忽略；空白行被忽略但计入物理行号。任一行非法（非法 JSON、非对象、缺失字段、字段类型或取值不符，或该物理行的字节不是合法 UTF-8）则整次导入失败：退出码 2、标准输出为空、标准错误给出首个错误的行号与原因，数据库已有记录不变。编码错误按物理行顺序只报告首个错误，不替换字节、不跳过坏行、不尝试其他编码；LF 与 CRLF 文件同样处理，末行没有换行符时仍能正确定位。

## 输出与退出码

- 导入成功：退出码 0，标准输出仅 `{"imported": N}`（本次有效记录数）
- 报告成功：退出码 0，标准输出仅 `{"visit_users": V, "converted_users": C, "conversion_rate": R}`；加 `--include-users` 时追加 `"visit_user_ids"` 与 `"converted_user_ids"` 两个字符串数组；加 `--include-pairs` 时追加 `"conversion_pairs"` 配对明细数组（两开关可合用）
- 输入文件无法读取、数据库无法访问、报告数据库不存在：退出码 2，标准输出为空，标准错误说明路径与原因

## 统计规则

- 访问人数：发生过 `visit` 的用户去重计数
- 转化人数：存在严格晚于某次 `visit` 的 `signup` 的用户去重计数（相等时刻不算转化）
- `--within-seconds N`：在上述条件上追加"间隔不超过 N 秒"（恰好 N 秒计入）；同一用户多次访问中任意一次满足即可。仅接受全为 ASCII 数字且大于零的整数（允许前导零），否则退出码 2、标准输出为空、标准错误指出 `--within-seconds` 及原因，不创建数据库也不修改记录
- `--visit-from` / `--visit-before`：成对使用时，访问人数只计在该时间段（含起点、不含终点）内发生过 `visit` 的用户，按 `user_id` 原值去重，同一用户任一段内访问满足即计一人；转化只允许与段内 `visit` 配对（段外访问不参与），`signup` 可以晚于终点。与 `--within-seconds` 同时使用时，注册仍须严格晚于段内访问，间隔上界（含）继续生效。两值均须为 `YYYY-MM-DDTHH:MM:SS` 格式（UTC，不带前后空白、时区后缀或小数秒）且为有效日期；缺少任一配对参数或参数值、起终点相等或逆序均退出码 2、标准输出为空、标准错误指出相关参数与原因，且先于数据库访问，不创建数据库或改动记录
- 统计不依赖行序；重复事件与重复导入不增加人数
- `--include-users`：在汇总之外输出 `visit_user_ids` 与 `converted_user_ids`，分别列出计入访问与转化统计的用户编号。明细沿用与汇总完全相同的筛选条件（访问时段、`--within-seconds` 上界含等值、signup 严格晚于段内 visit），数组长度等于对应人数，转化数组的成员都在访问数组中。两个数组按 `user_id` 原值去重，再按 Unicode 码点字典序升序排列：区分大小写，保留编号中的空白与中文，不按数字大小排序。不传该开关时仍只输出三个汇总字段；两种模式的汇总数值一致。没有符合条件的访问时三项指标为 0、两个数组为空；有访问却无人转化时仅转化数组为空
- `--include-pairs`：在汇总之外输出 `conversion_pairs` 配对明细，用于核对每个转化用户的事件依据。明细沿用与汇总完全相同的配对条件（访问时段含起点不含终点、signup 严格晚于段内 visit、`--within-seconds` 上界含等值）。每个转化用户只出现一次：先在其全部有效配对中选时间最早的 `signup`，再从能与该注册有效配对的 `visit` 中选时间最晚的一次。每个对象只含 `user_id`、`visit_timestamp`、`signup_timestamp` 三个字段，时间戳沿用 `YYYY-MM-DDTHH:MM:SS` UTC 格式。数组按 `user_id` 原值的 Unicode 码点字典序升序排列，长度等于 `converted_users`；与 `--include-users` 合用时，数组中的编号集合等于 `converted_user_ids`。没有转化时数组为空；不传该开关时输出保持现状，该开关不改变汇总数值与编号明细
- 转化比例 = 转化人数 / 访问人数；零访问时三项均为 0

## 样例

`sample.jsonl` 为 2026-10-06 的虚构数据：u1 在 10 点 visit、11 点 signup；u2 仅 10 点 visit；u3 在 9 点 signup、10 点 visit。依次执行上述两条命令，预期输出：

```json
{"imported": 5}
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```
