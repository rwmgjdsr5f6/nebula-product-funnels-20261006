"""命令行入口：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--within-seconds N]
        [--visit-from YYYY-MM-DDTHH:MM:SS --visit-before YYYY-MM-DDTHH:MM:SS]

仅使用 Python 3 标准库与 SQLite，处理本地虚构用户事件。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
VALID_EVENTS = ("visit", "signup")
# 严格锚定到完整参数值的首尾：必须使用 \A/\Z 而不是 ^/$——Python 正则中
# 的 $ 可匹配末尾 LF 之前的位置，^[0-9]+$ 会把 "60\n" 误判为合法值。
WITHIN_SECONDS_RE = re.compile(r"\A[0-9]+\Z")

# --visit-from / --visit-before 的取值格式：与事件时间戳相同的
# YYYY-MM-DDTHH:MM:SS（视为 UTC）。同样严格锚定首尾，时区后缀、小数秒、
# 前后空白（含末尾 LF）一律拒绝。
VISIT_BOUND_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\Z")

# SQLite INTEGER 的上限。合法时间戳（公元 1 至 9999 年）之间的最大间隔约
# 3.2e11 秒，远小于该上限，因此把窗口钳制到此值不会改变任何统计结果，
# 同时避免绑定超大 Python 整数时触发 OverflowError。
MAX_SQLITE_INTEGER = 2**63 - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    user_id   TEXT NOT NULL,
    event     TEXT NOT NULL,
    timestamp TEXT NOT NULL
)
"""


class LineError(Exception):
    """单行记录校验失败，携带从 1 开始的物理行号与原因。"""

    def __init__(self, line_no, reason):
        self.line_no = line_no
        self.reason = reason
        super().__init__(reason)


def parse_line(text, line_no):
    """校验一条非空 JSONL 记录，返回 (user_id, event, timestamp)。"""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LineError(line_no, "非法 JSON: %s" % exc.msg)
    if not isinstance(obj, dict):
        raise LineError(line_no, "记录不是 JSON 对象")

    if "user_id" not in obj:
        raise LineError(line_no, "缺少必填字段 user_id")
    user_id = obj["user_id"]
    if not isinstance(user_id, str) or user_id == "":
        raise LineError(line_no, "user_id 必须是非空字符串")

    if "event" not in obj:
        raise LineError(line_no, "缺少必填字段 event")
    event = obj["event"]
    if not isinstance(event, str):
        raise LineError(line_no, "event 必须是字符串")
    if event not in VALID_EVENTS:
        raise LineError(line_no, "event 只接受 visit 或 signup，得到 %r" % event)

    if "timestamp" not in obj:
        raise LineError(line_no, "缺少必填字段 timestamp")
    timestamp = obj["timestamp"]
    if not isinstance(timestamp, str):
        raise LineError(line_no, "timestamp 必须是字符串")
    if not TIMESTAMP_RE.match(timestamp):
        raise LineError(
            line_no,
            "timestamp 必须是 YYYY-MM-DDTHH:MM:SS 格式，不接受时区后缀或小数秒",
        )
    try:
        datetime.strptime(timestamp, TIMESTAMP_FORMAT)
    except ValueError:
        raise LineError(line_no, "timestamp 不是有效时间: %r" % timestamp)

    return (user_id, event, timestamp)


def load_events(path):
    """读取并校验整个 JSONL 文件；任一行非法则抛出 LineError，不返回部分结果。

    以二进制逐物理行读取并严格按 UTF-8 解码：任一行的字节不是合法 UTF-8
    即抛出 LineError（携带该物理行号），不替换字节、不跳过坏行、不尝试
    其他编码。按行顺序处理，首个错误（非法 JSON、字段问题或编码错误）
    即报即停，更早行的原有原因不会被后续行的编码错误覆盖。
    """
    events = []
    with open(path, "rb") as fh:
        # 二进制迭代按 b"\n" 切分：LF 与 CRLF 一致处理，
        # 末行没有换行符时仍作为最后一行产出。
        for line_no, raw in enumerate(fh, start=1):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LineError(line_no, "UTF-8 编码无效: %s" % exc.reason)
            text = text.strip()
            if not text:
                continue  # 空白行忽略，但物理行号照常递增
            events.append(parse_line(text, line_no))
    return events


