#!/usr/bin/env python3
"""The locked, on-box half of deploy.sh. No credentials leave the container.

stdin: a tracked-file SHA256 manifest, then {"returncode": N} after rsync.
stdout: one JSON ready response, then one JSON result. Diagnostics use stderr.
The process holds the deploy lock while the caller transfers files.
"""

import argparse
import asyncio
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit, urlunsplit


APP_SECRETS = {}
ERROR_LOG = None
# These app-owned lookup tables intentionally are not AppDaemon app configs.
DATA_CONFIGS = {f"appliances/{appliance}_programmes.yaml" for appliance in ("dishwasher", "dryer", "washer")}


class DeployError(Exception):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(root):
    files = subprocess.check_output(["git", "ls-files", "-z", "apps"], cwd=root)
    return {"files": {name: digest(root / name) for name in files.decode().split("\0") if name}}


def emit(**message):
    print(json.dumps(message, sort_keys=True), flush=True)


def note(message):
    print(message, file=sys.stderr, flush=True)


def save_pending(path, pending):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(pending, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def merge_before(original, current):
    """Retain the pre-failure baseline and every dependency seen since then."""
    result = json.loads(json.dumps(original))
    for name, app in current["apps"].items():
        result["apps"].setdefault(name, app)
    for field in ("imports", "app_dependencies"):
        for name, dependencies in current[field].items():
            result[field][name] = sorted(set(result[field].get(name, [])) | set(dependencies))
    result["modules"].update(current["modules"])
    for path, stamp in current["mtimes"].items():
        result["mtimes"][path] = max(stamp, result["mtimes"].get(path, 0))
    return result


def dependents(names, graph):
    """Reverse dependency closure (both AppDaemon graphs use node -> deps)."""
    found = set(names)
    while True:
        extra = {name for name, dependencies in graph.items() if found.intersection(dependencies)}
        if extra <= found:
            return found
        found.update(extra)


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (set, frozenset)):
        return sorted((canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


def affected_apps(before, after, changed, pending_expected=()):
    modules = {snapshot["modules"][path] for snapshot in (before, after)
               for path in changed if path in snapshot["modules"]}
    affected = set(pending_expected)
    for snapshot in (before, after):
        changed_modules = dependents(modules, snapshot["imports"])
        affected.update(name for name, app in snapshot["apps"].items()
                        if app["module"] in changed_modules)
        affected.update(name for name, app in snapshot.get("globals", {}).items()
                        if app["module"] in changed_modules)
    affected.update(name for name in before["apps"].keys() | after["apps"].keys()
                    if before["apps"].get(name) != after["apps"].get(name))
    # Touching a config consumer's module also restarts its sibling instances,
    # module importers and app dependents. Include those in the proof as well.
    while True:
        previous = set(affected)
        for snapshot in (before, after):
            touched = {snapshot["apps"][name]["module"] for name in affected if name in snapshot["apps"]}
            touched = dependents(touched, snapshot["imports"])
            affected.update(name for name, app in snapshot["apps"].items() if app["module"] in touched)
            affected.update(dependents(affected, snapshot["app_dependencies"]))
        if previous == affected:
            break
    return affected & (before["apps"].keys() | after["apps"].keys())


def dictionary_paths(value, prefix=()):
    return sorted([list(prefix + (key,)) for key, item in value.items() if isinstance(item, dict)] + [
        path for key, item in value.items() if isinstance(item, dict)
        for path in dictionary_paths(item, prefix + (key,))])


def check_supported(before, after, affected, changed=()):
    if before.get("globals", {}) != after.get("globals", {}):
        raise DeployError("Global module config changes need an explicit controlled restart; pending deploy retained")
    for state in (before, after):
        changed_modules = {state["modules"][path] for path in changed if path in state["modules"]}
        changed_modules = dependents(changed_modules, state["imports"])
        if any(changed_modules.intersection(item["modules"]) for item in state.get("globals", {}).values()):
            raise DeployError("Configured global module changes need an explicit controlled restart; pending deploy retained")
    for name in sorted(affected):
        old, new = before["apps"].get(name), after["apps"].get(name)
        if old and (new is None or (new["disabled"] and not old["disabled"])):
            raise DeployError("App removal/disable needs an explicit controlled restart: " + name
                              + "; pending deploy retained")
        if old and new:
            added = {tuple(path) for path in new["dict_paths"]} - {tuple(path) for path in old["dict_paths"]}
            if added:
                raise DeployError("New dictionary config keys need an explicit controlled restart on AppDaemon 4.5.13: "
                                  + name + "; pending deploy retained")


def snapshot(app_dir, exclude_dirs):
    """Use the installed release's loaders/import rules, privately on the box."""
    from appdaemon import utils
    from appdaemon.dependency import get_dependency_graph, get_full_module_name
    from appdaemon.models.config.app import AllAppConfig, AppConfig

    python_files = set(utils.recursive_get_files(base=app_dir.resolve(), suffix=".py", exclude=set(exclude_dirs)))
    imports, failed = get_dependency_graph(python_files)
    if failed:
        raise DeployError("Python dependency scan failed; check the changed modules")
    config_files = set(utils.recursive_get_files(
        base=app_dir.resolve(), suffix={".yaml", ".toml"},
        exclude=set(exclude_dirs) | {"ruff.toml", "pyproject.toml", "secrets.yaml"}))
    config_files = {path for path in config_files if str(path.relative_to(app_dir)) not in DATA_CONFIGS}
    # from_config_files tolerates invalid apps. Validate each document strictly
    # first so malformed YAML never turns into an apparently removed app.
    merged = {}
    with contextlib.redirect_stdout(sys.stderr):
        try:
            for path in sorted(config_files):
                # The installed loader replaces utils.secrets when a directory
                # has its own secrets file. Restore the main config's secrets
                # for every document so sibling directories cannot inherit it.
                utils.secrets = copy.deepcopy(APP_SECRETS)
                raw = utils.read_config_file(path, app_config=True)
                if raw:
                    validated = AllAppConfig.model_validate(raw)
                    for name, definition in validated.root.items():
                        if name in merged:
                            if name in {"global_modules", "sequence"}:
                                merged[name].root.update(definition.root)
                            else:
                                raise DeployError("Duplicate AppDaemon app names in config files")
                        else:
                            merged[name] = definition
        finally:
            utils.secrets = copy.deepcopy(APP_SECRETS)
    configs = AllAppConfig.model_construct(root=merged)
    apps, globals_ = {}, {}
    for name, app in configs.root.items():
        data = canonical(app.model_dump(exclude={"config_path"}))
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
        if isinstance(app, AppConfig):
            apps[name] = {
                "digest": fingerprint,
                "module": app.module_name,
                "disabled": app.disable,
                "dict_paths": dictionary_paths(data),
            }
        else:
            modules = [app.module_name] if hasattr(app, "module_name") else list(app.root) if name == "global_modules" else []
            globals_[name] = {"digest": fingerprint, "module": getattr(app, "module_name", name),
                              "modules": sorted(modules)}
    return {"apps": apps, "globals": globals_, "imports": {name: sorted(deps) for name, deps in imports.items()},
            "app_dependencies": {name: sorted(deps) for name, deps in configs.depedency_graph().items()},
            "modules": {str(path.relative_to(app_dir.parent)): get_full_module_name(path) for path in python_files},
            "mtimes": {str(path.relative_to(app_dir.parent)): path.stat().st_mtime_ns for path in python_files}}


def read_settings(config_path):
    from appdaemon import utils

    global APP_SECRETS, ERROR_LOG
    with contextlib.redirect_stdout(sys.stderr):
        config = utils.read_config_file(config_path, app_config=False)
    APP_SECRETS = copy.deepcopy(utils.secrets)
    filename = config.get("logs", {}).get("error_log", {}).get("filename")
    ERROR_LOG = Path(filename) if filename and filename not in {"STDOUT", "STDERR"} else None
    if ERROR_LOG is not None and not ERROR_LOG.is_absolute():
        ERROR_LOG = config_path.parent / ERROR_LOG
    ad = config["appdaemon"]
    if ad.get("production_mode") is not True:
        raise DeployError("production_mode must be true before deploying; activate it explicitly first")
    app_dir = Path(ad.get("app_dir", config_path.parent / "apps"))
    if not app_dir.is_absolute():
        app_dir = config_path.parent / app_dir
    http = config.get("http", {})
    parts = urlsplit(http.get("url", "http://127.0.0.1:5050"))
    host = "127.0.0.1" if parts.hostname in ("0.0.0.0", "::") else parts.hostname
    if not host:
        raise DeployError("AppDaemon HTTP URL is missing")
    authority = f"{host}:{parts.port}" if parts.port else host
    base = urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))
    return app_dir, ad.get("exclude_dirs", []), base, http.get("password")


