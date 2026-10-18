"""Distribution-facing behavior without invoking a vendor CLI."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from my_agents_fleet.cli import main


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(list(args)), 0)
        return output.getvalue()

    def fresh_python(self, code, **environment):
        env = {k: v for k, v in os.environ.items()
               if k not in {"MY_AGENTS2_CONFIG", "MY_AGENTS2_MODELS_PATH", "XDG_CACHE_HOME"}}
        env.update(MY_AGENTS2_AUTO_REFRESH="0", PYTHONDONTWRITEBYTECODE="1")
        env.update(environment)
        result = subprocess.run([sys.executable, "-c", code], cwd=self.root,
                                env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_init_inspect_and_runs_without_starting_agent(self):
        destination = self.root / "fleet"
        with patch("subprocess.Popen", side_effect=AssertionError("CLI must remain offline")):
            self.invoke("init", str(destination))
            description = self.invoke("inspect", str(destination))
            result = json.loads(self.invoke("runs", str(destination), "--json"))
        self.assertIn("reviewer", description)
        self.assertIn("summarizer", description)
        self.assertEqual(result, {"stats": [], "failures": []})
        self.assertTrue((destination / "data" / "sample.md").is_file())
        self.assertTrue((destination / "state" / "fleet.db").is_file())

    def test_init_refuses_to_overwrite_existing_files(self):
        original = self.root / "keep.md"
        original.write_text("existing content", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["init", str(self.root)])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(original.read_text(encoding="utf-8"), "existing content")
        self.assertFalse((self.root / "fleet.json").exists())

    def test_cache_uses_xdg_directory(self):
        cache = self.root / "user-cache"
        actual = self.fresh_python("from my_agents2.models import REGISTRY_PATH; print(REGISTRY_PATH)",
                                   XDG_CACHE_HOME=str(cache))
        self.assertEqual(Path(actual), cache / "my-agents-fleet" / "models.json")

    def test_explicit_cache_path_wins(self):
        cache = self.root / "explicit" / "models.json"
        actual = self.fresh_python("from my_agents2.models import REGISTRY_PATH; print(REGISTRY_PATH)",
                                   MY_AGENTS2_MODELS_PATH=str(cache), XDG_CACHE_HOME=str(self.root / "ignored"))
        self.assertEqual(Path(actual), cache)

    def test_default_cache_is_outside_installed_package(self):
        code = ("from pathlib import Path; import my_agents2; "
                "from my_agents2.models import REGISTRY_PATH; "
                "assert not REGISTRY_PATH.is_relative_to(Path(my_agents2.__file__).parent); "
                "assert REGISTRY_PATH == Path.home() / '.cache' / 'my-agents-fleet' / 'models.json'")
        self.fresh_python(code)

    def test_external_backend_configuration(self):
        config = self.root / "backends.json"
        config.write_text(json.dumps({"default": "review", "agents": {
            "review": {"kind": "cli", "impl": "codex_cli"}}}), encoding="utf-8")
        actual = self.fresh_python("from my_agents2 import list_agents; print(','.join(list_agents()))",
                                   MY_AGENTS2_CONFIG=str(config))
        self.assertEqual(actual, "review")

    def test_agents_help_is_offline(self):
        from my_agents2.__main__ import main as agents_main
        output = io.StringIO()
        with patch.object(sys, "argv", ["my-agents2", "--help"]), \
                patch("subprocess.Popen", side_effect=AssertionError("help must remain offline")), \
                contextlib.redirect_stdout(output):
            agents_main()
        self.assertIn("--models", output.getvalue())