def cmd_import(args):
    try:
        events = load_events(args.file)
    except LineError as exc:
        print("%s: 第 %d 行: %s" % (args.file, exc.line_no, exc.reason), file=sys.stderr)
        return 2
    except OSError as exc:
        print("%s: 无法读取输入文件: %s" % (args.file, exc.strerror or exc), file=sys.stderr)
        return 2

    try:
        conn = sqlite3.connect(args.db)
        try:
            with conn:  # 任一条写入失败都会回滚，保证本次不保存任何事件
                conn.execute(SCHEMA)
                conn.executemany(
                    "INSERT INTO events (user_id, event, timestamp) VALUES (?, ?, ?)",
                    events,
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print("%s: 数据库无法访问: %s" % (args.db, exc), file=sys.stderr)
        return 2

    print(json.dumps({"imported": len(events)}))
    return 0


def parse_within_seconds(text):
    """--within-seconds 取值校验：仅接受全为 ASCII 数字且大于零的整数（允许前导零）。

    数值与位数均不设上限。超过 SQLite INTEGER 上限的窗口等价于不限间隔
    （合法时间戳之间的间隔不可能达到该量级），统一钳制到该上限返回；
    比较按去前导零后的十进制字符串进行，不依赖 int() 的位数限制。
    """
    if not WITHIN_SECONDS_RE.match(text):
        raise argparse.ArgumentTypeError(
            "必须是只含 ASCII 数字的正整数，得到 %r" % text
        )
    digits = text.lstrip("0")
    if not digits:
        raise argparse.ArgumentTypeError("必须大于零，得到 %r" % text)
    max_digits = str(MAX_SQLITE_INTEGER)
    if len(digits) > len(max_digits) or (
        len(digits) == len(max_digits) and digits > max_digits
    ):
        return MAX_SQLITE_INTEGER
    return int(digits)


def parse_visit_bound(text):
    """--visit-from / --visit-before 取值校验：YYYY-MM-DDTHH:MM:SS 有效时间。

    视为 UTC；不接受时区后缀、小数秒或任何前后空白。校验通过后原样返回
    字符串——所有事件时间戳格式统一，字典序比较等价于时间先后比较。
    """
    if not VISIT_BOUND_RE.match(text):
        raise argparse.ArgumentTypeError(
            "必须是 YYYY-MM-DDTHH:MM:SS 格式（视为 UTC），"
            "不接受时区后缀、小数秒或前后空白，得到 %r" % text
        )
    try:
        datetime.strptime(text, TIMESTAMP_FORMAT)
    except ValueError:
        raise argparse.ArgumentTypeError("不是有效时间: %r" % text)
    return text


def cmd_report(args):
    if not os.path.exists(args.db):
        print("%s: 数据库不存在" % args.db, file=sys.stderr)
        return 2

    # 访问时间段筛选：仅 visit 落在 [visit_from, visit_before) 内的用户计入
    # 访问人数，转化也只允许与段内 visit 配对（signup 可晚于终点）。
    if args.visit_from is None:
        visit_where = ""
        visit_where_v = ""
        window_params = ()
    else:
        visit_where = " AND timestamp >= ? AND timestamp < ?"
        visit_where_v = " AND v.timestamp >= ? AND v.timestamp < ?"
        window_params = (args.visit_from, args.visit_before)

    within_cond = ""
    within_params = ()
    if args.within_seconds is not None:
        # 任一 (visit, signup) 对满足：signup 严格晚于 visit 且间隔 <= N 秒
        within_cond = (
            "\n                 AND CAST(strftime('%s', s.timestamp) AS INTEGER)"
            "\n                     - CAST(strftime('%s', v.timestamp) AS INTEGER) <= ?"
        )
        within_params = (args.within_seconds,)

    try:
        conn = sqlite3.connect(args.db)
        try:
            conn.execute(SCHEMA)
            visit_users = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE event = 'visit'"
                + visit_where,
                window_params,
            ).fetchone()[0]
            converted_users = conn.execute(
                """
                SELECT COUNT(DISTINCT v.user_id)
                FROM events v
                JOIN events s
                  ON s.user_id = v.user_id
                 AND s.event = 'signup'
                 AND s.timestamp > v.timestamp"""
                + within_cond
                + """
                WHERE v.event = 'visit'"""
                + visit_where_v,
                within_params + window_params,
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print("%s: 数据库无法访问: %s" % (args.db, exc), file=sys.stderr)
        return 2

    conversion_rate = converted_users / visit_users if visit_users else 0
    print(
        json.dumps(
            {
                "visit_users": visit_users,
                "converted_users": converted_users,
                "conversion_rate": conversion_rate,
            }
        )
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m funnel",
        description="合成 JSONL 导入与 visit -> signup 两步漏斗报告",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="导入 JSONL 事件到 SQLite")
    p_import.add_argument("file", help="UTF-8 JSONL 事件文件路径")
    p_import.add_argument("--db", required=True, help="SQLite 数据库路径")
    p_import.set_defaults(func=cmd_import)

    p_report = sub.add_parser("report", help="统计全库两步漏斗")
    p_report.add_argument("--db", required=True, help="SQLite 数据库路径")
    p_report.add_argument(
        "--within-seconds",
        type=parse_within_seconds,
        default=None,
        metavar="N",
        help="只统计访问后 N 秒内完成的注册（正整数秒，允许前导零）；不传则不限间隔",
    )
    p_report.add_argument(
        "--visit-from",
        type=parse_visit_bound,
        default=None,
        metavar="TS",
        help="访问时间段起点（YYYY-MM-DDTHH:MM:SS，UTC，含起点）；"
        "须与 --visit-before 成对使用",
    )
    p_report.add_argument(
        "--visit-before",
        type=parse_visit_bound,
        default=None,
        metavar="TS",
        help="访问时间段终点（YYYY-MM-DDTHH:MM:SS，UTC，不含终点）；"
        "须与 --visit-from 成对使用",
    )
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    if args.command == "report":
        # 成对与顺序校验属于参数错误：先于任何数据库访问返回退出码 2。
        if (args.visit_from is None) != (args.visit_before is None):
            missing = "--visit-before" if args.visit_before is None else "--visit-from"
            parser.error("--visit-from 与 --visit-before 必须成对使用，缺少 %s" % missing)
        if args.visit_from is not None and args.visit_from >= args.visit_before:
            parser.error(
                "--visit-from 必须严格早于 --visit-before，"
                "得到 %r 与 %r" % (args.visit_from, args.visit_before)
            )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