def verify_content(root, files):
    bad = [name for name, expected in files.items()
           if not (root / name).is_file() or digest(root / name) != expected]
    if bad:
        raise DeployError("Content verification failed: " + ", ".join(sorted(bad)))


async def enforce_production_mode(base, password):
    """Make the configured native mode effective before any bytes can move."""
    import aiohttp

    headers = {"x-ad-access": password} if password else {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(base + "/api/appdaemon/service/admin/production_mode/set",
                                    headers=headers, json={"mode": True}) as response:
                if response.status != 200:
                    raise DeployError("Cannot enforce configured production mode; transfer not started")
    except (asyncio.TimeoutError, aiohttp.ClientError):
        raise DeployError("Cannot reach the native production mode service; transfer not started") from None


def module_timestamps(app_dir, current, expected, previous, changed=()):
    modules = {current["apps"][name]["module"] for name in expected if name in current["apps"]}
    stamps = {}
    for relative, module in current["modules"].items():
        if module in modules or relative in changed:
            path = app_dir.parent / relative
            stat = path.stat()
            # Never rely on same-second writes or on the laptop's mtime.
            stamps[relative] = max(time.time_ns(), stat.st_mtime_ns + 1_000_000_000,
                                   previous.get(relative, 0) + 1_000_000_000)
    return stamps


def touch_modules(root, stamps):
    for relative, stamp in stamps.items():
        path = root / relative
        os.utime(path, ns=(path.stat().st_atime_ns, stamp))


class ScanErrors:
    """Read only fresh private error-log bytes, retaining rotated descriptors."""

    pattern = re.compile(rb"BadAppConfig(?:File)?\s*:|ConfigReadFailure\s*:|Reading user apps|"
                         rb"Unexpected error during utility|"
                         rb"Unhandled exception in.*check_app_updates", re.I)

    def __init__(self, path):
        self.path, self.streams, self.tails = path, {}, {}

    def open(self):
        if self.path is None:
            raise DeployError("A file-backed AppDaemon error_log is required to verify config scans")
        try:
            stream = self.path.open("rb")
            stat = os.fstat(stream.fileno())
            stream.seek(0, os.SEEK_END)
            self.streams[(stat.st_dev, stat.st_ino)] = stream
        except OSError:
            raise DeployError("AppDaemon error log cannot be read; reload verification unavailable") from None

    def check(self):
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity not in self.streams:
                self.streams[identity] = self.path.open("rb")
            for identity, stream in self.streams.items():
                if os.fstat(stream.fileno()).st_size < stream.tell():
                    raise DeployError("AppDaemon error log was truncated; reload verification uncertain")
                chunk = self.tails.get(identity, b"") + stream.read()
                self.tails[identity] = chunk[-256:]
                if self.pattern.search(chunk):
                    raise DeployError("AppDaemon config scan failed; check the private error log; pending deploy retained")
        except OSError:
            raise DeployError("AppDaemon error log became unreadable; reload verification uncertain") from None

    def close(self):
        for stream in self.streams.values():
            stream.close()


class Witness:
    def __init__(self, base, password):
        self.base, self.password = base, password

    async def __aenter__(self):
        import aiohttp

        self.errors = ScanErrors(ERROR_LOG)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        try:
            self.ws = await self.session.ws_connect(self.base + "/stream")
            await self.request("hello", {"client_name": "deploy-" + uuid.uuid4().hex,
                                         "password": self.password})
            await self.request("listen_event", {"namespace": "admin", "event": "app_initialized"})
            self.baseline = {name: (state.get("state"), state.get("last_changed"))
                             for name, state in (await self.states()).items()}
            self.errors.open()
        except BaseException:
            self.errors.close()
            await self.session.close()
            raise
        return self

    async def __aexit__(self, *_):
        self.errors.close()
        await self.session.close()

    async def request(self, kind, data):
        await self.ws.send_json({"request_type": kind, "request_id": kind, "data": data})
        reply = await self.ws.receive_json(timeout=15)
        if reply.get("response_id") != kind or reply.get("response_success") is not True:
            raise DeployError("AppDaemon websocket " + kind + " was not acknowledged")

    async def states(self):
        headers = {"x-ad-access": self.password} if self.password else {}
        async with self.session.get(self.base + "/api/appdaemon/state/admin", headers=headers) as response:
            if response.status != 200:
                raise DeployError("AppDaemon admin state request failed")
            return (await response.json())["state"]

    async def completed(self, expected, timeout):
        seen = set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            states = await self.states()
            if hasattr(self, "errors"):
                self.errors.check()
            failed = {name for name in expected if states.get("app." + name, {}).get("state")
                      in {"initialize_error", "compile_error", "dependency_error", "error"}
                      and (states["app." + name].get("state"), states["app." + name].get("last_changed"))
                      != getattr(self, "baseline", {}).get("app." + name)}
            if failed:
                raise DeployError("App initialization failed: " + ", ".join(sorted(failed)))
            if expected <= seen and all(states.get("app." + name, {}).get("state") == "idle" for name in expected):
                unrelated = sorted(name[4:] for name, state in states.items()
                                   if name.startswith("app.") and name[4:] not in expected
                                   and state.get("state", "").endswith("_error"))
                if unrelated:
                    note("WARN: unrelated apps have errors: " + ", ".join(unrelated))
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                event = await self.ws.receive_json(timeout=min(1, remaining))
            except asyncio.TimeoutError:
                continue
            data = event.get("data", {})
            if event.get("response_type") == "event" and data.get("namespace") == "admin" \
                    and data.get("event_type") == "app_initialized":
                seen.add(data.get("data", {}).get("app"))
        raise DeployError("Reload completion timed out; missing successful initialization: "
                          + ", ".join(sorted(expected - seen)))


async def deploy(args, stream=sys.stdin):
    app_dir, exclude, base, password = read_settings(args.config)
    root = app_dir.parent
    request = json.loads(stream.readline())
    files = request["files"]
    for name, value in files.items():
        path = Path(name)
        if not name.startswith("apps/") or ".." in path.parts or not isinstance(value, str) or len(value) != 64:
            raise DeployError("Invalid tracked-file manifest")
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(args.state_dir / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("Another cooperating deploy holds the lock") from None
        await enforce_production_mode(base, password)
        journal = args.state_dir / "pending.json"
        previous = json.loads(journal.read_text()) if journal.exists() else None
        try:
            current = snapshot(app_dir, exclude)
        except Exception:
            if previous is None:
                raise
            # A broken module/config from a partial or failed transfer must not
            # prevent the retry from sending repaired bytes.
            current = previous["before"]
            note("WARN: current source cannot be parsed; retrying from the pending baseline")
        changed = {name for name, value in files.items()
                   if not (root / name).is_file() or digest(root / name) != value}
        pending = {"version": 1, "before": merge_before(previous["before"], current) if previous else current,
                   "changed": sorted(changed | set(previous["changed"] if previous else [])),
                   "expected": previous["expected"] if previous else [],
                   "mtimes": dict(previous["mtimes"]) if previous else {}}
        for path, stamp in current["mtimes"].items():
            pending["mtimes"][path] = max(stamp, pending["mtimes"].get(path, 0))
        # Persist before any file can be overwritten, including transfer failures.
        save_pending(journal, pending)
        emit(status="ready", changed=len(changed), retry=previous is not None)
        result = await asyncio.to_thread(stream.readline)
        if not result or json.loads(result).get("returncode") != 0:
            raise DeployError("Transfer failed or disconnected; pending deploy retained")
        verify_content(root, files)
        after = snapshot(app_dir, exclude)
        affected = affected_apps(pending["before"], after, pending["changed"], pending["expected"])
        pending["expected"] = sorted(affected)
        save_pending(journal, pending)
        check_supported(pending["before"], after, affected, pending["changed"])
        expected = {name for name in affected if name in after["apps"] and not after["apps"][name]["disabled"]}
        if expected:
            stamps = module_timestamps(app_dir, after, expected, pending["mtimes"], pending["changed"])
            pending["mtimes"].update(stamps)
            save_pending(journal, pending)
            async with Witness(base, password) as witness:
                # The subscription is acknowledged before mtime invalidation and
                # the explicit scan. Config key removals need this on AD 4.5.13.
                touch_modules(root, stamps)
                os.kill(args.pid, signal.SIGUSR2)
                await witness.completed(expected, args.timeout)
        verify_content(root, files)
        journal.unlink()
        emit(status="ok", verified=len(files), initialized=sorted(expected), reload=bool(expected))
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("/conf/appdaemon.yaml"))
    parser.add_argument("--state-dir", type=Path, default=Path("/conf/.deploy"))
    parser.add_argument("--pid", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    try:
        if args.manifest:
            emit(**manifest(Path.cwd()))
        else:
            asyncio.run(deploy(args))
    except DeployError as error:
        emit(status="error", error=str(error))
        return 1
    except Exception as error:
        # Config validation may embed private args/secrets in its exception.
        emit(status="error", error="Deploy helper failed (" + type(error).__name__
             + "); check on-box configuration and logs; pending deploy retained")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
