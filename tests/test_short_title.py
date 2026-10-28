"""短标题的格式、配置路由和异步写入边界回归测试。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_title import FakeBackend, ID
import oil_codex_title as app
import codex_adapter

ROOT = Path(__file__).resolve().parents[1]


class ShortTitleTests(unittest.TestCase):
    def candidate(self, text="用户问卷展示规则"):
        return {"action": "rename", "title": text, "reason": "对象与目标明确"}

    def test_accepts_objects_and_rejects_decoration_and_private_data(self):
        for text in ("用户问卷展示规则", "Codex 侧栏整理", "短信验证码参数核对", "JSON 格式化", "url-watch 日志保留"):
            self.assertEqual(app.validate_candidate(self.candidate(text), "", "short")["title"], text)
        for text in ("🧩 问卷展示", "问卷｜展示", "问卷 | 展示", "问卷：展示", "问卷 — 展示", "问卷 - 展示", "问卷 / 展示", "① 问卷展示", "问卷展示\n", "a" * 33, "https://example.com", "user@example.com"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                app.validate_candidate(self.candidate(text), "", "short")

    def test_style_routes_prompt_and_invalidates_fingerprint(self):
        context = {"original_goal": "请修复购物车计算错误", "current_title": "待整理"}
        config = {**app.DEFAULTS, "title_style": "short"}
        with patch.object(codex_adapter, "generate_json", return_value=(self.candidate(), {})) as model:
            codex_adapter._generate_title_once("codex", config, context, ROOT)
            self.assertEqual(model.call_args.args[3], ROOT / "prompts/naming-short.md")
        backend = FakeBackend()
        self.assertNotEqual(app.snapshot(backend.read(ID), config)["fingerprint"],
                            app.snapshot(backend.read(ID), app.DEFAULTS)["fingerprint"])

    def test_write_readback_and_lock_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {**app.DEFAULTS, "title_style": "short"}
            app.atomic_json(root / "config.json", config)
            backend = FakeBackend()
            result = app.process_thread(backend, lambda _: (self.candidate(), {}), ID, root, config, apply=True)
            self.assertEqual(result["status"], "renamed")
            self.assertEqual(backend.read(ID)["name"], "用户问卷展示规则")
            app.atomic_json(app.state_path(root, ID), {"locked": True, "lock_reason": "用户设置"})
            result = app.process_thread(backend, lambda _: self.fail("锁定后不得调用模型"), ID, root, config, apply=True)
            self.assertEqual(result["status"], "locked")

    def test_style_change_during_generation_discards_old_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {**app.DEFAULTS, "title_style": "short"}
            app.atomic_json(root / "config.json", config)
            backend = FakeBackend()
            def generate(_):
                app.atomic_json(root / "config.json", {"title_style": "classic"})
                return self.candidate(), {}
            result = app.process_thread(backend, generate, ID, root, config, apply=True)
            self.assertEqual(result["status"], "stale_result")
            self.assertEqual(backend.writes, [])

    def test_cli_configuration_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app.atomic_json(root / "config.json", {"model": "existing-model", "enabled": False})
            result = subprocess.run([sys.executable, str(ROOT / "scripts/oil_codex_title.py"), "configure", "--title-style", "short"],
                                    env={**os.environ, "OIL_CODEX_TITLE_DATA": tmp}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(result.stdout)
            self.assertEqual(config["title_style"], "short")
            self.assertEqual(config["model"], "existing-model")
            self.assertFalse(config["enabled"])
            app.atomic_json(root / "config.json", {"title_style": "invalid"})
            with self.assertRaises(ValueError):
                app.load_config(root)
