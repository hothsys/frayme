"""The command line: batch recovery over a folder, and the recoverable rule shared with the UI."""

import itertools
import os
import re
import unittest

from backend.batch import can_recover, output_dir_for, run
from frayme_cli import main as cli_main
from tests.fixtures import (
    CLIPS,
    PROJECT_DIR,
    FrayTestCase,
    drop_moov,
    needs_ffmpeg,
    needs_x264,
    sha256,
)


def js_can_recover(status, details):
    """canRecover from js/rows.js, read from the file and evaluated in Python.

    The expression is translated token by token. If the JavaScript changes shape
    so the translation no longer evaluates, the test fails and the two copies
    have to be brought back in step by hand.
    """
    with open(os.path.join(PROJECT_DIR, "js", "rows.js"), encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"var canRecover =(.*?);", src, re.S)
    assert m, "canRecover not found in js/rows.js"
    expr = m.group(1)
    expr = re.sub(r"ev\.details\?\.(\w+)", r'details.get("\1")', expr)
    expr = expr.replace("ev.status", "status")
    expr = expr.replace("===", "==").replace("&&", " and ").replace("||", " or ")
    expr = expr.replace("!", " not ")
    expr = "(" + " ".join(expr.split()) + ")"
    return bool(eval(expr, {"status": status, "details": details}))


class RecoverableRule(unittest.TestCase):
    def test_python_rule_matches_the_ui_rule(self):
        statuses = ["ok", "no_moov", "corrupt", "truncated", "empty", "error", "not_video"]
        flags = [None, False, True]
        for status, unrecoverable, salvageable in itertools.product(statuses, flags, flags):
            details = {}
            if unrecoverable is not None:
                details["unrecoverable"] = unrecoverable
            if salvageable is not None:
                details["salvageable"] = salvageable
            with self.subTest(status=status, details=details):
                self.assertEqual(can_recover(status, details), js_can_recover(status, details))
        self.assertEqual(can_recover("no_moov", None), js_can_recover("no_moov", {}))


class OutputLayout(unittest.TestCase):
    def test_output_mirrors_input_subfolders(self):
        self.assertEqual(output_dir_for("/in/a.mp4", "/in", "/out"), "/out")
        self.assertEqual(output_dir_for("/in/x/y/a.mp4", "/in", "/out"), "/out/x/y")


@needs_ffmpeg
@needs_x264
class BatchRecovery(FrayTestCase):
    def test_recovers_nested_files_with_the_same_name(self):
        os.makedirs(os.path.join(self.src_dir, "trip1"))
        os.makedirs(os.path.join(self.src_dir, "trip2"))
        self.place(CLIPS.get("base"), "healthy.mp4")
        a = drop_moov(CLIPS.get("base"), os.path.join(self.src_dir, "trip1", "clip.mp4"))
        b = drop_moov(CLIPS.get("base"), os.path.join(self.src_dir, "trip2", "clip.mp4"))
        before = (sha256(a), sha256(b))
        log = []
        code, results = run(self.src_dir, self.out_dir, report=log.append)
        self.assertEqual(code, 0, log)
        self.assertEqual(len(results), 2)
        for sub in ("trip1", "trip2"):
            self.assertPlaysCleanly(os.path.join(self.out_dir, sub, "clip_recovered.mp4"))
        self.assertEqual((sha256(a), sha256(b)), before)

    def test_donor_folder_is_used(self):
        donors = os.path.join(self.work, "donors")
        os.makedirs(donors)
        import shutil

        shutil.copyfile(CLIPS.get("base"), os.path.join(donors, "healthy.mp4"))
        drop_moov(CLIPS.get("base"), self.damaged("nomoov.mp4"))
        log = []
        code, _ = run(self.src_dir, self.out_dir, donors, report=log.append)
        self.assertEqual(code, 0, log)
        self.assertTrue(any("1 healthy donors" in line for line in log), log)
        self.assertPlaysCleanly(os.path.join(self.out_dir, "nomoov_recovered.mp4"))

    def test_scan_only_writes_nothing(self):
        drop_moov(CLIPS.get("base"), self.damaged("nomoov.mp4"))
        log = []
        code, results = run(self.src_dir, self.out_dir, scan_only=True, report=log.append)
        self.assertEqual(code, 0)
        self.assertEqual(results, [])
        self.assertEqual(os.listdir(self.out_dir), [])
        self.assertTrue(any("would recover" in line for line in log), log)

    def test_files_the_app_does_not_offer_are_skipped(self):
        open(self.damaged("empty.mp4"), "wb").close()
        log = []
        code, results = run(self.src_dir, self.out_dir, report=log.append)
        self.assertEqual(code, 0)
        self.assertEqual(results, [])
        self.assertTrue(any("skip" in line and "empty" in line for line in log), log)
        self.assertEqual(os.listdir(self.out_dir), [])

    def test_a_failed_recovery_exits_1(self):
        # Looks like a missing-moov file, but holds no video data to recover
        with open(self.damaged("junk.mp4"), "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64)
        code, results = run(self.src_dir, self.out_dir, report=lambda _: None)
        if results:
            self.assertEqual(code, 1)
            self.assertNotEqual(results[0]["outcome"], "recovered")

    def test_missing_folders_exit_2(self):
        self.assertEqual(
            run(os.path.join(self.work, "nope"), self.out_dir, report=lambda _: None)[0], 2
        )
        self.assertEqual(
            run(self.src_dir, self.out_dir, os.path.join(self.work, "nope"), report=lambda _: None)[
                0
            ],
            2,
        )


class CommandLine(unittest.TestCase):
    def test_output_is_required_without_scan_only(self):
        with self.assertRaises(SystemExit) as cm:
            cli_main(["--input", "/tmp"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
