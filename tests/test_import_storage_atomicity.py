"""SQLite 写入中途失败时导入原子性的回归测试。

与 test_import_atomicity.py 覆盖的读取/校验阶段失败不同，本文件的输入
行全部合法，失败发生在数据库写入阶段：测试库上预先安装一个**测试夹具
约束**（BEFORE INSERT 触发器），只拒绝 user_id 为 blocked-user 的写入，
拒绝原因固定为 "blocked-user"。该约束只是测试布景，不是产品对用户编号
的新限制，产品源码、公开命令与数据库格式均不改动。

固定公开约定：

- 写入中途失败：退出码 2、标准输出为空、标准错误包含数据库路径、
  "数据库无法访问" 与触发器给出的拒绝原因；不输出异常堆栈
- 写入失败整批回滚：失败前后 events 表逐行一致（用户编号、事件、
  时间、记录数量），本次导入的合法前置记录也不留下
- 失败后的数据库仍可正常使用：普通报告结果不变，后续合法导入照常追加

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

# 写入失败场景的三条输入，全部合法；第三条在写入时被测试夹具约束拒绝。
BLOCKED_BATCH_EVENTS = [
    {"user_id": "new-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "new-user", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "blocked-user", "event": "visit", "timestamp": "2026-10-06T10:02:00"},
]

# 失败恢复场景：只含前两条 new-user 事件的合法文件。
NEW_USER_EVENTS = BLOCKED_BATCH_EVENTS[:2]

# 失败后数据库应当仍然只剩种子两行（按 user_id, event, timestamp 排序）。
EXPECTED_SEED_ROWS = [
    ("seed-user", "signup", "2026-10-06T10:01:00"),
    ("seed-user", "visit", "2026-10-06T10:00:00"),
]

# 恢复导入成功后库中恰好四行（同一排序）。
EXPECTED_FOUR_ROWS = [
    ("new-user", "signup", "2026-10-06T10:00:30"),
    ("new-user", "visit", "2026-10-06T10:00:00"),
    ("seed-user", "signup", "2026-10-06T10:01:00"),
    ("seed-user", "visit", "2026-10-06T10:00:00"),
]

# 测试夹具约束：只拒绝 blocked-user 的写入，拒绝原因固定为 "blocked-user"。
# 安装在测试库上，不属于产品 schema，也不构成对用户编号的新限制。
BLOCKING_CONSTRAINT_SQL = """
CREATE TRIGGER reject_blocked_user
BEFORE INSERT ON events
WHEN NEW.user_id = 'blocked-user'
BEGIN
    SELECT RAISE(ABORT, 'blocked-user');
END
"""


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


class ImportStorageAtomicityTests(unittest.TestCase):
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

    def seed_database(self):
        """先成功导入固定种子数据，返回 db 路径。"""
        db = self.db_path()
        path = self.write_jsonl("seed.jsonl", SEED_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="种子导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 2})
        return db

    def install_blocking_constraint(self, db):
        """在测试库上安装只拒绝 blocked-user 写入的测试夹具约束。"""
        with sqlite3.connect(db) as conn:
            conn.execute(BLOCKING_CONSTRAINT_SQL)

    def snapshot_events(self, db):
        """返回 (逐行记录快照, 记录总数)，用于失败前后逐行比对。"""
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return rows, count

    def run_blocked_import(self, db):
        """执行第三条必被写入约束拒绝的导入，返回 (失败前快照, 命令结果)。"""
        path = self.write_jsonl("blocked.jsonl", BLOCKED_BATCH_EVENTS)
        before = self.snapshot_events(db)
        result = run_funnel("import", path, "--db", db)
        return before, result

    def assert_blocked_import_failed(self, db, before, result):
        """写入中途失败的完整公开约定 + 数据库逐行不变。"""
        before_rows, before_count = before

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # 标准错误必须给出数据库路径、"数据库无法访问" 与约束的拒绝原因。
        self.assertIn(db, result.stderr)
        self.assertIn("数据库无法访问", result.stderr)
        self.assertIn("blocked-user", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 整批回滚：用户编号、事件类型、时间、记录数量完全一致；
        # 库中仍只有两条种子事件，前两条 new-user 事件也没有留下。
        after_rows, after_count = self.snapshot_events(db)
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(after_count, before_count)
        self.assertEqual(after_rows, EXPECTED_SEED_ROWS)
        self.assertEqual(after_count, 2)
        self.assertFalse(
            any(row[0] in ("new-user", "blocked-user") for row in after_rows),
            msg="失败的导入不得留下任何新事件: %r" % (after_rows,),
        )

    def assert_report(self, db, visit_users, converted_users, conversion_rate):
        """普通报告成功且汇总数值与预期一致。"""
        result = run_funnel("report", "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["visit_users"], visit_users)
        self.assertEqual(payload["converted_users"], converted_users)
        self.assertEqual(payload["conversion_rate"], conversion_rate)

    # -- 写入中途失败：整批回滚，库中只剩种子两行 --------------------------

    def test_storage_write_failure_rolls_back_entire_batch(self):
        db = self.seed_database()
        self.install_blocking_constraint(db)

        before, result = self.run_blocked_import(db)
        self.assert_blocked_import_failed(db, before, result)

        # 失败后的普通报告不受影响：访问 1 人、转化 1 人、比例 1。
        self.assert_report(db, visit_users=1, converted_users=1, conversion_rate=1)

    # -- 同一失败后的数据库：合法导入照常追加，报告随之更新 ----------------

    def test_import_after_storage_write_failure_appends_normally(self):
        db = self.seed_database()
        self.install_blocking_constraint(db)

        # 先制造一次写入中途失败（约束保留在库上），确认已整批回滚。
        before, result = self.run_blocked_import(db)
        self.assert_blocked_import_failed(db, before, result)

        # 同一数据库上导入只含两条 new-user 事件的合法文件：
        # 成功、标准错误为空、标准输出是单个 JSON 对象且 imported 为 2。
        path = self.write_jsonl("new-user.jsonl", NEW_USER_EVENTS)
        again = run_funnel("import", path, "--db", db)
        self.assertEqual(again.returncode, 0, msg=again.stderr)
        self.assertEqual(again.stderr, "")
        payload = json.loads(again.stdout)
        self.assertEqual(payload, {"imported": 2})

        # 此时恰有四条事件：种子两行 + new-user 两行。
        rows, count = self.snapshot_events(db)
        self.assertEqual(rows, EXPECTED_FOUR_ROWS)
        self.assertEqual(count, 4)

        # 两名用户各自 visit 后 60/30 秒内 signup：访问 2 人、转化 2 人、比例 1。
        self.assert_report(db, visit_users=2, converted_users=2, conversion_rate=1)


if __name__ == "__main__":
    unittest.main()
