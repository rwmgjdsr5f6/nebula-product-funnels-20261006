"""时间戳与访问边界只接受 ASCII 数字的回归测试。

固定公开约定（本次修正：非 ASCII 年份数字曾被当作合法时间戳接受）：

- timestamp、--visit-from、--visit-before 的数字范围统一：格式仍为
  ``YYYY-MM-DDTHH:MM:SS``、统一视为 UTC、日历有效性沿用既有要求，但
  **所有数字位置只接受 ASCII 0 到 9**。全角数字（２０２６）、阿拉伯印度
  数字（٢٠٢٦）与混排数字（２026）一律拒绝，不自动转换、裁剪或补零
- 导入遇到含非 ASCII 数字的 timestamp：退出码 2、标准输出为空，标准错误
  给出输入路径、首个错误的**物理行号**、timestamp 及“只接受 ASCII 数字”
  的原因，不输出异常堆栈；空白行照常计入行号，合法前缀不入库，已有记录
  不变，数据库原本不存在时不创建文件。更早行的非法 JSON、字段错误或
  UTF-8 错误仍优先报告
- report 的单个访问边界含非 ASCII 数字时同样退出码 2、标准输出为空，
  标准错误指出对应参数（--visit-from / --visit-before）与 ASCII 数字要求，
  在数据库访问前拒绝（argparse 阶段）；即使带 --output，也不创建或改写
  报告文件、不创建数据库
- 合法对照不受影响：u1 在 2024-02-29T23:59:59 访问、2024-03-01T00:00:00
  注册，u2 仅在 2024-02-29T12:00:00 访问，导入 ``{"imported": 3}``，
  --within-seconds 1 的报告仍为访问 2 人、转化 1 人、比例 0.5；
  user_id 中的 Unicode 字符按原值保留，额外事件字段继续忽略

只通过公开命令 ``python -m funnel import|report`` 观察行为，不直接调用
任何内部校验函数。仅使用 Python 3 标准库；每个场景在独立临时目录中自备
输入并在测试结束时清理，全部用户编号均为虚构，不依赖预存数据、第三方包
或网络。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 合法对照（闰日 + 跨月瞬间，间隔 1 秒）：三行逐字固定。
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
EXPECTED_REPORT = {
    "visit_users": 2,
    "converted_users": 1,
    "conversion_rate": 0.5,
}

# 三种“年份”写法：全角、阿拉伯印度、混排（首个为全角其余为 ASCII）。
# 接上 "-10-06T10:00:00" 后形态与合法串等长，专门针对数字范围被放宽的缺陷。
NON_ASCII_YEARS = [
    ("fullwidth", "２０２６"),
    ("arabic_indic", "٢٠٢٦"),
    ("mixed", "２026"),
]
BAD_TIMESTAMP_SUFFIX = "-10-06T10:00:00"

# 合法窗口（report 参数用）。
WINDOW_FROM = "2026-10-06T10:00:00"
WINDOW_BEFORE = "2026-10-06T11:00:00"

# 时间串中六组两位/四位数字的起始下标：年、月、日、时、分、秒。
# 形态 "YYYY-MM-DDTHH:MM:SS"。
DIGIT_GROUP_START = (0, 5, 8, 11, 14, 17)
LEGAL_SHAPE = "2024-03-01T10:00:00"


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


class AsciiTimestampDigitTests(unittest.TestCase):
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

    def write_bytes(self, name, raw_lines):
        """按物理行写入原始字节（每行已自带 LF），用于构造非法 UTF-8 行。"""
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as fh:
            for raw in raw_lines:
                fh.write(raw)
        return path

    def snapshot_events(self, db):
        # 显式 close：with conn 只管事务不管关闭，避免残留连接在后续测试的
        # GC 时输出 ResourceWarning。
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        return rows, count

    def seed_valid_events(self, db):
        path = self.write_jsonl("seed-valid.jsonl", VALID_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode, 0,
            msg="合法对照导入失败:\n%r\n%r" % (result.stdout, result.stderr),
        )
        return db

    # -- 合法对照：导入 3 条，--within-seconds 1 报告 2/1/0.5 -------------

    def test_legal_control_imports_and_reports(self):
        db = self.db_path()
        path = self.write_jsonl("good.jsonl", VALID_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 3})

        rows, count = self.snapshot_events(db)
        self.assertEqual(rows, EXPECTED_VALID_ROWS)
        self.assertEqual(count, 3)

        report = run_funnel("report", "--db", db, "--within-seconds", "1")
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        self.assertEqual(json.loads(report.stdout), EXPECTED_REPORT)

    def test_unicode_user_id_preserved_and_extra_fields_ignored(self):
        # user_id 中的非 ASCII 字符按原值保留；额外字段（含非 ASCII 值）忽略。
        event = {
            "user_id": "用户Ａ",
            "event": "visit",
            "timestamp": "2024-02-29T12:00:00",
            "note": {"任意": "额外字段", "n": 1},
        }
        db = self.db_path()
        path = self.write_jsonl("unicode-user.jsonl", [event])
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(result.stdout), {"imported": 1})
        rows, _ = self.snapshot_events(db)
        self.assertEqual(rows, [("用户Ａ", "visit", "2024-02-29T12:00:00")])

    # -- 导入：三种非 ASCII 年份一律拒绝（全角/阿拉伯印度/混排） ---------

    def test_bad_year_import_cases(self):
        for name, bad_year in NON_ASCII_YEARS:
            with self.subTest(case=name):
                # 每个子场景独立临时目录，避免文件名与库快照互相干扰。
                with tempfile.TemporaryDirectory() as sub:
                    self._run_bad_year_in_dir(sub, name, bad_year)

    def _run_bad_year_in_dir(self, sub, name, bad_year):
        bad_timestamp = bad_year + BAD_TIMESTAMP_SUFFIX
        lines = [
            json.dumps(VALID_EVENTS[0], ensure_ascii=False),
            "",
            json.dumps(
                {"user_id": "u2", "event": "visit", "timestamp": bad_timestamp},
                ensure_ascii=False,
            ),
            "{",
        ]
        path = os.path.join(sub, "bad.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        # 不存在的库：拒绝后目录中只剩输入文件。
        fresh_db = os.path.join(sub, "fresh.sqlite")
        result = run_funnel("import", path, "--db", fresh_db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 3 行", result.stderr)
        self.assertIn("timestamp", result.stderr)
        self.assertIn("ASCII", result.stderr)
        self.assertNotIn("第 4 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(fresh_db))
        self.assertEqual(sorted(os.listdir(sub)), ["bad.jsonl"])

        # 已有合法库：逐行不变。
        good = os.path.join(sub, "good.jsonl")
        with open(good, "w", encoding="utf-8") as fh:
            for event in VALID_EVENTS:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        seeded = os.path.join(sub, "seeded.sqlite")
        seed = run_funnel("import", good, "--db", seeded)
        self.assertEqual(seed.returncode, 0, msg=seed.stderr)
        conn = sqlite3.connect(seeded)
        try:
            before = conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()
        finally:
            conn.close()
        result = run_funnel("import", path, "--db", seeded)
        self.assertEqual(result.returncode, 2)
        self.assertIn("第 3 行", result.stderr)
        conn = sqlite3.connect(seeded)
        try:
            after = conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(after, before)
        self.assertEqual(after, EXPECTED_VALID_ROWS)

    # -- 导入：年/月/日/时/分/秒任一数字位置为全角都拒绝 -----------------

    def test_every_digit_position_must_be_ascii(self):
        for start in DIGIT_GROUP_START:
            chars = list(LEGAL_SHAPE)
            chars[start] = "６"  # 用全角 6 替换该组首位数字
            bad_timestamp = "".join(chars)
            with self.subTest(position=start, timestamp=bad_timestamp):
                with tempfile.TemporaryDirectory() as sub:
                    lines = [
                        json.dumps(VALID_EVENTS[0], ensure_ascii=False),
                        "",
                        json.dumps(
                            {"user_id": "u2", "event": "visit",
                             "timestamp": bad_timestamp},
                            ensure_ascii=False,
                        ),
                    ]
                    path = os.path.join(sub, "bad-pos.jsonl")
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(lines) + "\n")
                    db = os.path.join(sub, "fresh.sqlite")
                    result = run_funnel("import", path, "--db", db)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("第 3 行", result.stderr)
                    self.assertIn("ASCII", result.stderr)
                    self.assertFalse(os.path.exists(db))

    # -- 导入：更早行的 JSON / 字段 / UTF-8 错误优先报告 ------------------

    def test_earlier_invalid_json_reported_first(self):
        # 第 1 行即非法 JSON：必须报告第 1 行，而非第 3 行的非 ASCII 时间戳。
        path = self.write_bytes(
            "earlier-json.jsonl",
            [
                b"{\n",
                b"\n",
                json.dumps(
                    {"user_id": "u2", "event": "visit",
                     "timestamp": "２０２６" + BAD_TIMESTAMP_SUFFIX},
                    ensure_ascii=False,
                ).encode("utf-8") + b"\n",
            ],
        )
        result = run_funnel("import", path, "--db", self.db_path("x.sqlite"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("第 1 行", result.stderr)
        self.assertIn("JSON", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_earlier_field_error_reported_first(self):
        # 第 1 行 JSON 合法但缺字段，第 3 行时间戳含非 ASCII 数字：先报第 1 行。
        path = os.path.join(self.tmpdir, "earlier-field.jsonl")
        lines = [
            json.dumps({"user_id": "u9", "event": "visit"}, ensure_ascii=False),
            "",
            json.dumps(
                {"user_id": "u2", "event": "visit",
                 "timestamp": "２０２６" + BAD_TIMESTAMP_SUFFIX},
                ensure_ascii=False,
            ),
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        result = run_funnel("import", path, "--db", self.db_path("x.sqlite"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("第 1 行", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_earlier_utf8_error_reported_first(self):
        # 第 2 行字节不是合法 UTF-8，第 3 行时间戳含非 ASCII 数字：先报第 2 行。
        path = self.write_bytes(
            "earlier-utf8.jsonl",
            [
                json.dumps(VALID_EVENTS[0], ensure_ascii=False).encode("utf-8")
                + b"\n",
                b"\xff\xfe\n",  # 第 2 行：非法 UTF-8 字节
                json.dumps(
                    {"user_id": "u2", "event": "visit",
                     "timestamp": "２０２６" + BAD_TIMESTAMP_SUFFIX},
                    ensure_ascii=False,
                ).encode("utf-8") + b"\n",
            ],
        )
        result = run_funnel("import", path, "--db", self.db_path("x.sqlite"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("第 2 行", result.stderr)
        self.assertIn("UTF-8", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # -- report：两个访问边界含非 ASCII 数字都在数据库访问前拒绝 ----------

    def test_non_ascii_visit_bounds_rejected_before_db_access(self):
        for name, bad_year in NON_ASCII_YEARS:
            bad_value = bad_year + BAD_TIMESTAMP_SUFFIX
            for side, kwargs in (
                ("from", {"visit_from": bad_value, "visit_before": WINDOW_BEFORE}),
                ("before", {"visit_from": WINDOW_FROM, "visit_before": bad_value}),
            ):
                with self.subTest(case=name, side=side):
                    with tempfile.TemporaryDirectory() as sub:
                        db = os.path.join(sub, "missing.sqlite")
                        output = os.path.join(sub, "report.json")
                        marker = "UNTOUCHED\n"
                        with open(output, "w", encoding="utf-8") as fh:
                            fh.write(marker)
                        cli = ["report", "--db", db,
                               "--visit-from", kwargs["visit_from"],
                               "--visit-before", kwargs["visit_before"],
                               "--output", output]
                        result = run_funnel(*cli)
                        self.assertEqual(result.returncode, 2)
                        self.assertEqual(result.stdout, "")
                        # 标准错误指出对应参数与 ASCII 数字要求，无异常堆栈。
                        self.assertIn("--visit-%s" % side, result.stderr)
                        self.assertIn("ASCII", result.stderr)
                        self.assertNotIn("Traceback", result.stderr)
                        # 拒绝发生在数据库访问前：不创建数据库，
                        # 即使带 --output 也不改写既有报告文件。
                        self.assertFalse(os.path.exists(db))
                        with open(output, encoding="utf-8") as fh:
                            self.assertEqual(fh.read(), marker)

    def test_non_ascii_bound_does_not_create_output_or_db(self):
        # --output 目标原本不存在、数据库也不存在：两者都不得被创建。
        with tempfile.TemporaryDirectory() as sub:
            db = os.path.join(sub, "missing.sqlite")
            output = os.path.join(sub, "report.json")
            result = run_funnel(
                "report", "--db", db,
                "--visit-from", "２０２６" + BAD_TIMESTAMP_SUFFIX,
                "--visit-before", WINDOW_BEFORE,
                "--output", output,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertFalse(os.path.exists(db))
            self.assertFalse(os.path.exists(output))

    def test_non_ascii_digit_in_other_bound_positions_rejected(self):
        # 年份以外的数字位置同样只接受 ASCII：月、秒各取一个代表，两侧都验。
        month_bad = "2026-１０-06T10:00:00"
        second_bad = "2026-10-06T10:00:００"
        for bad_value, side in (
            (month_bad, "from"),
            (month_bad, "before"),
            (second_bad, "from"),
            (second_bad, "before"),
        ):
            with self.subTest(value=bad_value, side=side):
                with tempfile.TemporaryDirectory() as sub:
                    db = os.path.join(sub, "missing.sqlite")
                    if side == "from":
                        bounds = (bad_value, WINDOW_BEFORE)
                    else:
                        bounds = (WINDOW_FROM, bad_value)
                    result = run_funnel(
                        "report", "--db", db,
                        "--visit-from", bounds[0],
                        "--visit-before", bounds[1],
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("--visit-%s" % side, result.stderr)
                    self.assertIn("ASCII", result.stderr)
                    self.assertFalse(os.path.exists(db))

    def test_non_ascii_bound_keeps_existing_database_unchanged(self):
        # 边界非法时连已有数据库也不触碰：记录逐行不变。
        db = self.seed_valid_events(self.db_path())
        before_rows, before_count = self.snapshot_events(db)
        result = run_funnel(
            "report", "--db", db,
            "--visit-from", "２０２６" + BAD_TIMESTAMP_SUFFIX,
            "--visit-before", WINDOW_BEFORE,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        after_rows, after_count = self.snapshot_events(db)
        self.assertEqual((after_rows, after_count), (before_rows, before_count))


if __name__ == "__main__":
    unittest.main()
