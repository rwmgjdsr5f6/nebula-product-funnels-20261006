"""SQLite 写入中途失败的导入原子性回归测试。

与 test_import_atomicity.py 的分工：后者固定**读取与逐行校验阶段**
（非法 JSON、缺字段、编码错误）的坏行整批拒绝；本模块专门制造一次
所有输入行均通过校验、但第三条记录在 **SQLite 写入阶段**被拒绝的
失败，固定以下公开约定：

- 写入阶段失败：退出码 2、标准输出为空、标准错误给出数据库路径、
  “数据库无法访问”与被拒绝原因；不输出异常堆栈
- 整批不写入：失败前后 events 表逐行一致（用户编号、事件、时间戳、
  每类记录的数量与总数量），同批中更早的合法记录也不得留下
- 失败不破坏数据库：随后的普通报告与正常追加导入行为不变
- 追加导入成功：退出码 0、标准错误为空、标准输出仅 ``{"imported": N}``

只通过公开命令 ``python -m funnel import|report`` 观察行为；仅使用
Python 3 标准库。每个场景使用独立临时目录中的 JSONL 与 SQLite，
测试结束不遗留任何文件，不依赖预存数据、第三方包或网络。

测试夹具说明：被拒原因与“只拒绝某个编号”的行为来自用例在**自己的
临时库**上安装的一个 SQLite 触发器（RAISE(ABORT)），它只是把存储层
故障注入到第三条写入上的手段，纯属测试夹具，不构成、也不应被理解为
产品对用户编号的任何新限制；产品源码与建表语句均不包含该触发器。
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

# 失败导入：三条记录全部通过读取与字段校验，第三条 blocked-user 的
# visit 在数据库写入时被夹具触发器拒绝（前两条必须随之一并回滚）。
BLOCKED_IMPORT_EVENTS = [
    {"user_id": "new-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "new-user", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "blocked-user", "event": "visit", "timestamp": "2026-10-06T10:02:00"},
]

# 失败之后用于证明仍可正常追加的合法文件：只有 new-user 的两条事件。
NEW_USER_ONLY_EVENTS = [
    {"user_id": "new-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "new-user", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

BLOCKED_USER_ID = "blocked-user"

# 存储层故障注入夹具：只拒绝 blocked-user 的插入，拒绝原因固定为
# blocked-user；其他用户（seed-user、new-user）的写入完全不受影响。
# 仅安装在本用例的临时库上，失败事务回滚不会删除该触发器。
REJECT_BLOCKED_USER_TRIGGER = """
CREATE TRIGGER reject_blocked_user_before_insert
BEFORE INSERT ON events
WHEN NEW.user_id = 'blocked-user'
BEGIN
    SELECT RAISE(ABORT, 'blocked-user');
