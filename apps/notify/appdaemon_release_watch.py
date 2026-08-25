"""
AppDaemon release watch - weekly tripwire that tells Mikkel when it's worth
revisiting the check_app_updates() workaround.

Background: AppDaemon 4.5.13's check_app_updates() re-``ast.parse()``s every app .py
file on the event loop on EVERY utility-loop pass - measured on the box: ~2.2s per
pass, 86% of the event loop blocked, a core pinned (upstream issue #2599, AppDaemon/
appdaemon, still unfixed on the ``dev`` branch, no released fix). The workaround in
``/data/appdaemon/appdaemon.yaml`` is ``utility_delay: 15`` + ``max_utility_skew: 30``,
which trades ~1s -> ~17s of app-reload detection latency for ~7% CPU (see
``scripts/deploy.sh``'s ``sleep 25``, which has to outlast it). The docker image is now
pinned by digest in ``/data/docker-compose.yaml``, so upgrading is a deliberate act, not
something that happens on its own - this app is the tripwire that makes it worth
reconsidering: it polls the public GitHub API (no auth needed) once a week for (1) the
latest AppDaemon/appdaemon release and (2) issue #2599's open/closed state, and pushes
ONE notification to Mikkel only, and only when either actually changed since the last
check.

Seeding: the very first run (no state file yet) NEVER notifies, even if the latest
release already differs from the pinned version or the issue is already closed - there
is no prior baseline to call that "new" against, so the first observation just becomes
the baseline. Every run after that compares against the last-seen values and notifies
on a genuine change: a different release tag, or the issue transitioning open -> closed.

Threading: the GitHub calls are plain blocking ``requests`` run via
``self.submit_to_executor`` so they never sit on this app's pinned callback thread -
see apps/intercom/intercom.py's sonos offload (and its comments) for why that rule
exists in this repo: a blocking call on the pinned thread stalls every other callback
queued behind it. ``fetch_snapshot`` below is a pure module-level function (no ``self``
access) so it is safe to run off-thread and is directly unit-testable without an
AppDaemon runtime. Its result comes back to THIS app's pinned thread via
submit_to_executor's own ``callback=`` mechanism - AppDaemon dispatches that callback
through its normal scheduler queue (honoring pin_app/pin_thread), so the notify-or-not
decision and all state-file writes still happen on the pinned thread, same as every
other callback in this app.
"""

import json
import os

import requests  # type: ignore

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

GITHUB_API = "https://api.github.com"

_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def fetch_snapshot(repo, issue_number, timeout_s):
    """Latest-release + issue-state snapshot from the public GitHub API. Pure and
    self-free (see module docstring: "Threading") - runs on AppDaemon's executor
    thread pool, never on this app's pinned thread. NEVER raises: any failure
    (network error, timeout, non-2xx status, malformed body) is reported back as
    ``{"ok": False, "error": ...}`` instead."""
    try:
        release_resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/releases/latest", timeout=timeout_s
        )
        release_resp.raise_for_status()
        release = release_resp.json()

        issue_resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/issues/{issue_number}", timeout=timeout_s
        )
        issue_resp.raise_for_status()
        issue = issue_resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    tag_name = release.get("tag_name")
    issue_state = issue.get("state")
    if not tag_name or not issue_state:
        return {
            "ok": False,
            "error": f"malformed GitHub API response (tag={tag_name!r}, issue_state={issue_state!r})",
        }

    return {
        "ok": True,
        "tag_name": tag_name,
        "html_url": release.get("html_url"),
        "published_at": release.get("published_at"),
        "issue_state": issue_state,
    }


