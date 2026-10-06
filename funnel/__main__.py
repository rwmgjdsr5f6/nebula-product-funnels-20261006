"""命令行入口：python -m funnel import / report。"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    user_id TEXT NOT NULL,
    event TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    UNIQUE (user_id, event, timestamp)
)
"""

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
VALID_EVENTS = ("visit", "signup")
REQUIRED_FIELDS = ("user_id", "event", "timestamp")


def fail(message):
    """打印错误到标准错误并以退出码 2 终止。"""
    print(message, file=sys.stderr)
    sys.exit(2)


def validate_record(obj):
    """校验一条事件记录，返回错误原因字符串；合法时返回 None。"""
    if not isinstance(obj, dict):
        return "expected a JSON object"
    for field in REQUIRED_FIELDS:
        if field not in obj:
            return "missing required field %r" % field
    if not isinstance(obj["user_id"], str) or obj["user_id"] == "":
        return "user_id must be a non-empty string"
    if obj["event"] not in VALID_EVENTS:
        return "event must be 'visit' or 'signup'"
    timestamp = obj["timestamp"]
    if not isinstance(timestamp, str) or not TIMESTAMP_RE.match(timestamp):
        return "timestamp must match YYYY-MM-DDTHH:MM:SS (UTC, no timezone or fractions)"
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return "timestamp is not a valid calendar time"
    return None


def load_events(path):
    """读取并校验 JSONL 文件，返回 (user_id, event, timestamp) 列表。

    任一非空行非法时直接终止（退出码 2），不返回部分结果。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        fail("cannot read input file %s: %s" % (path, exc.strerror or exc))
    except UnicodeDecodeError as exc:
        fail("cannot read input file %s: not valid UTF-8 (%s)" % (path, exc))

    events = []
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            fail("line %d: invalid JSON (%s)" % (lineno, exc.msg))
        error = validate_record(obj)
        if error is not None:
            fail("line %d: %s" % (lineno, error))
        events.append((obj["user_id"], obj["event"], obj["timestamp"]))
    return events


def cmd_import(args):
    events = load_events(args.input)
    try:
        conn = sqlite3.connect(args.db)
        try:
            with conn:
                conn.execute(SCHEMA)
                conn.executemany(
                    "INSERT OR IGNORE INTO events (user_id, event, timestamp)"
                    " VALUES (?, ?, ?)",
                    events,
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        fail("cannot write to database %s: %s" % (args.db, exc))
    print(json.dumps({"imported": len(events)}))


def cmd_report(args):
    if not os.path.exists(args.db):
        fail("database %s does not exist" % args.db)
    try:
        conn = sqlite3.connect(args.db)
        try:
            visit_users = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE event = 'visit'"
            ).fetchone()[0]
            converted_users = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT v.user_id
                    FROM events v
                    WHERE v.event = 'visit'
                      AND EXISTS (
                          SELECT 1 FROM events s
                          WHERE s.event = 'signup'
                            AND s.user_id = v.user_id
                            AND s.timestamp > v.timestamp
                      )
                )
                """
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        fail("cannot read database %s: %s" % (args.db, exc))

    conversion_rate = (converted_users / visit_users) if visit_users else 0
    print(json.dumps({
        "visit_users": visit_users,
        "converted_users": converted_users,
        "conversion_rate": conversion_rate,
    }))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="funnel",
        description="导入合成 JSONL 事件并生成 visit -> signup 两步漏斗报告。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_import = subparsers.add_parser("import", help="导入 JSONL 事件文件")
    p_import.add_argument("input", help="JSONL 文件路径")
    p_import.add_argument("--db", required=True, help="SQLite 数据库路径")
    p_import.set_defaults(func=cmd_import)

    p_report = subparsers.add_parser("report", help="输出漏斗报告")
    p_report.add_argument("--db", required=True, help="SQLite 数据库路径")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