END
"""

# 失败后数据库应当仍然只剩种子两行（按 user_id, event, timestamp 排序）。
EXPECTED_SEED_ROWS = [
    ("seed-user", "signup", "2026-10-06T10:01:00"),
    ("seed-user", "visit", "2026-10-06T10:00:00"),
]

# 追加两条 new-user 事件后恰好四行（同样按 user_id, event, timestamp
# 排序：signup 排在 visit 之前）。
EXPECTED_FOUR_ROWS = [
    ("new-user", "signup", "2026-10-06T10:00:30"),
    ("new-user", "visit", "2026-10-06T10:00:00"),
    ("seed-user", "signup", "2026-10-06T10:01:00"),
    ("seed-user", "visit", "2026-10-06T10:00:00"),
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

    def seed_database(self, db, jsonl_name):
        """先成功导入固定种子数据，返回导入命令结果。"""
        path = self.write_jsonl(jsonl_name, SEED_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="种子导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 2})
        return result

    def install_blocked_user_trigger(self, db):
        """在临时库上安装只拒绝 blocked-user 的夹具触发器。

        这是测试侧的存储层故障注入，不改动产品源码或其建表语句。
        """
        with sqlite3.connect(db) as conn:
            conn.execute(REJECT_BLOCKED_USER_TRIGGER)

    def snapshot_events(self, db):
        """返回 (逐行记录, 按用户/事件分类的数量, 总记录数)。

        逐行记录按 user_id、event、timestamp 排序，供失败前后逐行比对
        用户编号、事件与时间戳；分类数量固定每类记录条数，总数固定
        表规模，三者任何一项变化都视为发生了部分写入。
        """
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()
            grouped_counts = conn.execute(
                "SELECT user_id, event, COUNT(*) FROM events "
                "GROUP BY user_id, event ORDER BY user_id, event"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return rows, grouped_counts, total

    def assert_plain_report(self, db, visit_users, converted_users, conversion_rate):
        """普通报告（无任何可选参数）成功且只输出三个汇总字段。"""
        result = run_funnel("report", "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload,
            {
                "visit_users": visit_users,
                "converted_users": converted_users,
                "conversion_rate": conversion_rate,
            },
        )
        return result

    def prepare_failed_database(self, prefix):
        """构造“种子两行 + 夹具触发器 + 一次三行写入失败”后的数据库。

        返回 (db 路径, 失败导入的进程结果, 失败前快照)。
        """
        db = self.db_path(prefix + ".sqlite")
        self.seed_database(db, prefix + "-seed.jsonl")
        before = self.snapshot_events(db)
        self.install_blocked_user_trigger(db)

        path = self.write_jsonl(prefix + "-blocked.jsonl", BLOCKED_IMPORT_EVENTS)
        result = run_funnel("import", path, "--db", db)
        return db, result, before

    def assert_storage_failure_contract(self, db, result):
        """写入阶段失败的完整公开约定：退出码/输出/数据库路径/原因/无堆栈。"""
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # 标准错误必须给出数据库路径、“数据库无法访问”与固定拒绝原因，
        # 且不得吐出异常堆栈（输入文件路径不做要求：这是数据库错误而非坏行）。
        self.assertIn(db, result.stderr)
        self.assertIn("数据库无法访问", result.stderr)
        self.assertIn(BLOCKED_USER_ID, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # -- 第三条记录写入被拒：整批回滚，库中只剩种子两行 ------------------

    def test_third_row_storage_failure_rolls_back_entire_batch(self):
        db, result, before = self.prepare_failed_database("atomic")
        self.assert_storage_failure_contract(db, result)

        # 整批不写入：逐行记录、每类记录数量、总数量与失败前完全一致。
        after_rows, after_grouped, after_total = self.snapshot_events(db)
        before_rows, before_grouped, before_total = before
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(after_grouped, before_grouped)
        self.assertEqual(after_total, before_total)

        # 库中仍只有两条种子事件；同批前两条 new-user 合法事件与第三条
        # blocked-user 事件都不得留下。
        self.assertEqual(after_rows, EXPECTED_SEED_ROWS)
        self.assertEqual(after_total, 2)
        self.assertFalse(
            any(row[0] in ("new-user", BLOCKED_USER_ID) for row in after_rows),
            msg="失败的写入不得留下 new-user/blocked-user 记录: %r" % (after_rows,),
        )

        # 失败不破坏数据库：普通报告仍成功，只剩种子用户的访问 1、转化 1、
        # 比例 1，且输出仅含这三个汇总字段。
        self.assert_plain_report(db, 1, 1, 1)

    # -- 失败后保留夹具约束，仍可正常追加合法导入 ------------------------

    def test_valid_import_appends_after_storage_failure(self):
        db, failed, _ = self.prepare_failed_database("repair")
        # 前置状态：先确实发生过一次写入阶段失败，且夹具约束仍在库中。
        self.assert_storage_failure_contract(db, failed)
        with sqlite3.connect(db) as conn:
            trigger = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'reject_blocked_user_before_insert'"
            ).fetchone()
        self.assertIsNotNone(trigger, msg="追加前夹具触发器应当仍然保留在测试库中")

        # 在同一个失败后的数据库中导入只含两条 new-user 事件的合法文件，
        # 约束保留：导入成功、标准错误为空、标准输出为单个 JSON 对象，
        # imported 恰为 2。
        path = self.write_jsonl("repair-new-user.jsonl", NEW_USER_ONLY_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 2})
        # 标准输出只含这一个 JSON 对象（单行，无多余内容）。
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertTrue(result.stdout.endswith("\n"))

        # 此时恰有四条事件：两条种子 + 两条追加，blocked-user 仍不在库中。
        rows, grouped_counts, total = self.snapshot_events(db)
        self.assertEqual(rows, EXPECTED_FOUR_ROWS)
        self.assertEqual(total, 4)
        self.assertEqual(
            grouped_counts,
            [
                ("new-user", "signup", 1),
                ("new-user", "visit", 1),
                ("seed-user", "signup", 1),
                ("seed-user", "visit", 1),
            ],
        )
        self.assertFalse(
            any(row[0] == BLOCKED_USER_ID for row in rows),
            msg="夹具约束只拒绝 blocked-user，追加后库中仍不应出现该编号: %r" % (rows,),
        )

        # 普通报告：seed-user 与 new-user 各自访问并在之后注册，
        # 访问人数 2、转化人数 2、比例 1。
        self.assert_plain_report(db, 2, 2, 1)


if __name__ == "__main__":
    unittest.main()
