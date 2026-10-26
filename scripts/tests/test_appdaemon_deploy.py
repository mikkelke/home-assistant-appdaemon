"""Focused deploy transaction tests; no SSH, containers or third-party packages."""

import argparse
import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


SPEC = importlib.util.spec_from_file_location("appdaemon_deploy", Path(__file__).parents[1] / "appdaemon_deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def snapshot(apps=None, imports=None, dependencies=None, modules=None):
    return {
        "apps": apps if apps is not None else {"Target": app("target")},
        "globals": {},
        "imports": imports or {},
        "app_dependencies": dependencies or {},
        "modules": modules if modules is not None else {"apps/target.py": "target"},
        "mtimes": {},
    }


def app(module, digest="config", disabled=False, dictionaries=()):
    return {"module": module, "digest": digest, "disabled": disabled,
            "dict_paths": [list(path) for path in dictionaries]}


class MappingTests(unittest.TestCase):
    def test_comments_and_documents_do_not_require_reload(self):
        before = snapshot()
        self.assertEqual(deploy.affected_apps(before, before, ["apps/target.yaml", "apps/README.md"]), set())

    def test_python_import_and_app_dependencies_are_transitive(self):
        state = snapshot(
            {"Target": app("target"), "Consumer": app("consumer"), "Downstream": app("downstream"),
             "Other": app("other")},
            {"target": ["helper"], "consumer": ["target"]},
            {"Downstream": ["Consumer"]},
            {"apps/helper.py": "helper", "apps/target.py": "target"})
        self.assertEqual(deploy.affected_apps(state, state, ["apps/helper.py"]),
                         {"Target", "Consumer", "Downstream"})

    def test_module_siblings_and_mixed_dependencies_reach_fixed_point(self):
        state = snapshot(
            {"A": app("a"), "B": app("b"), "B2": app("b"), "C": app("c"),
             "D": app("d"), "D2": app("d"), "E": app("e"), "F": app("f")},
            {"c": ["b"], "e": ["d"]}, {"B": ["A"], "D": ["C"], "F": ["E"]},
            {"apps/a.py": "a"})
        self.assertEqual(deploy.affected_apps(state, state, ["apps/a.py"]), set(state["apps"]))

    def test_old_import_graph_is_retained_after_source_changes(self):
        before = snapshot(imports={"target": ["helper"]}, modules={"apps/helper.py": "helper"})
        after = snapshot(modules={"apps/helper.py": "helper"})
        self.assertEqual(deploy.affected_apps(before, after, ["apps/helper.py"]), {"Target"})

    def test_config_key_removal_requires_target_reload(self):
        before, after = snapshot(), snapshot({"Target": app("target", "removed-key")})
        self.assertEqual(deploy.affected_apps(before, after, ["apps/target.yaml"]), {"Target"})
        deploy.check_supported(before, after, {"Target"})

    def test_unsafe_config_shapes_and_removal_fail_explicitly(self):
        before = snapshot()
        for after in (snapshot({}), snapshot({"Target": app("target", disabled=True)}),
                      snapshot({"Target": app("target", dictionaries=[("new_dict",)])})):
            with self.subTest(after=after), self.assertRaisesRegex(deploy.DeployError, "controlled restart"):
                deploy.check_supported(before, after, {"Target"})

    def test_set_and_yaml_map_order_do_not_change_semantic_snapshot(self):
        a = {"deps": {"B", "A"}, "args": {"b": {}, "a": {"inside": {}}}}
        b = {"args": {"a": {"inside": {}}, "b": {}}, "deps": {"A", "B"}}
        self.assertEqual(json.dumps(deploy.canonical(a)), json.dumps(deploy.canonical(b)))
        self.assertEqual(deploy.dictionary_paths(a), deploy.dictionary_paths(b))

    def test_non_app_definition_and_configured_global_python_changes_fail(self):
        before, after = snapshot(), snapshot()
        after["globals"] = {"sequence": {"digest": "changed", "module": "sequence", "modules": []}}
        with self.assertRaisesRegex(deploy.DeployError, "controlled restart"):
            deploy.check_supported(before, after, set())
        before["globals"] = after["globals"] = {
            "Global": {"digest": "same", "module": "target", "modules": ["target"]}}
        with self.assertRaisesRegex(deploy.DeployError, "Configured global"):
            deploy.check_supported(before, after, set(), ["apps/target.py"])


class ConfigLoadingTests(unittest.TestCase):
    def test_each_document_uses_main_secrets_and_data_yaml_is_not_an_app(self):
        class App:
            module_name = "target"
            disable = False

            def model_dump(self, **_):
                return {"module": self.module_name, "dependencies": {"B", "A"}}

        class Configs:
            def __init__(self, root):
                self.root = root

            @classmethod
            def model_validate(cls, raw):
                return cls(raw)

            @classmethod
            def model_construct(cls, root):
                return cls(root)

            def depedency_graph(self):
                return {}

        with tempfile.TemporaryDirectory() as temporary:
            apps = Path(temporary) / "apps"
            paths = [apps / name for name in ("presence/first.yaml", "transit/second.yaml",
                                               "appliances/dishwasher_programmes.yaml")]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            utils = types.ModuleType("appdaemon.utils")
            utils.secrets = {"main": "not-printed"}
            calls = []

            def read_config(path, **_):
                calls.append((path, dict(utils.secrets)))
                utils.secrets = {"local": "not-printed"}
                return {path.stem: App()}

            utils.read_config_file = read_config
            utils.recursive_get_files = lambda **kw: [] if kw["suffix"] == ".py" else paths
            package = types.ModuleType("appdaemon")
            package.utils = utils
            dependency = types.ModuleType("appdaemon.dependency")
            dependency.get_dependency_graph = lambda files: ({}, set())
            dependency.get_full_module_name = lambda path: path.stem
            models = types.ModuleType("appdaemon.models.config.app")
            models.AppConfig, models.AllAppConfig = App, Configs
            with patch.dict("sys.modules", {"appdaemon": package, "appdaemon.utils": utils,
                                             "appdaemon.dependency": dependency,
                                             "appdaemon.models.config.app": models}), \
                    patch.object(deploy, "APP_SECRETS", {"main": "not-printed"}):
                result = deploy.snapshot(apps, ["tests"])
            self.assertEqual({path.name for path, _ in calls}, {"first.yaml", "second.yaml"})
            self.assertTrue(all(secrets == {"main": "not-printed"} for _, secrets in calls))
            self.assertEqual(set(result["apps"]), {"first", "second"})
            self.assertNotIn("not-printed", json.dumps(result))


class ScanErrorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "error.log"
        self.path.write_text("old BadAppConfigFile: secret-old\n")
        self.errors = deploy.ScanErrors(self.path)
        self.errors.open()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.errors.close)

    def append(self, text):
        with self.path.open("a") as stream:
            stream.write(text)

    def test_old_errors_and_unrelated_app_errors_do_not_fail(self):
        self.append("ERROR OtherApp: Error during callback\n")
        self.errors.check()

    def test_new_native_config_failure_has_sanitized_error(self):
        self.append("BadAppConfigFile: secret-value\n")
        with self.assertRaisesRegex(deploy.DeployError, "config scan failed") as failure:
            self.errors.check()
        self.assertNotIn("secret-value", str(failure.exception))

    def test_error_split_across_reads_is_not_missed(self):
        self.append("ConfigRead")
        self.errors.check()
        self.append("Failure: private-details\n")
        with self.assertRaises(deploy.DeployError):
            self.errors.check()

    def test_rotation_checks_both_original_and_replacement_log(self):
        self.path.rename(self.path.with_suffix(".log.1"))
        self.path.write_text("BadAppConfig: private-details\n")
        with self.assertRaises(deploy.DeployError):
            self.errors.check()

    def test_truncated_log_fails_instead_of_losing_evidence(self):
        self.path.write_text("")
        with self.assertRaisesRegex(deploy.DeployError, "truncated"):
            self.errors.check()

    def test_missing_file_backed_error_log_fails_explicitly(self):
        with self.assertRaisesRegex(deploy.DeployError, "file-backed"):
            deploy.ScanErrors(None).open()


class TransactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.apps = self.root / "apps"
        self.apps.mkdir()
        self.target = self.apps / "target.py"
        self.target.write_text("old\n")
        self.args = argparse.Namespace(config=self.root / "appdaemon.yaml", state_dir=self.root / ".deploy",
                                       pid=123456, timeout=0.02)
        self.before = snapshot()
        self.before["mtimes"] = {"apps/target.py": self.target.stat().st_mtime_ns}
        self.after = snapshot()
        self.witness = AsyncMock()
        self.witness.__aenter__.return_value = self.witness
        self.patches = [
            patch.object(deploy, "read_settings", return_value=(self.apps, ["tests"], "http://localhost", None)),
            patch.object(deploy, "Witness", return_value=self.witness),
            patch.object(deploy.os, "kill"),
            patch.object(deploy, "enforce_production_mode", new_callable=AsyncMock),
        ]
        self.settings, self.witness_class, self.kill, self.mode = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(self.temporary.cleanup)

    def request(self, content="new\n"):
        import hashlib
        return {"files": {"apps/target.py": hashlib.sha256(content.encode()).hexdigest()}}

    async def run_deploy(self, content="new\n", rc=0, write=True, snapshots=None):
        request = json.dumps(self.request(content)) + "\n"
        target = self.target

        class Stream:
            calls = 0

            def readline(self):
                self.calls += 1
                if self.calls == 1:
                    return request
                if write:
                    target.write_text(content)
                return json.dumps({"returncode": rc}) + "\n"

        self.output = io.StringIO()
        with patch.object(deploy, "snapshot", side_effect=snapshots or [self.before, self.after]), \
                contextlib.redirect_stdout(self.output):
            await deploy.deploy(self.args, Stream())

    def pending(self):
        return json.loads((self.args.state_dir / "pending.json").read_text())

    async def test_success_requires_completion_and_clears_journal(self):
        await self.run_deploy()
        self.kill.assert_called_once_with(123456, signal.SIGUSR2)
        self.witness.completed.assert_awaited_once_with({"Target"}, 0.02)
        self.assertFalse((self.args.state_dir / "pending.json").exists())
        result = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(result["initialized"], ["Target"])

    async def test_transfer_failure_even_if_bytes_landed_never_signals_and_identical_retry_works(self):
        with self.assertRaisesRegex(deploy.DeployError, "Transfer failed"):
            await self.run_deploy(rc=23)
        self.kill.assert_not_called()
        self.assertIn("apps/target.py", self.pending()["changed"])
        await self.run_deploy(write=False)
        self.assertTrue(json.loads(self.output.getvalue().splitlines()[0])["retry"])
        self.kill.assert_called_once()

    async def test_content_mismatch_never_signals(self):
        with self.assertRaisesRegex(deploy.DeployError, "Content verification failed"):
            await self.run_deploy(write=False)
        self.kill.assert_not_called()
        self.assertTrue(self.pending())

    async def test_init_failure_retains_pending_for_zero_byte_retry(self):
        self.witness.completed.side_effect = deploy.DeployError("App initialization failed: Target")
        with self.assertRaisesRegex(deploy.DeployError, "initialization failed"):
            await self.run_deploy()
        first_stamp = self.pending()["mtimes"]["apps/target.py"]
        self.assertEqual(self.pending()["expected"], ["Target"])
        self.witness.completed.side_effect = None
        await self.run_deploy(write=False)
        self.assertEqual(self.kill.call_count, 2)
        self.assertGreater(self.target.stat().st_mtime_ns, first_stamp)

    async def test_broken_current_source_does_not_block_pending_repair(self):
        with self.assertRaises(deploy.DeployError):
            await self.run_deploy(rc=1)
        await self.run_deploy(content="repaired\n", snapshots=[SyntaxError("private config"), self.after])
        self.kill.assert_called_once()
        self.assertNotIn("private config", self.output.getvalue())

    async def test_malformed_after_snapshot_retains_baseline_and_retry_repairs(self):
        with self.assertRaises(SyntaxError):
            await self.run_deploy(snapshots=[self.before, SyntaxError("invalid")])
        self.kill.assert_not_called()
        await self.run_deploy(content="repaired\n", snapshots=[SyntaxError("invalid"), self.after])
        self.kill.assert_called_once()

    async def test_no_change_does_not_signal(self):
        await self.run_deploy(content="old\n", write=False)
        self.kill.assert_not_called()
        self.witness_class.assert_not_called()
        self.assertFalse(json.loads(self.output.getvalue().splitlines()[-1])["reload"])

    async def test_yaml_comments_transfer_but_do_not_signal(self):
        # The manifest file name is immaterial to byte verification; mapping
        # deliberately excludes this file as the runtime excludes test sources.
        self.before["modules"] = self.after["modules"] = {}
        await self.run_deploy()
        self.kill.assert_not_called()

    async def test_unsupported_config_never_signals_and_stays_pending(self):
        self.after["apps"]["Target"]["dict_paths"] = [["new_dictionary"]]
        with self.assertRaisesRegex(deploy.DeployError, "controlled restart"):
            await self.run_deploy()
        self.kill.assert_not_called()
        self.assertEqual(self.pending()["expected"], ["Target"])

    async def test_second_deploy_cannot_enter_held_lock(self):
        import fcntl
        self.args.state_dir.mkdir()
        with (self.args.state_dir / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(deploy.DeployError, "holds the lock"):
                await self.run_deploy()
        self.kill.assert_not_called()
        self.mode.assert_not_awaited()

    async def test_mode_service_failure_aborts_before_ready_or_transfer(self):
        self.mode.side_effect = deploy.DeployError("Cannot enforce configured production mode")
        with self.assertRaisesRegex(deploy.DeployError, "production mode"):
            await self.run_deploy()
        self.assertEqual(self.output.getvalue(), "")
        self.assertEqual(self.target.read_text(), "old\n")
        self.assertFalse((self.args.state_dir / "pending.json").exists())
        self.kill.assert_not_called()

    async def test_replacement_after_failed_future_stamp_still_gets_newer_mtime(self):
        self.witness.completed.side_effect = deploy.DeployError("timeout")
        with patch.object(deploy.time, "time_ns", return_value=10):
            with self.assertRaises(deploy.DeployError):
                await self.run_deploy()
        stamp = self.pending()["mtimes"]["apps/target.py"]
        # rsync replaces a file with a fresh wall-clock timestamp, potentially
        # below a previous invalidation that AppDaemon has already consumed.
        self.target.write_text("replacement\n")
        deploy.os.utime(self.target, ns=(1, 1))
        self.witness.completed.side_effect = None
        with patch.object(deploy.time, "time_ns", return_value=10):
            await self.run_deploy(content="replacement\n", write=False)
        self.assertGreater(self.target.stat().st_mtime_ns, stamp)

    async def test_changed_imported_helper_gets_own_mtime_invalidation(self):
        helper = self.apps / "helper.py"
        helper.write_text("NEW = 1\n")
        state = snapshot(modules={"apps/target.py": "target", "apps/helper.py": "helper"})
        stamps = deploy.module_timestamps(self.apps, state, {"Target"}, {}, ["apps/helper.py"])
        self.assertEqual(set(stamps), {"apps/target.py", "apps/helper.py"})


class WitnessTests(unittest.IsolatedAsyncioTestCase):
    def witness(self, states, events):
        witness = deploy.Witness("http://localhost", None)
        witness.states = AsyncMock(return_value=states)
        witness.ws = AsyncMock()
        witness.ws.receive_json.side_effect = events
        return witness

    @staticmethod
    def initialized(name):
        return {"response_type": "event", "data": {"namespace": "admin", "event_type": "app_initialized",
                                                     "data": {"app": name}}}

    async def test_loaded_or_idle_state_alone_does_not_prove_completion(self):
        witness = self.witness({"app.Target": {"state": "idle"}}, [])
        with self.assertRaisesRegex(deploy.DeployError, "timed out"):
            await witness.completed({"Target"}, 0)

    async def test_each_expected_app_needs_own_completion_event(self):
        witness = self.witness({"app.Target": {"state": "idle"}, "app.Other": {"state": "idle"}},
                               [self.initialized("Other")])
        with patch.object(deploy.time, "monotonic", side_effect=[0, 0, 0, 1]), \
                self.assertRaisesRegex(deploy.DeployError, "Target"):
            await witness.completed({"Target"}, 0.5)

    async def test_signal_success_but_initialization_failure_is_failure(self):
        witness = self.witness({"app.Target": {"state": "initialize_error"}}, [])
        with self.assertRaisesRegex(deploy.DeployError, "initialization failed: Target"):
            await witness.completed({"Target"}, 1)

    async def test_old_initialization_error_does_not_fail_retry_before_scan(self):
        witness = self.witness({}, [self.initialized("Target")])
        witness.baseline = {"app.Target": ("initialize_error", "old")}
        witness.states.side_effect = [
            {"app.Target": {"state": "initialize_error", "last_changed": "old"}},
            {"app.Target": {"state": "idle", "last_changed": "new"}},
        ]
        await witness.completed({"Target"}, 1)

    async def test_new_failure_after_preexisting_error_fails(self):
        witness = self.witness({"app.Target": {"state": "initialize_error", "last_changed": "new"}}, [])
        witness.baseline = {"app.Target": ("initialize_error", "old")}
        with self.assertRaisesRegex(deploy.DeployError, "initialization failed"):
            await witness.completed({"Target"}, 1)

    async def test_completed_target_succeeds_with_unrelated_existing_failure(self):
        witness = self.witness({"app.Target": {"state": "idle"}, "app.Unrelated": {"state": "initialize_error"}},
                               [self.initialized("Target")])
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            await witness.completed({"Target"}, 1)
        self.assertIn("unrelated", output.getvalue())

    async def test_successful_init_cannot_hide_native_config_read_failure(self):
        witness = self.witness({"app.Target": {"state": "idle"}}, [self.initialized("Target")])
        witness.errors = unittest.mock.Mock()
        witness.errors.check.side_effect = [None, deploy.DeployError("AppDaemon config scan failed")]
        with self.assertRaisesRegex(deploy.DeployError, "config scan failed"):
            await witness.completed({"Target"}, 1)


class ProductionModeTests(unittest.IsolatedAsyncioTestCase):
    def fake_aiohttp(self, status=200, error=None):
        response = Mock(status=status)
        request = AsyncMock()
        request.__aenter__.return_value = response
        request.__aenter__.side_effect = error
        self.session = AsyncMock()
        self.session.post = Mock(return_value=request)
        manager = AsyncMock()
        manager.__aenter__.return_value = self.session
        module = types.ModuleType("aiohttp")
        module.ClientSession = Mock(return_value=manager)
        module.ClientTimeout = Mock()
        module.ClientError = type("ClientError", (Exception,), {})
        return module

    async def test_configured_mode_is_enforced_via_authenticated_native_service(self):
        with patch.dict("sys.modules", {"aiohttp": self.fake_aiohttp()}):
            await deploy.enforce_production_mode("http://localhost:5050", "private-password")
        self.session.post.assert_called_once_with(
            "http://localhost:5050/api/appdaemon/service/admin/production_mode/set",
            headers={"x-ad-access": "private-password"}, json={"mode": True})

    async def test_refused_native_service_fails(self):
        with patch.dict("sys.modules", {"aiohttp": self.fake_aiohttp(status=400)}), \
                self.assertRaisesRegex(deploy.DeployError, "transfer not started"):
            await deploy.enforce_production_mode("http://localhost", None)

    async def test_native_service_timeout_fails(self):
        with patch.dict("sys.modules", {"aiohttp": self.fake_aiohttp(error=asyncio.TimeoutError())}), \
                self.assertRaisesRegex(deploy.DeployError, "transfer not started"):
            await deploy.enforce_production_mode("http://localhost", None)


if __name__ == "__main__":
    unittest.main()
