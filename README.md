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

## 事件格式

UTF-8 JSONL，每个非空行是一个 JSON 对象，必填字段：

- `user_id`：非空字符串，按原值区分用户
- `event`：仅接受 `visit` 或 `signup`
- `timestamp`：`YYYY-MM-DDTHH:MM:SS` 有效时间，统一视为 UTC，不接受时区后缀或小数秒

额外字段被忽略；空白行被忽略但计入物理行号。任一行非法（非法 JSON、非对象、缺失字段、字段类型或取值不符）则整次导入失败：退出码 2、标准输出为空、标准错误给出首个错误的行号与原因，数据库已有记录不变。

## 输出与退出码

- 导入成功：退出码 0，标准输出仅 `{"imported": N}`（本次有效记录数）
- 报告成功：退出码 0，标准输出仅 `{"visit_users": V, "converted_users": C, "conversion_rate": R}`
- 输入文件无法读取、数据库无法访问、报告数据库不存在：退出码 2，标准输出为空，标准错误说明路径与原因

## 统计规则

- 访问人数：发生过 `visit` 的用户去重计数
- 转化人数：存在严格晚于某次 `visit` 的 `signup` 的用户去重计数（相等时刻不算转化）
- `--within-seconds N`：在上述条件上追加"间隔不超过 N 秒"（恰好 N 秒计入）；同一用户多次访问中任意一次满足即可。仅接受全为 ASCII 数字且大于零的整数（允许前导零），否则退出码 2、标准输出为空、标准错误指出 `--within-seconds` 及原因，不创建数据库也不修改记录
- 统计不依赖行序；重复事件与重复导入不增加人数
- 转化比例 = 转化人数 / 访问人数；零访问时三项均为 0

## 样例

`sample.jsonl` 为 2026-10-06 的虚构数据：u1 在 10 点 visit、11 点 signup；u2 仅 10 点 visit；u3 在 9 点 signup、10 点 visit。依次执行上述两条命令，预期输出：

```json
{"imported": 5}
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```
