"""时间戳与访问边界只接受 ASCII 数字的回归测试。

固定公开约定（在既有 JSONL 导入与访问时段筛选之上的一次收紧）：

- ``timestamp`` 与 ``--visit-from`` / ``--visit-before`` 的所有数字位只
  接受 ASCII 数字 0-9，格式仍为固定的 ``YYYY-MM-DDTHH:MM:SS``，统一视为
  UTC，日历有效性沿用既有 strptime 校验
- 全角数字（２０２６）、阿拉伯印度数字（٢٠٢٦）与混排数字（２026）一律
  拒绝：不自动转换、不裁剪、不补零；非 ASCII 数字出现在年以外的数字位
  （月、日、时、分、秒）同样拒绝
- 导入侧：退出码 2、标准输出为空、标准错误给出输入路径、首个错误的
  **物理行号**、字段名 timestamp 与"只接受 ASCII 数字"的原因，不输出
  异常堆栈；空白行照常计入行号，合法前缀不入库，已有记录不变，数据库
  原本不存在时不创建文件；更早行的 JSON、字段或 UTF-8 错误仍优先报告
- 报告侧：单个访问边界含非 ASCII 数字即退出码 2、标准输出为空，标准
  错误指出对应参数与 ASCII 数字要求，且在数据库访问前拒绝——数据库不
  存在时不创建，即使带 --output 也不创建或改写报告文件
- 合法对照不受影响：闰日与跨月时间逐字入库，``{"imported": 3}``，
  --within-seconds 1 的报告仍为访问 2 人、转化 1 人、比例 0.5；
  user_id 中的 Unicode 字符按原值保留，额外事件字段继续忽略；
  时区后缀、小数秒与无效日期仍拒绝

只通过公开命令 ``python -m funnel import|report`` 观察行为。期望值全部
来自固定输入事件本身。仅使用 Python 3 标准库；每个场景在独立临时目录
中自备输入并在测试结束时清理，全部用户编号均为虚构。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 合法对照样例：u1 在闰日 2024-02-29 末尾访问、跨月瞬间注册（间隔恰 1 秒，
# 在 --within-seconds 1 窗口内转化）；u2 仅在同一闰日中午访问。
VALID_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2024-02-29T23:59:59"},
    {"user_id": "u1", "event": "signup", "timestamp": "2024-03-01T00:00:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2024-02-29T12:00:00"},
]

EXPECTED_VALID_ROWS = [
    ("u1", "signup", "2024-03-01T00:00:00"),
    ("u1", "visit", "2024-02-29T23:59:59"),
    ("u2", "visit", "2024-02-29T12:00:00"),
]
EXPECTED_REPORT_WITHIN_1 = {
    "visit_users": 2,
    "converted_users": 1,
    "conversion_rate": 0.5,
}

# 拒绝场景固定骨架（物理行号）：
#   第 1 行：合法访问（失败时不得留存）
#   第 2 行：空白行（不导入，但占据一个物理行号）
#   第 3 行：u2 的 visit，timestamp 含非 ASCII 数字（首个错误必须在此行）
#   第 4 行：非法 JSON（不得被报告）
FIRST_LINE_VISIT = {"user_id": "fresh-user", "event": "visit",
                    "timestamp": "2024-03-01T10:00:00"}
FOURTH_LINE_INVALID_JSON = "{"

# 三类任务点名的非 ASCII 年份，以及非 ASCII 数字出现在年以外数字位的样例。
# 每个值在形态上恰好 19 个字符，strptime 的 %Y 等本会把这些数字当合法
# 数字接受——必须由显式 ASCII 字符类挡下。
BAD_TIMESTAMPS = [
    "２０２６-10-06T10:00:00",  # 全角数字年份
    "٢٠٢٦-10-06T10:00:00",      # 阿拉伯印度数字年份
    "２026-10-06T10:00:00",      # 全角与 ASCII 混排年份
    "2026-1０-06T10:00:00",      # 非 ASCII 数字出现在月份位
    "2026-10-06T1０:00:00",      # 非 ASCII 数字出现在小时位
    "2026-10-06T10:0０:00",      # 非 ASCII 数字出现在分钟位
]

# 收紧后仍必须拒绝的既有非法形态（与非 ASCII 数字走同一条形态校验）。
STILL_BAD_TIMESTAMPS = [
    "2026-10-06T10:00:00Z",      # 时区后缀
    "2026-10-06T10:00:00.5",     # 小数秒
    "2026-02-29T10:00:00",       # 形态合法但日历无效（2026 非闰年）
]

# 报告边界用的合法起终点（UTC，含起点、不含终点）。
BOUND_FROM = "2024-02-29T00:00:00"
BOUND_BEFORE = "2024-03-02T00:00:00"


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


class AsciiDigitTimestampTests(unittest.TestCase):
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

    def write_bad_input(self, name, bad_timestamp):
        """按固定骨架写 bad.jsonl：合法访问、空白行、第 3 行待拒绝 visit、
        第 4 行非法 JSON，共四个物理行。"""
        bad_record = {"user_id": "u2", "event": "visit",
                      "timestamp": bad_timestamp}
        lines = [
            json.dumps(FIRST_LINE_VISIT, ensure_ascii=False),
            "",
            json.dumps(bad_record, ensure_ascii=False),
            FOURTH_LINE_INVALID_JSON,
        ]
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def seed_valid_events(self, db):
        """通过公开 import 命令建立三条合法记录的库。"""
        path = self.write_jsonl("seed-valid.jsonl", VALID_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode, 0,
            msg="合法样例导入失败:\nstdout=%r\nstderr=%r"
            % (result.stdout, result.stderr),
        )
        return db

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    def assert_import_rejected(self, path, result):
        """导入拒绝协议：退出码 2、空 stdout、路径/第 3 行/timestamp/ASCII
        原因，不越过首个错误、不输出堆栈。"""
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 3 行", result.stderr)
        self.assertIn("timestamp", result.stderr)
        self.assertIn("ASCII", result.stderr)
        self.assertNotIn("第 4 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # -- 合法对照：闰日/跨月逐字入库，within-seconds 1 下 2/1/0.5 ----------

    def test_valid_control_imports_three_and_reports_within_one_second(self):
        db = self.db_path()
        path = self.write_jsonl("valid.jsonl", VALID_EVENTS)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), ['{"imported": 3}'])
        self.assertEqual(self.snapshot_events(db), EXPECTED_VALID_ROWS)

        # u1 的 visit->signup 间隔恰 1 秒，窗口上界含等值：仍计转化。
        report = run_funnel(
            "report", "--db", db, "--within-seconds", "1"
        )
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        self.assertEqual(report.stderr, "")
        self.assertEqual(
            json.loads(report.stdout), EXPECTED_REPORT_WITHIN_1
        )

    # -- 导入拒绝：非 ASCII 数字，新建库与已有库两种情形 ------------------

    def test_import_rejects_non_ascii_digits_without_creating_db(self):
        for bad_ts in BAD_TIMESTAMPS:
            with self.subTest(bad_ts=bad_ts):
                # 每个子场景使用独立文件名，临时目录在整个测试方法内复用，
                # 因此逐一核对本场景产生的两个路径。
                tag = str(BAD_TIMESTAMPS.index(bad_ts))
                path = self.write_bad_input("bad-%s.jsonl" % tag, bad_ts)
                fresh_db = self.db_path("fresh-%s.sqlite" % tag)
                self.assertFalse(os.path.exists(fresh_db))

                result = run_funnel("import", path, "--db", fresh_db)
                self.assert_import_rejected(path, result)

                # 数据库文件不创建，合法前缀（第 1 行）不留存：临时目录中
                # 只应有本场景输入与此前子场景/本方法留下的输入 JSONL，
                # 不应出现任何 .sqlite 文件。
                self.assertFalse(os.path.exists(fresh_db))
                self.assertTrue(
                    all(not name.endswith(".sqlite")
                        for name in os.listdir(self.tmpdir))
                )

    def test_import_rejection_leaves_existing_records_unchanged(self):
        db = self.seed_valid_events(self.db_path("seeded.sqlite"))
        before = self.snapshot_events(db)
        self.assertEqual(before, EXPECTED_VALID_ROWS)

        for bad_ts in BAD_TIMESTAMPS[:3]:  # 任务点名的全角/阿拉伯印度/混排
            with self.subTest(bad_ts=bad_ts):
                path = self.write_bad_input(
                    "bad-seeded-%d.jsonl" % BAD_TIMESTAMPS.index(bad_ts),
                    bad_ts,
                )
                result = run_funnel("import", path, "--db", db)
                self.assert_import_rejected(path, result)
                # 已有三条记录逐行不变；第 1 行合法前缀与第 3 行 u2 都不留存
                # （u2 已在合法对照库中，按三元组快照核对即可）。
                self.assertEqual(self.snapshot_events(db), before)

    def test_other_shape_and_calendar_errors_still_rejected(self):
        # 时区后缀、小数秒、无效日期沿用同一形态/日历校验，仍定位第 3 行。
        for bad_ts in STILL_BAD_TIMESTAMPS:
            with self.subTest(bad_ts=bad_ts):
                path = self.write_bad_input(
                    "still-bad-%d.jsonl" % STILL_BAD_TIMESTAMPS.index(bad_ts),
                    bad_ts,
                )
                fresh_db = self.db_path(
                    "still-bad-%d.sqlite" % STILL_BAD_TIMESTAMPS.index(bad_ts)
                )
                result = run_funnel("import", path, "--db", fresh_db)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn(path, result.stderr)
                self.assertIn("第 3 行", result.stderr)
                self.assertIn("timestamp", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(os.path.exists(fresh_db))

    # -- 更早行的 JSON / 字段 / UTF-8 错误优先于第 3 行 -------------------

    def test_earlier_invalid_json_reported_first(self):
        # 第 1 行即非法 JSON：即使第 3 行有非 ASCII 时间戳，也只报第 1 行。
        lines = [
            FOURTH_LINE_INVALID_JSON,
            "",
            json.dumps({"user_id": "u2", "event": "visit",
                        "timestamp": BAD_TIMESTAMPS[0]}, ensure_ascii=False),
        ]
        path = os.path.join(self.tmpdir, "earlier-json.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        fresh_db = self.db_path("earlier-json.sqlite")

        result = run_funnel("import", path, "--db", fresh_db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("第 1 行", result.stderr)
        self.assertIn("JSON", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)
        self.assertFalse(os.path.exists(fresh_db))

    def test_earlier_missing_field_reported_first(self):
        # 第 2 行（非空行）缺少 timestamp 字段：字段错误先于第 3 行报告。
        lines = [
            json.dumps(FIRST_LINE_VISIT, ensure_ascii=False),
            json.dumps({"user_id": "u9", "event": "visit"},
                       ensure_ascii=False),
            json.dumps({"user_id": "u2", "event": "visit",
                        "timestamp": BAD_TIMESTAMPS[0]}, ensure_ascii=False),
        ]
        path = os.path.join(self.tmpdir, "earlier-field.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        fresh_db = self.db_path("earlier-field.sqlite")

        result = run_funnel("import", path, "--db", fresh_db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("第 2 行", result.stderr)
        self.assertIn("缺少", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)
        self.assertFalse(os.path.exists(fresh_db))

    def test_earlier_invalid_utf8_reported_first(self):
        # 第 2 行字节不是合法 UTF-8：编码错误先于第 3 行的非 ASCII 时间戳。
        line1 = json.dumps(FIRST_LINE_VISIT, ensure_ascii=False).encode("utf-8")
        line3 = json.dumps({"user_id": "u2", "event": "visit",
                            "timestamp": BAD_TIMESTAMPS[0]},
                           ensure_ascii=False).encode("utf-8")
        path = os.path.join(self.tmpdir, "earlier-utf8.jsonl")
        with open(path, "wb") as fh:
            fh.write(line1 + b"\n")
            fh.write(b"\xff\xfe\xfd\n")  # 非法 UTF-8 字节
            fh.write(line3 + b"\n")
        fresh_db = self.db_path("earlier-utf8.sqlite")

        result = run_funnel("import", path, "--db", fresh_db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("第 2 行", result.stderr)
        self.assertIn("UTF-8", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)
        self.assertFalse(os.path.exists(fresh_db))

    # -- 报告边界：任一参数含非 ASCII 数字即先于数据库拒绝 ----------------

    def test_report_bounds_reject_non_ascii_digits_before_db(self):
        for bad in BAD_TIMESTAMPS[:3]:  # 全角 / 阿拉伯印度 / 混排年份
            tag = BAD_TIMESTAMPS.index(bad)
            cases = [
                ("--visit-from", bad, BOUND_BEFORE),
                ("--visit-before", BOUND_FROM, bad),
            ]
            for param, visit_from, visit_before in cases:
                with self.subTest(param=param, bad=bad):
                    missing_db = self.db_path(
                        "missing-%s-%d.sqlite" % (param.strip("-"), tag)
                    )
                    self.assertFalse(os.path.exists(missing_db))
                    result = run_funnel(
                        "report", "--db", missing_db,
                        "--visit-from", visit_from,
                        "--visit-before", visit_before,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    # 标准错误指出出问题的参数与 ASCII 数字要求，无堆栈。
                    self.assertIn(param, result.stderr)
                    self.assertIn("ASCII", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    # 拒绝发生在数据库访问前：不存在的库不被创建。
                    self.assertFalse(os.path.exists(missing_db))

    def test_report_bound_error_does_not_touch_output_file(self):
        bad = BAD_TIMESTAMPS[0]
        missing_db = self.db_path("missing-output.sqlite")

        # (1) --output 目标原本不存在：不创建报告文件，也不创建数据库。
        out_absent = self.db_path("report-absent.json")
        result = run_funnel(
            "report", "--db", missing_db,
            "--visit-from", bad, "--visit-before", BOUND_BEFORE,
            "--output", out_absent,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--visit-from", result.stderr)
        self.assertIn("ASCII", result.stderr)
        self.assertFalse(os.path.exists(out_absent))
        self.assertFalse(os.path.exists(missing_db))

        # (2) --output 目标已存在且有内容：原内容逐字节保留。
        out_existing = self.db_path("report-existing.json")
        marker = '{"marker": "do-not-touch"}\n'
        with open(out_existing, "w", encoding="utf-8") as fh:
            fh.write(marker)
        result = run_funnel(
            "report", "--db", missing_db,
            "--visit-from", BOUND_FROM, "--visit-before", bad,
            "--output", out_existing,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--visit-before", result.stderr)
        self.assertIn("ASCII", result.stderr)
        with open(out_existing, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), marker)
        self.assertFalse(os.path.exists(missing_db))

    def test_report_ascii_bound_still_accepted(self):
        # 合法 ASCII 边界不被误伤：合法对照库在段 [02-29, 03-02) 内
        # 访问 2 人、1 秒窗口下转化 1 人。
        db = self.seed_valid_events(self.db_path("seeded-window.sqlite"))
        result = run_funnel(
            "report", "--db", db,
            "--within-seconds", "1",
            "--visit-from", BOUND_FROM, "--visit-before", BOUND_BEFORE,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), EXPECTED_REPORT_WITHIN_1)

    # -- Unicode 用户编号保留原值；额外事件字段忽略 -----------------------

    def test_unicode_user_id_preserved_and_extra_fields_ignored(self):
        events = [
            {"user_id": "用户Ａ", "event": "visit",
             "timestamp": "2024-02-29T23:59:59", "note": "额外字段"},
            {"user_id": "用户Ａ", "event": "signup",
             "timestamp": "2024-03-01T00:00:00", "extra": [1, 2, 3]},
            {"user_id": "u2", "event": "visit",
             "timestamp": "2024-02-29T12:00:00"},
        ]
        db = self.db_path()
        path = self.write_jsonl("unicode-users.jsonl", events)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(result.stdout), {"imported": 3})

        # 用户编号按原值落库（不规范化），额外字段不产生额外列或记录。
        self.assertEqual(
            self.snapshot_events(db),
            [
                ("u2", "visit", "2024-02-29T12:00:00"),
                ("用户Ａ", "signup", "2024-03-01T00:00:00"),
                ("用户Ａ", "visit", "2024-02-29T23:59:59"),
            ],
        )

        # 编号按 Unicode 码点排序（'u' < '用'）；中文用户同样可转化。
        report = run_funnel(
            "report", "--db", db,
            "--within-seconds", "1", "--include-users",
        )
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        payload = json.loads(report.stdout)
        self.assertEqual(payload["visit_users"], 2)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 0.5)
        self.assertEqual(payload["visit_user_ids"], ["u2", "用户Ａ"])
        self.assertEqual(payload["converted_user_ids"], ["用户Ａ"])


if __name__ == "__main__":
    unittest.main()
