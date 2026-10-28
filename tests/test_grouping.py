"""自动归组的目录匹配、人工保护和官方写入边界。"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_title import FakeBackend, ID, TURN
import auto_group as group
import oil_codex_title as title


class GroupBackend(FakeBackend):
    def __init__(self, directory):
        super().__init__()
        self.thread.update(id=ID, cwd=str(directory / "shop"), createdAt=200, projectId=None, section=None)
        self.projects = [{"id": "project-a", "roots": [{"path": str(directory / "shop")}]}]
        self.sections = [{"id": "section-a", "name": "工具与流程"}]
        self.placements = []
        self.fail_after_move = False

    def call(self, method, params):
        if method == "project/list":
            return {"data": copy.deepcopy(self.projects)}
        if method == "threadSection/list":
            return {"data": copy.deepcopy(self.sections)}
        self.placements.append((method, params))
        if method == "thread/metadata/update":
            self.thread["projectId"] = params["projectId"]
        elif method == "thread/section/move":
            self.thread["section"] = {"id": params["sectionId"]}
        else:
            raise AssertionError(method)
        if self.fail_after_move:
            raise group.BackendError("模拟写入后断线")
        return {}


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {**title.DEFAULTS, "auto_group_since": 100}
        title.atomic_json(self.root / "config.json", self.config)
        self.backend = GroupBackend(self.root)

    def run_group(self, selector=None):
        return group.process_group(self.backend, "codex", ID, self.root, self.config, TURN,
                                   selector=selector or (lambda *args: self.fail("不应调用模型")))

    def test_project_path_and_readback_then_no_repeat_after_manual_removal(self):
        self.assertEqual(self.run_group()["status"], "grouped")
        self.assertEqual(self.backend.thread["projectId"], "project-a")
        self.backend.thread["projectId"] = None
        self.assertEqual(self.run_group()["status"], "already_checked")
        self.assertEqual(len(self.backend.placements), 1)

    def test_nested_project_longest_root_and_same_path_ambiguity(self):
        self.backend.projects.append({"id": "parent", "roots": [{"path": str(self.root)}]})
        self.assertEqual(group.matching_project(self.backend.thread["cwd"], self.backend.projects)["id"], "project-a")
        self.backend.projects.append({"id": "duplicate", "roots": self.backend.projects[0]["roots"]})
        self.assertEqual(self.run_group()["status"], "no_match")
        self.assertIsNone(group.matching_project(str(self.root / "shop-other"), self.backend.projects[:1]))

    def test_old_existing_placed_locked_archived_and_missing_created_skip(self):
        original = copy.deepcopy(self.backend.thread)
        for fields, expected in [({"createdAt": 100}, "historical"), ({"createdAt": None}, "historical"),
                                 ({"ephemeral": True}, "not_user_chat"),
                                 ({"source": {"subAgent": "review"}}, "not_user_chat"),
                                 ({"source": "exec"}, "not_user_chat"),
                                 ({"projectId": "manual"}, "already_placed"),
                                 ({"section": {"id": "pinned"}}, "already_placed"),
                                 ({"sectionEnteredAt": 123}, "already_placed")]:
            self.backend.thread = {**original, **fields}
            self.assertEqual(self.run_group()["status"], expected)
        self.backend.thread = original
        self.backend.archived = True
        self.assertEqual(self.run_group()["status"], "archived")
        self.backend.archived = False
        title.atomic_json(title.state_path(self.root, ID), {"locked": True})
        self.assertEqual(self.run_group()["status"], "locked")
        self.assertFalse(self.backend.placements)

    def test_section_choice_uses_official_move_and_no_match_does_not_move(self):
        self.backend.projects = []
        self.assertEqual(self.run_group(lambda *args: {"kind": "section", "id": "section-a"})["status"], "grouped")
        self.assertEqual(self.backend.placements[0][0], "thread/section/move")

    def test_unknown_section_and_pinned_choices_are_rejected(self):
        sections = [{"id": "pin", "name": "Pinned"}, *self.backend.sections]
        with patch.object(group, "generate_json", return_value=({"section_id": "pin"}, {})) as generate:
            with self.assertRaises(ValueError):
                group.choose_section("codex", self.config, {}, sections)
            choices = generate.call_args.args[2]["sections"]
            self.assertEqual([c["id"] for c in choices], ["section-a"])
        with patch.object(group, "generate_json", return_value=({"section_id": ""}, {})):
            self.assertIsNone(group.choose_section("codex", self.config, {}, sections))

    def test_manual_move_during_model_is_preserved(self):
        self.backend.projects = []
        def choose(*args):
            self.backend.thread["section"] = {"id": "manual"}
            return {"kind": "section", "id": "section-a"}
        self.assertEqual(self.run_group(choose)["status"], "already_placed")
        self.assertFalse(self.backend.placements)

    def test_disable_during_model_prevents_move(self):
        self.backend.projects = []
        def choose(*args):
            title.atomic_json(self.root / "config.json", {**self.config, "auto_group_since": 0})
            return {"kind": "section", "id": "section-a"}
        self.assertEqual(self.run_group(choose)["status"], "disabled")
        self.assertFalse(self.backend.placements)

    def test_new_turn_and_deleted_target_prevent_move(self):
        self.backend.projects = []
        def choose(*args):
            self.backend.thread["turns"][-1]["id"] = "new-turn"
            return {"kind": "section", "id": "section-a"}
        self.assertEqual(self.run_group(choose)["status"], "outdated_event")
        self.backend.thread["turns"][-1]["id"] = TURN
        def removed(*args):
            self.backend.sections = []
            return {"kind": "section", "id": "section-a"}
        self.assertEqual(self.run_group(removed)["status"], "target_removed")
        self.assertFalse(self.backend.placements)

    def test_uncertain_write_is_not_retried(self):
        self.backend.fail_after_move = True
        with self.assertRaises(group.BackendError):
            self.run_group()
        self.backend.thread["projectId"] = None
        self.assertEqual(self.run_group()["status"], "already_checked")
        self.assertEqual(len(self.backend.placements), 1)

    def test_paginated_list_and_repeated_cursor(self):
        with patch.object(self.backend, "call", side_effect=[{"data": [1], "nextCursor": "next"}, {"data": [2]}]):
            self.assertEqual(group.list_all(self.backend, "project/list"), [1, 2])
        with patch.object(self.backend, "call", return_value={"data": [], "nextCursor": "next"}):
            with self.assertRaises(group.BackendError):
                group.list_all(self.backend, "project/list")

    def test_configuration_is_idempotent_and_off_preserves_title_style(self):
        script = Path(title.__file__)
        for flag, expected in [("on", 100), ("off", 0)]:
            result = subprocess.run([sys.executable, str(script), "configure", "--auto-group", flag],
                                    env={**os.environ, "OIL_CODEX_TITLE_DATA": str(self.root)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout)
            config = json.loads(result.stdout)
            self.assertEqual(config["auto_group_since"], expected)
            self.assertEqual(config["title_style"], self.config["title_style"])

    def test_empty_decision_is_recorded_without_a_write(self):
        self.backend.projects = []
        self.assertEqual(self.run_group(lambda *args: None)["status"], "no_match")
        self.assertEqual(self.run_group()["status"], "already_checked")
        self.assertFalse(self.backend.placements)
