# 本地产品事件漏斗台

整理产品事件，分析转化漏斗及用户分组。面向本地单机使用，仅使用 Python 3 标准库与 SQLite，只处理本地虚构用户数据。

## 使用说明

导入 JSONL 事件（追加到指定数据库，可重复导入，重复事件不重复计数）：

```sh
python -m funnel import sample.jsonl --db events.sqlite
# 标准输出：{"imported": 5}
```

生成 visit → signup 两步漏斗报告（统计整个数据库）：

```sh
python -m funnel report --db events.sqlite
# 标准输出：{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

成功时退出码为 0；输入无法读取、记录非法、数据库不可访问或报告库不存在时退出码为 2，标准输出为空，原因写往标准错误。

## 事件格式（JSONL，UTF-8）

每个非空行是一个 JSON 对象，必填字段：

- `user_id`：非空字符串，按原值区分用户
- `event`：仅接受 `"visit"` 或 `"signup"`
- `timestamp`：`YYYY-MM-DDTHH:MM:SS` 格式的有效时间，统一视为 UTC（不接受时区后缀或小数秒）

额外字段被忽略；空白行忽略但计入物理行号。任一行非法则整次导入失败，不保存任何事件。

## 统计规则

- `visit_users`：发生过 visit 的去重用户数
- `converted_users`：存在严格晚于某次 visit 的 signup 的去重用户数（相等时刻不算转化）
- `conversion_rate`：`converted_users / visit_users`；零访问时三项均为 0
