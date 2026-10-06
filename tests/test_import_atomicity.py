"""JSONL 导入失败原子性的回归测试。

固定公开约定：

- 成功导入：退出码 0，标准输出仅 ``{"imported": N}``，事件追加入库
- 任一非空行非法：退出码 2、标准输出为空、标准错误给出输入路径、
  首个错误行的**物理行号**与原因；不报告后续行，也不输出异常堆栈
- 失败的导入整批不写入：失败前后 events 表逐行一致（用户编号、事件、
  时间、记录数量），已入库数据的报告结果不变
- 空白行不导入，但仍参与物理行号计算
- 目标数据库路径原先不存在时，失败的导入不创建数据库文件

只通过公开命令 ``python -m funnel import|report`` 观察行为，不直接调用
任何内部函数。仅使用 Python 3 标准库；每个场景使用独立临时目录中的
JSONL 与 SQLite，全部用户编号均为虚构，测试结束不遗留任何文件，
不依赖预存数据、第三方包或网络。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 固定种子数据：seed-user 在 10:00 visit、10:01 signup（间隔 60 秒，转化）。
SEED_EVENTS = [
    {"user_id": "seed-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "seed-user", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
]

# 失败后数据库应当仍然只剩种子两行（按 user_id, event, timestamp 排序）。
EXPECTED_SEED_ROWS = [
    ("seed-user", "signup", "2026-10-06T10:01:00"),
    ("seed-user", "visit", "2026-10-06T10:00:00"),
]

# 两个失败场景共用的前三行（物理行号）：
#   第 1 行：new-user 10:00:00 visit（合法）
#   第 2 行：空白行（不导入，但占据一个物理行号）
#   第 3 行：new-user 10:00:30 signup（合法）
# 若空白行不参与行号计算，首个错误就会被报成第 3 行而非第 4 行。
NEW_USER_VISIT = json.dumps(
    {"user_id": "new-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    ensure_ascii=False,
)
NEW_USER_SIGNUP = json.dumps(
    {"user_id": "new-user", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    ensure_ascii=False,
)
LEADING_VALID_AND_BLANK_LINES = [NEW_USER_VISIT, "", NEW_USER_SIGNUP]

# 场景 A：第 4 行只有 "{"（非法 JSON），第 5 行是缺少必填字段的 JSON 对象。
# 首个错误必须定位在第 4 行的非法 JSON，不能越过它去报告第 5 行。
INVALID_JSON_LINES = LEADING_VALID_AND_BLANK_LINES + [
    "{",
    json.dumps({"user_id": "x9", "event": "visit"}, ensure_ascii=False),  # 缺 timestamp
]

# 场景 B：第 4 行 event、timestamp 合法但缺少 user_id，第 5 行是非法 JSON。
# 首个错误仍必须是第 4 行的 user_id 缺失，而非第 5 行的非法 JSON。
MISSING_USER_ID_LINES = LEADING_VALID_AND_BLANK_LINES + [
    json.dumps(
        {"event": "visit", "timestamp": "2026-10-06T10:00:00"},
        ensure_ascii=False,
    ),
    "{not-json",
]


def run_funnel(*cli_args):
    """在项目目录执行 python -m funnel，返回完成的进程结果。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "funnel", *cli_args],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class ImportAtomicityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助函数 ---------------------------------------------------------

    def db_path(self, name="events.sqlite"):
        return os.path.join(self.tmpdir, name)

    def write_jsonl(self, name, events):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def write_raw_lines(self, name, lines):
        """按给定物理行写入 JSONL（允许空字符串表示空白行）。"""
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def seed_database(self, db=None, jsonl_name="seed.jsonl"):
        """先成功导入固定种子数据，返回 (db 路径, 导入命令结果)。"""
        if db is None:
            db = self.db_path()
        path = self.write_jsonl(jsonl_name, SEED_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="种子导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {"imported": 2})
        return db

    def snapshot_events(self, db):
        """返回 (逐行记录快照, 记录总数)，用于失败前后逐行比对。"""
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return rows, count

    def assert_failed_import_is_atomic(self, db, path, reason_tokens):
        """失败导入的完整公开约定：退出码/输出/行号/原因 + 数据库逐行不变。"""
        before_rows, before_count = self.snapshot_events(db)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

        # 标准错误必须给出输入路径、首个错误物理行号（第 4 行）与原因。
        self.assertIn(path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        for token in reason_tokens:
            self.assertIn(token, result.stderr)
        # 不得越过首个错误去报告第 5 行，也不得吐出异常堆栈。
        self.assertNotIn("第 5 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 整批不写入：用户编号、事件类型、时间、记录数量完全一致。
        after_rows, after_count = self.snapshot_events(db)
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(after_count, before_count)

    def assert_only_seed_rows_remain(self, db):
        """失败后表中恰好是种子两行，new-user 的任何合法前置记录都不得留下。"""
        rows, count = self.snapshot_events(db)
        self.assertEqual(rows, EXPECTED_SEED_ROWS)
        self.assertEqual(count, 2)
        self.assertFalse(
            any(row[0] == "new-user" for row in rows),
            msg="失败的导入不得留下 new-user 记录: %r" % (rows,),
        )

    def assert_seed_report_unchanged(self, db):
        """种子用户的两步报告仍为访问 1 人、转化 1 人、比例 1。"""
        result = run_funnel("report", "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["visit_users"], 1)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1)

    # -- 失败原子性：空白行计入物理行号，整批不写入 ----------------------

    def test_invalid_json_line_fails_atomically(self):
        db = self.seed_database(jsonl_name="seed-a.jsonl")
        path = self.write_raw_lines("bad-json.jsonl", INVALID_JSON_LINES)

        # 第 4 行 "{" 是首个错误：报非法 JSON 原因，不得转而报告第 5 行。
        self.assert_failed_import_is_atomic(db, path, ["非法 JSON"])
        self.assert_only_seed_rows_remain(db)
        self.assert_seed_report_unchanged(db)

        # 失败之后同一数据库上的合法导入仍正常追加，证明失败没有破坏库。
        repair = self.write_jsonl("repair.jsonl", SEED_EVENTS)
        again = run_funnel("import", repair, "--db", db)
        self.assertEqual(again.returncode, 0, msg=again.stderr)
        self.assertEqual(json.loads(again.stdout), {"imported": 2})
        rows, count = self.snapshot_events(db)
        # 追加后共 4 行；快照按 user_id、event、timestamp 排序，
        # 两条 signup 排在两条 visit 之前。
        self.assertEqual(
            rows,
            [
                ("seed-user", "signup", "2026-10-06T10:01:00"),
                ("seed-user", "signup", "2026-10-06T10:01:00"),
                ("seed-user", "visit", "2026-10-06T10:00:00"),
                ("seed-user", "visit", "2026-10-06T10:00:00"),
            ],
        )
        self.assertEqual(count, 4)

    def test_missing_user_id_line_reported_before_later_bad_json(self):
        db = self.seed_database(jsonl_name="seed-b.jsonl")
        path = self.write_raw_lines("missing-user.jsonl", MISSING_USER_ID_LINES)

        # 第 4 行缺 user_id 是首个错误：原因包含 user_id，
        # 第 5 行虽是非法 JSON 也不得被报告（同时再次固定空白行占第 2 行）。
        self.assert_failed_import_is_atomic(db, path, ["缺少", "user_id"])
        self.assert_only_seed_rows_remain(db)
        self.assert_seed_report_unchanged(db)

    # -- 数据库路径原先不存在：失败导入不创建任何数据库文件 --------------

    def test_failed_import_does_not_create_database(self):
        db = self.db_path("never-existed.sqlite")
        self.assertFalse(os.path.exists(db))
        path = self.write_raw_lines("bad-json-fresh.jsonl", INVALID_JSON_LINES)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        self.assertIn("非法 JSON", result.stderr)
        self.assertNotIn("第 5 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 数据库主文件不得被创建（journal/wal/shm 等附属文件同理：
        # 临时目录中除输入 JSONL 外不应出现任何其他文件）。
        self.assertFalse(os.path.exists(db))
        self.assertEqual(
            sorted(os.listdir(self.tmpdir)),
            [os.path.basename(path)],
        )


if __name__ == "__main__":
    unittest.main()
