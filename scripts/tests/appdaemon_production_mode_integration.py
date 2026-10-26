#!/usr/bin/env python3
"""Exercise production-mode reloads in a disposable copy of the live AD image.

Run explicitly (this is not part of the ordinary unittest discovery):
    python3 scripts/tests/appdaemon_production_mode_integration.py
    python3 scripts/tests/appdaemon_production_mode_integration.py --helper scripts/appdaemon_deploy.py

The remote fixture has no HA/MQTT plugins, no production mounts and no network.
Only its uniquely named container is signalled or removed. Its fixture and JSON
report remain under /tmp for inspection. No image is pulled or built.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import uuid


DEPLOY_HELPER_SOURCE = None


PROBE = '''import json
import time
import uuid
from pathlib import Path
import appdaemon.adapi as adapi
{imports}

CODE = {code!r}

class Probe(adapi.ADAPI):
    def initialize(self):
        self.instance = uuid.uuid4().hex
        if CODE == "slow-initialize":
            time.sleep(2)
        if CODE == "fail-until-unblocked" and not Path("/conf/allow-initialize").exists():
            raise RuntimeError("synthetic initialization blocked by fixture")
        if CODE == "poison-runtime-secrets":
            import appdaemon.utils as utils
            utils.secrets = {{"fixture_unrelated_secret": "synthetic-only"}}
        self.emit("initialized")
        self.run_every(self.heartbeat, "now+1", 1)

    def emit(self, kind):
        record = {{"kind": kind, "name": self.name, "instance": self.instance,
                  "code": CODE, "helper": {helper},
                  "production_mode": self.AD.production_mode,
                  "args": {{k: self.args[k] for k in ("value", "optional") if k in self.args}},
                  "at": time.time()}}
        with Path("/conf/events.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\\n")

    def heartbeat(self, kwargs):
        self.emit("heartbeat")

    def terminate(self):
        self.emit("terminated")
'''


class Fixture:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="appdaemon-deploy-test-"))
        self.name = "appdaemon-deploy-test-" + uuid.uuid4().hex[:12]
        self.image = self.docker("inspect", "appdaemon", "--format", "{{.Image}}").strip()
        if not self.image.startswith("sha256:"):
            raise RuntimeError("production image ID was not a content-addressed image")
        self.apps = self.root / "apps"
        self.apps.mkdir()
        self.results = []
        self.started = False

    def docker(self, *args, check=True):
        result = subprocess.run(["sudo", "-n", "docker", *args], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and result.returncode:
            raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        # FileCheck only recognizes an increasing mtime. Do not rely on the
        # filesystem's timestamp granularity, or Python's same-second pyc cache.
        stamp = time.time() + 1
        os.utime(target, (stamp, stamp))

    def config(self, production):
        self.write("appdaemon.yaml", f'''appdaemon:
  latitude: 55.67
  longitude: 12.56
  elevation: 10
  time_zone: UTC
  utility_delay: 1
  max_utility_skew: 30
  production_mode: {str(production).lower()}
  plugins: {{}}
logs:
  main_log:
    filename: /conf/main.log
  error_log:
    filename: /conf/error.log
http:
  url: http://127.0.0.1:5050
api: {{}}
''')

    def source(self, code="initial"):
        return PROBE.format(imports="import helper", code=code, helper="helper.VALUE")

    def app_config(self, value="initial", extra=True):
        suffix = "  optional: keep-me\n" if extra else ""
        self.write("apps/apps.yaml", f'''Consumer:
  module: consumer
  class: Probe
  value: {value}
{suffix}Unrelated:
  module: unrelated
  class: Probe
''')

    def prepare(self):
        self.config(True)
        self.write("apps/helper.py", 'VALUE = "helper-initial"\n')
        self.write("apps/consumer.py", self.source())
        self.write("apps/unrelated.py", PROBE.format(imports="", code="unrelated", helper="None"))
        self.app_config()

    def start(self):
        self.docker("run", "--detach", "--name", self.name, "--network", "none",
                    "--entrypoint", "python3", "--mount",
                    f"type=bind,src={self.root},dst=/conf", self.image,
                    "-m", "appdaemon", "-c", "/conf")
        self.started = True

    def events(self, kind=None, name=None):
        path = self.root / "events.jsonl"
        result = []
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # the writer may currently be appending the tail
                if (kind is None or record["kind"] == kind) and (name is None or record["name"] == name):
                    result.append(record)
        return result

    def count(self, name):
        return len(self.events("initialized", name))

    def wait(self, predicate, label, timeout=18):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        raise AssertionError(f"timed out: {label}")

    def record(self, name, **details):
        result = {"check": name, "passed": True, **details}
        self.results.append(result)
        print(json.dumps(result), flush=True)

    def signal(self):
        self.docker("kill", "--signal", "SIGUSR2", self.name)

    def runtime_mode(self, production):
        code = '''import json,sys,urllib.request
request=urllib.request.Request("http://127.0.0.1:5050/api/appdaemon/service/admin/production_mode/set",
    data=json.dumps({"mode":sys.argv[1]=="true"}).encode(),
    headers={"Content-Type":"application/json"},method="POST")
response=urllib.request.urlopen(request,timeout=5)
print(response.status, response.read().decode())
'''
        result = self.docker("exec", self.name, "python3", "-c", code, str(production).lower())
        if not result.startswith("200 "):
            raise AssertionError(f"runtime mode service failed: {result}")

    def helper_deploy(self, updates=None, transfer_returncode=0, expect_success=True, mtimes=None):
        """Run the real two-phase helper against only this isolated fixture."""
        updates = updates or {}
        files = {str(path.relative_to(self.root)): path.read_text()
                 for path in self.apps.rglob("*") if path.is_file() and not path.name.endswith(".pyc")}
        files.update(updates)
        manifest = {"files": {name: hashlib.sha256(source.encode()).hexdigest()
                               for name, source in files.items()}}
        process = subprocess.Popen(
            ["sudo", "-n", "docker", "exec", "-i", self.name, "python3", "/conf/deploy_helper.py",
             "--config", "/conf/appdaemon.yaml", "--state-dir", "/conf/.deploy", "--pid", "1",
             "--timeout", "7"],
            text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            process.stdin.write(json.dumps(manifest) + "\n")
            process.stdin.flush()
            if not select.select([process.stdout], [], [], 15)[0]:
                raise AssertionError("deploy helper did not report transfer readiness")
            first = process.stdout.readline().strip()
            ready = json.loads(first)
            if not (ready.get("ready") or ready.get("status") == "ready" or ready.get("event") == "ready"):
                raise AssertionError(f"deploy helper rejected readiness: {first}")
            for name, source in updates.items():
                self.write(name, source)
                if mtimes and name in mtimes:
                    os.utime(self.root / name, (mtimes[name], mtimes[name]))
            process.stdin.write(json.dumps({"returncode": transfer_returncode}) + "\n")
            process.stdin.flush()
            stdout, stderr = process.communicate(timeout=35)
            (self.root / "helper-last.log").write_text(first + "\n" + stdout + stderr)
            if (process.returncode == 0) != expect_success:
                raise AssertionError(f"deploy helper exit {process.returncode}: {stdout} {stderr}")
            return stdout
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def check_helper(self):
        if DEPLOY_HELPER_SOURCE is None:
            return
        self.write("deploy_helper.py", DEPLOY_HELPER_SOURCE)
        self.check_helper_secret_isolation()
        self.runtime_mode(False)
        before = {name: self.count(name) for name in ("Consumer", "Unrelated")}
        self.helper_deploy()
        toggled_at = time.time()
        self.wait(lambda: any(e["at"] > toggled_at and e["production_mode"] is True
                              for e in self.events("heartbeat", "Unrelated")),
                  "deploy helper restores configured production mode")
        if before != {name: self.count(name) for name in before}:
            raise AssertionError("real helper no-op reloaded unchanged apps")
        self.record("deploy_helper_runtime_false_converges_to_configured_production_mode")
        self.record("deploy_helper_noop")

        before = self.count("Consumer")
        unrelated = self.count("Unrelated")
        self.helper_deploy({"apps/consumer.py": self.source("helper-code")})
        if self.count("Consumer") <= before or self.events("initialized", "Consumer")[-1]["code"] != "helper-code":
            raise AssertionError("real helper did not load transferred Python")
        if self.count("Unrelated") != unrelated:
            raise AssertionError("real helper reloaded unrelated app")
        self.record("deploy_helper_python_reload_complete")

        # Production writes .pyc files. A same-length helper edit with an
        # increasing fractional mtime in the same integer second must not load
        # stale cached bytecode, even though FileCheck sees the source change.
        bytecode = list((self.apps / "__pycache__").glob("helper.*.pyc"))
        if not bytecode:
            raise AssertionError("shared helper bytecode was not cached; regression test is ineffective")
        helper_path = self.apps / "helper.py"
        old_stamp = helper_path.stat().st_mtime
        same_second_newer = int(old_stamp) + ((old_stamp % 1) + 1) / 2
        old_source = helper_path.read_text()
        new_source = old_source.replace("helper-updated", "helper-newest!")
        if len(old_source) != len(new_source) or old_source == new_source:
            raise AssertionError("shared helper replacement did not preserve source length")
        self.helper_deploy({"apps/helper.py": new_source}, mtimes={"apps/helper.py": same_second_newer})
        if self.events("initialized", "Consumer")[-1]["helper"] != "helper-newest!":
            raise AssertionError("real helper accepted stale shared-module bytecode")
        self.record("deploy_helper_same_second_shared_module_bytecode_refresh")

        start = time.monotonic()
        self.helper_deploy({"apps/consumer.py": self.source("slow-initialize")})
        if time.monotonic() - start < 2 or self.events("initialized", "Consumer")[-1]["code"] != "slow-initialize":
            raise AssertionError("real helper returned before initialize completed")
        self.record("deploy_helper_waits_for_initialize_completion")

        old_config = (self.apps / "apps.yaml").read_text()
        new_config = old_config.replace("  value: yaml-updated\n", "  value: helper-yaml\n  optional: helper-added\n")
        self.helper_deploy({"apps/apps.yaml": new_config})
        latest = self.events("initialized", "Consumer")[-1]
        if latest["args"] != {"value": "helper-yaml", "optional": "helper-added"}:
            raise AssertionError("real helper YAML argument transfer did not take effect")
        self.record("deploy_helper_yaml_argument_reload")
        self.helper_deploy({"apps/apps.yaml": new_config.replace("  optional: helper-added\n", "")})
        latest = self.events("initialized", "Consumer")[-1]
        if latest["args"] != {"value": "helper-yaml"}:
            raise AssertionError("real helper did not force reload for removed YAML argument")
        self.record("deploy_helper_yaml_argument_removal_reload")

        before = self.count("Consumer")
        self.helper_deploy({"apps/consumer.py": self.source("helper-pending")},
                           transfer_returncode=1, expect_success=False)
        if self.count("Consumer") != before:
            raise AssertionError("real helper signalled after failed transfer")
        self.helper_deploy()  # bytes are already present: the journal must retain the reload obligation
        if self.count("Consumer") <= before or self.events("initialized", "Consumer")[-1]["code"] != "helper-pending":
            raise AssertionError("real helper lost pending reload on unchanged retry")
        self.record("deploy_helper_failed_transfer_unchanged_retry")

        self.helper_deploy({"apps/consumer.py": "def broken(:\n"}, expect_success=False)
        self.helper_deploy({"apps/consumer.py": self.source("helper-syntax-recovered")})
        if self.events("initialized", "Consumer")[-1]["code"] != "helper-syntax-recovered":
            raise AssertionError("real helper could not recover pending broken source")
        self.record("deploy_helper_broken_source_fixed_content_retry")

        self.helper_deploy({"apps/consumer.py": self.source("fail-until-unblocked")}, expect_success=False)
        self.write("allow-initialize", "fixture permits initialization now\n")
        before = self.count("Consumer")
        self.helper_deploy()
        if self.count("Consumer") <= before or self.events("initialized", "Consumer")[-1]["code"] != "fail-until-unblocked":
            raise AssertionError("real helper lost failed initialization on unchanged retry")
        self.record("deploy_helper_failed_initialization_unchanged_retry")

        current_config = (self.apps / "apps.yaml").read_text()
        disabled = current_config.replace("Consumer:\n", "Consumer:\n  disable: true\n", 1)
        self.helper_deploy({"apps/apps.yaml": disabled}, expect_success=False)
        self.helper_deploy({"apps/apps.yaml": current_config})
        self.record("deploy_helper_disabled_app_requires_controlled_restart")

        # Native 4.5.13 can swallow a config-read failure and reinitialize an
        # app with cached old args. The isolated helper loader deliberately
        # resolves secrets correctly; a fresh native error must still fail the
        # deployment even when app_initialized and idle are both observed.
        self.helper_deploy({"apps/consumer.py": self.source("poison-runtime-secrets")})
        self.write("secrets.yaml", "fixture_runtime_value: changed-secret-value\n")
        secret_config = current_config.replace("  value: helper-yaml\n",
                                               "  value: !secret fixture_runtime_value\n")
        before = self.count("Consumer")
        self.helper_deploy({"apps/apps.yaml": secret_config}, expect_success=False)
        self.wait(lambda: self.count("Consumer") > before, "native cached-argument initialization")
        if self.events("initialized", "Consumer")[-1]["args"].get("value") != "helper-yaml":
            raise AssertionError("runtime config-error fixture did not reproduce old-argument initialization")
        self.record("deploy_helper_native_config_error_cannot_fake_completion")
        self.helper_deploy({"apps/apps.yaml": current_config,
                            "apps/consumer.py": self.source("helper-native-recovered")})
        latest = self.events("initialized", "Consumer")[-1]
        if latest["code"] != "helper-native-recovered" or latest["args"].get("value") != "helper-yaml":
            raise AssertionError("real helper could not recover native config error")
        self.record("deploy_helper_native_config_error_fixed_content_retry")

    def check_helper_secret_isolation(self):
        # This parser-only fixture is outside the running apps tree: it cannot
        # create additional runtime apps or alter their argument resolution.
        self.write("parser-case/appdaemon.yaml", "appdaemon:\n  production_mode: true\n")
        self.write("parser-case/secrets.yaml", "fixture_module: unrelated\n")
        self.write("parser-case/apps/consumer.py", "VALUE = 1\n")
        self.write("parser-case/apps/unrelated.py", "VALUE = 2\n")
        self.write("parser-case/apps/a_local/secrets.yaml", "fixture_module: consumer\n")
        self.write("parser-case/apps/a_local/a.yaml",
                   "LocalSecret:\n  module: !secret fixture_module\n  class: Probe\n")
        self.write("parser-case/apps/b_fallback/b.yaml",
                   "FallbackSecret:\n  module: !secret fixture_module\n  class: Probe\n")
        code = '''import runpy
from pathlib import Path
helper=runpy.run_path("/conf/deploy_helper.py")
app_dir,exclude,_,_=helper["read_settings"](Path("/conf/parser-case/appdaemon.yaml"))
state=helper["snapshot"](app_dir,exclude)
assert state["apps"]["LocalSecret"]["module"] == "consumer"
assert state["apps"]["FallbackSecret"]["module"] == "unrelated"
print("secret isolation verified")
'''
        self.docker("exec", self.name, "python3", "-c", code)
        self.record("deploy_helper_app_local_secret_then_main_fallback")

    def reload_consumer(self, expected, label):
        count = self.count("Consumer")
        unrelated = self.count("Unrelated")
        self.signal()
        self.wait(lambda: self.count("Consumer") > count, label)
        latest = self.events("initialized", "Consumer")[-1]
        if not expected(latest):
            raise AssertionError(f"{label}: wrong initialized payload: {latest}")
        if self.count("Unrelated") != unrelated:
            raise AssertionError(f"{label}: unrelated app reinitialized")
        self.wait(lambda: any(e["instance"] == latest["instance"]
                              for e in self.events("heartbeat", "Consumer")),
                  f"{label} callback completion")
        self.record(label, unrelated_unchanged=True)

    def check(self):
        self.prepare()
        self.start()
        self.wait(lambda: self.count("Consumer") == 1 and self.count("Unrelated") == 1, "startup")
        self.record("startup", image=self.image, container=self.name)

        self.write("apps/consumer.py", self.source("code-updated"))
        heartbeats = len(self.events("heartbeat", "Consumer"))
        self.wait(lambda: len(self.events("heartbeat", "Consumer")) >= heartbeats + 4,
                  "production mode heartbeat continuity")
        if self.count("Consumer") != 1:
            raise AssertionError("production_mode allowed an automatic reload")
        self.record("production_mode_suppresses_automatic_scan", heartbeats_continued=True)
        self.reload_consumer(lambda e: e["code"] == "code-updated", "python_edit_explicit_scan")

        self.write("apps/helper.py", 'VALUE = "helper-updated"\n')
        self.reload_consumer(lambda e: e["helper"] == "helper-updated", "import_dependent_reloaded")

        self.app_config(value="yaml-updated")
        self.reload_consumer(lambda e: e["args"].get("value") == "yaml-updated", "yaml_argument_change")

        self.app_config(value="yaml-updated", extra=False)
        before = self.count("Consumer")
        self.signal()
        # Removing an argument alone is a known asymmetric compare defect in
        # 4.5.13. Observe it explicitly rather than claiming SIGUSR2 fixes it.
        time.sleep(3)
        latest = self.events("initialized", "Consumer")[-1]
        removed = self.count("Consumer") > before and "optional" not in latest["args"]
        self.results.append({"check": "yaml_key_removal_without_module_touch",
                             "passed": removed, "known_upstream_limitation": not removed})
        print(json.dumps(self.results[-1]), flush=True)
        if not removed:
            self.write("apps/consumer.py", self.source("yaml-removal-reload"))
            self.reload_consumer(lambda e: "optional" not in e["args"], "yaml_key_removal_module_touch_recovery")

        before = {name: self.count(name) for name in ("Consumer", "Unrelated")}
        self.signal()
        time.sleep(3)
        if before != {name: self.count(name) for name in before}:
            raise AssertionError("no-op SIGUSR2 scan reloaded an unchanged app")
        self.record("noop_scan_does_not_reload")

        error_path = self.root / "error.log"
        error_size = error_path.stat().st_size if error_path.exists() else 0
        self.write("apps/consumer.py", "def broken(:\n")
        self.signal()
        self.wait(lambda: error_path.exists() and error_path.stat().st_size > error_size,
                  "syntax error is reported")
        self.record("syntax_failure_is_reported")
        self.write("apps/consumer.py", self.source("recovered"))
        self.reload_consumer(lambda e: e["code"] == "recovered", "syntax_failure_recovery_fresh_mtime")

        # A no-content-change retry still asks for a scan: the previous transfer
        # may have succeeded before signal delivery/verification failed.
        self.write("apps/consumer.py", self.source("retry-ready"))
        before = self.count("Consumer")
        time.sleep(2)
        if self.count("Consumer") != before:
            raise AssertionError("unrequested reload during retry preparation")
        self.reload_consumer(lambda e: e["code"] == "retry-ready", "no_content_change_retry_scans_pending_source")

        self.runtime_mode(False)
        before = self.count("Consumer")
        self.write("apps/consumer.py", self.source("runtime-automatic"))
        self.wait(lambda: self.count("Consumer") > before, "runtime mode false automatic reload")
        if self.events("initialized", "Consumer")[-1]["code"] != "runtime-automatic":
            raise AssertionError("runtime false toggle loaded stale source")
        self.record("runtime_production_mode_false_restores_automatic_scan")
        self.runtime_mode(True)
        before = self.count("Consumer")
        self.write("apps/consumer.py", self.source("runtime-explicit"))
        time.sleep(3)
        if self.count("Consumer") != before:
            raise AssertionError("runtime true toggle did not suppress automatic scan")
        self.reload_consumer(lambda e: e["code"] == "runtime-explicit", "runtime_production_mode_true_explicit_reload")

        self.check_helper()

        self.docker("stop", "--time", "10", self.name)
        self.config(False)
        self.docker("start", self.name)
        self.wait(lambda: self.count("Consumer") > before + 1, "rollback startup")
        before = self.count("Consumer")
        self.write("apps/consumer.py", self.source("automatic-restored"))
        self.wait(lambda: self.count("Consumer") > before, "rollback automatic reload")
        if self.events("initialized", "Consumer")[-1]["code"] != "automatic-restored":
            raise AssertionError("rollback loaded stale source")
        self.record("rollback_restores_automatic_scanning")

    def run(self):
        failure = None
        try:
            self.check()
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            if self.started:
                (self.root / "container.log").write_text(self.docker("logs", self.name, check=False))
                self.docker("rm", "--force", self.name, check=False)
            report = {"success": failure is None, "failure": failure, "image": self.image,
                      "fixture": str(self.root), "checks": self.results}
            (self.root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report), flush=True)
        return 1 if failure else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="mke@10.21.0.5")
    parser.add_argument("--helper", type=Path, help="also exercise this two-phase deployment helper")
    parser.add_argument("--remote", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.remote:
        return Fixture().run()
    source = Path(__file__).read_text()
    if args.helper is not None:
        source = source.replace("DEPLOY_HELPER_SOURCE = None\n",
                                "DEPLOY_HELPER_SOURCE = " + repr(args.helper.read_text()) + "\n", 1)
    command = "python3 - --remote"
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", args.host, command],
                            input=source, text=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