class AppdaemonReleaseWatch(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.pinned_version = str(a("pinned_version", "4.5.13"))
        self.repo = str(a("github_repo", "AppDaemon/appdaemon"))
        self.issue_number = int(a("issue_number", 2599))
        self.request_timeout_s = float(a("request_timeout_seconds", 10))
        self.notify_target = a("notify_target", "mikkel")
        self.state_file = a("state_file", "/conf/apps/notify/appdaemon_release_watch_state.json")
        self.startup_delay_s = float(a("startup_check_delay_seconds", 90))
        self.check_time = str(a("check_time", "09:00:00"))

        day_name = str(a("check_day", "monday")).strip().lower()
        self.check_weekday = _WEEKDAY_NAMES.get(day_name)
        if self.check_weekday is None:
            self.log(f"Unknown check_day '{day_name}' - defaulting to monday", level="WARNING")
            self.check_weekday = 0

        self._state = self._load_state()

        # get_app must be resolved in sync initialize() - from an async context AD
        # hands back a dead Task instead of the app instance (same note as elsewhere
        # in this codebase, e.g. deploy_advisor.py / weather_opening_alert.py).
        self._notifier = self.get_app("MobileNotifier")
        if self._notifier is None:
            self.log("MobileNotifier app not found - release notifications will not be sent", level="WARNING")

        self.run_daily(self._on_weekly_tick, self.check_time)
        # A restart landing right around the weekly slot must not silently skip a
        # whole week - also check once shortly after startup. Deliberately not AT
        # init: this app has nothing time-critical, so it shouldn't compete with
        # everything else's initialize().
        self.run_in(self._on_startup_tick, self.startup_delay_s)

        self.log(
            f"AppdaemonReleaseWatch: watching {self.repo} (pinned {self.pinned_version}) "
            f"+ issue #{self.issue_number}, weekly {day_name} {self.check_time}, "
            f"startup check in {self.startup_delay_s:.0f}s"
        )

    # -- scheduling (pinned thread) --------------------------------------------------
    def _on_weekly_tick(self, kwargs=None):
        if self.get_now().weekday() != self.check_weekday:
            return
        self._start_check()

    def _on_startup_tick(self, kwargs=None):
        self._start_check()

    def _start_check(self):
        self.log("Checking for a new AppDaemon release / issue #2599 state...", level="DEBUG")
        self.submit_to_executor(
            fetch_snapshot,
            self.repo,
            self.issue_number,
            self.request_timeout_s,
            callback=self._on_snapshot,
        )

    # -- executor result, dispatched back onto the pinned thread ---------------------
    def _on_snapshot(self, result=None, **kwargs):
        try:
            self._handle_snapshot(result or {})
        except Exception as e:
            self.log(f"release-watch snapshot handling failed: {e}", level="ERROR")

    def _handle_snapshot(self, snapshot):
        if not snapshot.get("ok"):
            self.log(f"AppDaemon release/issue check failed: {snapshot.get('error', 'unknown error')}", level="WARNING")
            return

        tag = snapshot["tag_name"]
        release_url = snapshot.get("html_url")
        issue_state = snapshot["issue_state"]

        prev_tag = self._state.get("last_seen_tag")
        prev_issue_state = self._state.get("last_seen_issue_state")
        first_run = prev_tag is None or prev_issue_state is None

        if first_run:
            # Seed only - see module docstring "Seeding". Whatever GitHub reports
            # right now simply becomes the baseline; nothing to compare it against yet.
            self._state["last_seen_tag"] = tag
            self._state["last_seen_issue_state"] = issue_state
            self._save_state()
            self.log(
                f"Seeded release-watch state: latest release {tag}, issue #{self.issue_number} "
                f"is {issue_state} (pinned version {self.pinned_version}) - no notification on first run",
                level="INFO",
            )
            return

        new_release = tag != prev_tag
        issue_newly_closed = issue_state == "closed" and prev_issue_state != "closed"

        if new_release or issue_newly_closed:
            message = self._build_message(tag, release_url, issue_state, new_release)
            self._notify(message)
            self.log(f"release-watch notified: {message}")

        self._state["last_seen_tag"] = tag
        self._state["last_seen_issue_state"] = issue_state
        self._save_state()

    def _build_message(self, tag, release_url, issue_state, new_release):
        """Short + actionable (see module docstring). ``issue_state`` is whatever was
        JUST fetched, so the revert hint is included whenever the issue is CURRENTLY
        closed - not only on the cycle it flipped - since that fact stays relevant
        for as long as the box hasn't been upgraded past pinned_version."""
        issue_url = f"https://github.com/{self.repo}/issues/{self.issue_number}"
        revert_hint = (
            f"Issue #{self.issue_number} is closed - once you upgrade past {self.pinned_version}, "
            "the utility_delay/max_utility_skew workaround in appdaemon.yaml and the 25s wait "
            "in scripts/deploy.sh can potentially be reverted."
        )
        if new_release:
            parts = [f"AppDaemon {tag} is out (pinned: {self.pinned_version})."]
            if issue_state == "closed":
                parts.append(revert_hint)
            if release_url:
                parts.append(release_url)
            return " ".join(parts)
        # Issue closed this cycle, release tag unchanged - no new tag to name.
        return f"{revert_hint} {issue_url}"

    def _notify(self, message):
        if self._notifier is None:
            self.log("MobileNotifier unavailable - cannot push release-watch notification", level="WARNING")
            return
        try:
            self.create_task(
                self._notifier.notify(title="AppDaemon update", message=message, target=self.notify_target)
            )
        except Exception as e:
            self.log(f"release-watch notify failed: {e}", level="WARNING")

    # -- state persistence (last_seen_tag / last_seen_issue_state only) --------------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                d = json.load(f)
            return {
                "last_seen_tag": d.get("last_seen_tag"),
                "last_seen_issue_state": d.get("last_seen_issue_state"),
            }
        except Exception:
            return {"last_seen_tag": None, "last_seen_issue_state": None}

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed ({e}) - continuing in-memory", level="WARNING")
