"""Dry-run setup tests; no real Hermes configuration, models, or cron jobs are touched."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configure


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        plugin = self.home / "plugins" / "memory-reviewer"
        plugin.mkdir(parents=True)
        for filename in ("monitor.py", "review_prompt.txt"):
            shutil.copy2(ROOT / filename, plugin / filename)
        (self.home / "cron").mkdir()
        self.calls = []
        self.primary = {"provider": "openrouter", "default": "example/regular-model"}
        self.providers = {}
        self.environment = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.mock_hermes = mock.patch.object(configure, "hermes", side_effect=self.fake_hermes)
        self.mock_hermes.start()
        self.addCleanup(self.mock_hermes.stop)
        self.executable = mock.patch.object(configure.shutil, "which", return_value="/fake/hermes")
        self.executable.start()
        self.addCleanup(self.executable.stop)

    def args(self, provider=None, model=None, timezone=None, non_interactive=True):
        return types.SimpleNamespace(provider=provider, model=model,
                                     timezone=timezone, non_interactive=non_interactive)

    def read(self):
        return configure.jobs(self.home)

    def save(self, data):
        (self.home / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": data, "meta": {"keep": True}}), encoding="utf-8"
        )

    def fake_hermes(self, *argv, capture=False):
        self.calls.append(argv)
        if argv[:2] == ("config", "get"):
            value = {"model": self.primary, "providers": self.providers}.get(argv[2], None)
            return json.dumps(value)
        if argv[:2] in (("plugins", "enable"), ("config", "set")):
            return ""
        self.assertEqual(argv[0], "cron")
        cmd = argv[1]
        if cmd == "create":
            args = argv[4:]
            job = {"id": "new-review-id", "name": "unset", "enabled": False,
                   "schedule": argv[2], "prompt": argv[3], "timezone": "UTC"}
        elif cmd == "edit":
            rows = self.read()
            job = next(j for j in rows if j["id"] == argv[2])
            args = argv[3:]
        elif cmd in ("pause", "resume"):
            rows = self.read()
            match = next(j for j in rows if j["id"] == argv[2])
            match["enabled"] = (cmd == "resume")
            self.save(rows)
            return ""
        else:
            self.fail(f"Unexpected cron command: {cmd}")
        pairs = dict(zip(args[::2], args[1::2])) if cmd == "edit" else {}
        if cmd == "create":
            args = [a for a in args if a != "--paused"]
            pairs = dict(zip(args[::2], args[1::2]))
        updates = {"--name": "name", "--provider": "provider", "--model": "model",
                   "--prompt": "prompt", "--monitor-script": "monitor_script",
                   "--reasoning-effort": "reasoning_effort", "--timezone": "timezone"}
        for key, value in pairs.items():
            if key in updates:
                job[updates[key]] = value
        if cmd == "create":
            self.save(self.read() + [job])
        else:
            self.save(rows)
        return ""

    def test_new_install_custom_model_leaves_hermes_default_alone(self):
        configure.run(self.args("google", "gemini-example"))
        job = self.read()[0]
        self.assertEqual(job["name"], configure.JOB_NAME)
        self.assertEqual(job["provider"], "google")
        self.assertEqual(job["model"], "gemini-example")
        self.assertEqual(job["enabled_toolsets"], ["memory-reviewer", "no_mcp"])
        self.assertTrue(job["enabled"])
        self.assertEqual(job["schedule"], "* * * * *")
        self.assertEqual(job["timezone"], "UTC")
        self.assertEqual(self.primary["default"], "example/regular-model")
        self.assertEqual(job["monitor_script"], str(self.home / "scripts" / "memory-pending-monitor.py"))
        self.assertEqual([x for x in self.calls if x[:2] == ("cron", "resume")],
                         [("cron", "resume", "new-review-id")])

    def test_migrate_legacy_job_in_place_preserving_history_and_schedule(self):
        self.save([
            {"id": "original", "name": configure.LEGACY_NAME, "provider": "xai-oauth",
             "model": "old-grok", "enabled": True, "schedule": "*/2 * * * *",
             "timezone": "America/Edmonton", "enabled_toolsets": ["memory-reviewer", "no_mcp"]},
            {"id": "other", "name": "daily-news", "enabled": True, "schedule": "0 8 * * *"},
        ])
        configure.run(self.args("openrouter", "other-model"))
        rows = self.read()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "original")
        self.assertEqual(rows[0]["name"], configure.JOB_NAME)
        self.assertEqual(rows[0]["provider"], "openrouter")
        self.assertEqual(rows[0]["model"], "other-model")
        self.assertEqual(rows[0]["schedule"], "*/2 * * * *")
        self.assertEqual(rows[0]["timezone"], "America/Edmonton")
        self.assertTrue(rows[0]["enabled"])
        self.assertEqual(rows[1]["name"], "daily-news")
        self.assertNotIn(("cron", "create"), [x[:2] for x in self.calls])

    def test_existing_paused_job_remains_paused(self):
        self.save([{"id": "paused", "name": configure.JOB_NAME, "enabled": False,
                    "provider": "google", "model": "old-model", "schedule": "0 * * * *"}])
        configure.run(self.args("google", "new-model"))
        self.assertFalse(self.read()[0]["enabled"])
        self.assertEqual(self.read()[0]["model"], "new-model")
        self.assertFalse(any(x[1] in ("pause", "resume") for x in self.calls if x[0] == "cron"))

    def test_rerunning_setup_is_idempotent(self):
        configure.run(self.args("openrouter", "model-a"))
        configure.run(self.args("openrouter", "model-b"))
        self.assertEqual(len(self.read()), 1)
        self.assertEqual(self.read()[0]["id"], "new-review-id")
        self.assertEqual(self.read()[0]["model"], "model-b")

    def test_noninteractive_can_use_current_main_model(self):
        configure.run(self.args())
        self.assertEqual((self.read()[0]["provider"], self.read()[0]["model"]),
                         ("openrouter", "example/regular-model"))

    def test_invalid_model_does_not_change_memory_settings_or_jobs(self):
        with self.assertRaises(ValueError):
            configure.run(self.args("openrouter", "--bad model"))
        self.assertEqual(self.read(), [])
        self.assertFalse(any(x[:2] == ("config", "set") for x in self.calls))

    def test_unknown_provider_without_model_fails_before_writing(self):
        with self.assertRaises(ValueError):
            configure.run(self.args("unknown-provider"))
        self.assertEqual(self.read(), [])
        self.assertFalse(any(x[:2] == ("cron", "create") for x in self.calls))

    def test_failed_update_restores_previously_active_job(self):
        self.save([{"id": "old-job", "name": configure.LEGACY_NAME,
                    "enabled": True, "provider": "xai-oauth",
                    "model": "old-model", "schedule": "* * * * *"}])
        original = self.fake_hermes

        def fail_edit(*argv, capture=False):
            if argv[:2] == ("cron", "edit"):
                raise __import__("subprocess").CalledProcessError(1, ["hermes", *argv])
            return original(*argv, capture=capture)

        with mock.patch.object(configure, "hermes", side_effect=fail_edit):
            with self.assertRaises(__import__("subprocess").CalledProcessError):
                configure.run(self.args("google", "new-model"))
        self.assertTrue(self.read()[0]["enabled"])
        self.assertEqual(self.read()[0]["name"], configure.LEGACY_NAME)
        self.assertEqual(self.read()[0]["model"], "old-model")

    def test_duplicate_legacy_and_generic_jobs_are_refused(self):
        self.save([{"id": "a", "name": configure.JOB_NAME},
                   {"id": "b", "name": configure.LEGACY_NAME}])
        with self.assertRaises(RuntimeError):
            configure.run(self.args("google", "model"))
        self.assertFalse(any(x[:2] == ("cron", "edit") for x in self.calls))

    def test_interactive_picker_uses_configured_provider_model(self):
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        with mock.patch.object(configure.sys, "stdin", fake_stdin), \
             mock.patch("builtins.input", side_effect=["1", "1", "y"]):
            configure.run(self.args(non_interactive=False))
        self.assertEqual(self.read()[0]["provider"], "openrouter")
        self.assertEqual(self.read()[0]["model"], "example/regular-model")

    def test_tool_is_restricted_to_new_or_legacy_review_job(self):
        fake_const = types.ModuleType("hermes_constants")
        fake_const.get_hermes_home = lambda: self.home
        cron_package = types.ModuleType("cron")
        cron_package.__path__ = []
        identity_module = types.ModuleType("cron.execution_identity")
        identity_module.current_cron_execution = lambda: types.SimpleNamespace(job_name=configure.JOB_NAME)
        with mock.patch.dict(sys.modules, {"hermes_constants": fake_const, "cron": cron_package,
                                           "cron.execution_identity": identity_module}):
            spec = importlib.util.spec_from_file_location("memory_review_plugin_test", ROOT / "__init__.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertTrue(mod._authorized())
            identity_module.current_cron_execution = lambda: types.SimpleNamespace(job_name=configure.LEGACY_NAME)
            self.assertTrue(mod._authorized())
            identity_module.current_cron_execution = lambda: types.SimpleNamespace(job_name="unrelated")
            self.assertFalse(mod._authorized())
            identity_module.current_cron_execution = lambda: None
            self.assertFalse(mod._authorized())


if __name__ == "__main__":
    unittest.main()
