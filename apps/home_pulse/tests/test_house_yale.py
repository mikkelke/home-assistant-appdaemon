# Yale keypad notes on the real HouseUpcoming app and its existing fake AppDaemon surface.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import json
import unittest
from datetime import timedelta

from test_house_upcoming import NOW, MIN, base_states, fire, full, iso, make_app
import upcoming_core as core
import upcoming_yale as yale

LOCK = "lock.yale"
OPERATOR = "sensor.yale_operator"
PEOPLE = ("person.mikkel", "person.kristine", "person.claudia")


def lock(state, at):
    return full(state, changed=at)


def operator(name, at, method="keypad"):
    return {**full(name, {"method": method}, changed=at), "last_updated": iso(at)}


def make_yale_app(states=None, **kwargs):
    if states is None:
        states = base_states()
        states.update({person: full("not_home") for person in PEOPLE})
        states[LOCK] = lock("locked", NOW - MIN)
        states[OPERATOR] = operator("Auto Lock", NOW - MIN, "autorelock")
    app = make_app(states=states, **kwargs)
    app.checks = {}
    app.cancelled = []
    sequence = [0]

    def run_in(callback, delay, **kw):
        sequence[0] += 1
        handle = f"timer-{sequence[0]}"
        app.checks[handle] = (app.now + timedelta(seconds=delay), callback, kw)
        return handle

    def cancel_timer(handle):
        app.cancelled.append(handle)
        app.checks.pop(handle, None)

    app.run_in = run_in
    app.cancel_timer = cancel_timer
    return app


def until(app, at):
    while app.checks:
        handle, (when, callback, kw) = min(app.checks.items(), key=lambda item: item[1][0])
        if when > at:
            break
        del app.checks[handle]
        app.now = when
        callback(kw)
    app.now = at


def unlock(app, name="Kristine", method="keypad", at=None):
    at = at or app.now
    app.now = at
    fire(app, LOCK, lock("unlocked", at))
    app.now = at + timedelta(seconds=4)
    fire(app, OPERATOR, operator(name, app.now, method))


def notes(app):
    return [row for row in app.published[-1]["attributes"]["rows"] if row["id"].startswith(yale.ID_PREFIX)]


class YaleNoteTests(unittest.TestCase):
    def test_still_away_after_fifteen_minutes_makes_one_quiet_admin_row(self):
        app = make_yale_app()
        unlock(app)
        until(app, NOW + 15 * MIN - timedelta(seconds=1))
        self.assertEqual(notes(app), [])
        until(app, NOW + 15 * MIN)
        row, = notes(app)
        self.assertEqual(row["title"], "Kristine's code used, not home")
        self.assertEqual(row["detail"], "front door 12:30; phone not home 15 min later")
        self.assertEqual(row["audience"], "admin")
        self.assertEqual(row["done"], "tap")
        self.assertEqual(row["next"], "skip")
        self.assertTrue(core.is_valid_row(row))
        self.assertNotIn(row["title"], [title for title, _ in app._conditions])
        self.assertEqual(app.notifier.sent, [])
        self.assertEqual(app.reports, [])
        app._on_yale_check({"row": row["id"]})
        self.assertEqual(len(notes(app)), 1)

    def test_home_after_ten_minutes_cancels_even_if_away_again(self):
        app = make_yale_app()
        unlock(app)
        _, callback, kw = next(iter(app.checks.values()))
        until(app, NOW + 10 * MIN)
        fire(app, "person.kristine", full("home", changed=app.now))
        self.assertEqual(len(app.cancelled), 1)
        self.assertEqual(app.checks, {})
        fire(app, "person.kristine", full("not_home", changed=app.now))
        until(app, NOW + 15 * MIN)
        callback(kw)  # A callback already queued when cancelled cannot resurrect the note.
        self.assertEqual(notes(app), [])

    def test_home_at_unlock_does_not_start_a_check_even_if_away_at_attribution(self):
        app = make_yale_app()
        fire(app, "person.kristine", full("home"))
        fire(app, LOCK, lock("unlocked", NOW))
        app.now += timedelta(seconds=1)
        fire(app, "person.kristine", full("not_home"))
        app.now += timedelta(seconds=3)
        fire(app, OPERATOR, operator("Kristine", app.now))
        until(app, NOW + 15 * MIN)
        self.assertEqual(app.checks, {})
        self.assertEqual(notes(app), [])

    def test_home_before_delayed_attribution_cancels_the_unlock_snapshot(self):
        app = make_yale_app()
        fire(app, LOCK, lock("unlocked", NOW))
        app.now += timedelta(seconds=1)
        fire(app, "person.kristine", full("home"))
        fire(app, "person.kristine", full("not_home"))
        app.now += timedelta(seconds=3)
        fire(app, OPERATOR, operator("Kristine", app.now))
        until(app, NOW + 15 * MIN)
        self.assertEqual(notes(app), [])
        self.assertEqual(app.checks, {})

    def test_other_methods_and_unknown_operators_are_ignored(self):
        for name, method in (("Kristine", "remote"), ("Kristine", "app"), ("Kristine", "manual"),
                             ("Kristine", "tag"), ("Kristine", None), ("Visitor", "keypad"),
                             ("Kristine2", "keypad"), (None, "keypad")):
            with self.subTest(name=name, method=method):
                app = make_yale_app()
                unlock(app, name=name, method=method)
                until(app, NOW + 15 * MIN)
                self.assertEqual(notes(app), [])
                self.assertEqual(app.checks, {})

    def test_unknown_unavailable_or_missing_presence_at_either_endpoint_is_ignored(self):
        for state in ("unknown", "unavailable", "", None):
            for endpoint in ("unlock", "check"):
                with self.subTest(state=state, endpoint=endpoint):
                    app = make_yale_app()
                    bad = None if state is None else full(state)
                    if endpoint == "unlock":
                        fire(app, "person.kristine", bad)
                    unlock(app)
                    if endpoint == "check":
                        # Exercise the timeout guard directly as well as the listener cancellation.
                        app._cache["person.kristine"] = bad
                    until(app, NOW + 15 * MIN)
                    self.assertEqual(notes(app), [])

    def test_unknown_presence_during_wait_cancels_the_check(self):
        app = make_yale_app()
        unlock(app)
        fire(app, "person.kristine", full("unavailable"))
        fire(app, "person.kristine", full("not_home"))
        until(app, NOW + 15 * MIN)
        self.assertEqual(app.checks, {})
        self.assertEqual(len(app.cancelled), 1)
        self.assertEqual(notes(app), [])

    def test_named_zone_is_known_away(self):
        app = make_yale_app()
        fire(app, "person.kristine", full("Work"))
        unlock(app)
        until(app, NOW + 15 * MIN)
        self.assertEqual(len(notes(app)), 1)

    def test_keypad_lock_and_recovery_to_unlocked_are_ignored(self):
        for old, new in (("unlocked", "locked"), ("locked", "locked"),
                         ("unknown", "unlocked"), ("unavailable", "unlocked"), (None, "unlocked")):
            with self.subTest(old=old, new=new):
                app = make_yale_app()
                app.states[LOCK] = None if old is None else lock(old, NOW - MIN)
                fire(app, LOCK, lock(new, NOW))
                app.now += timedelta(seconds=4)
                fire(app, OPERATOR, operator("Kristine", app.now))
                until(app, NOW + 15 * MIN)
                self.assertEqual(notes(app), [])

    def test_replayed_unlock_does_not_replace_presence_at_the_real_unlock(self):
        app = make_yale_app()
        fire(app, "person.kristine", full("home"))
        fire(app, LOCK, lock("unlocked", NOW))
        app.now += timedelta(seconds=1)
        fire(app, "person.kristine", full("not_home"))
        app._on_source(LOCK, "all", lock("locked", NOW - MIN), app.states[LOCK], {})
        app.now += timedelta(seconds=3)
        fire(app, OPERATOR, operator("Kristine", app.now))
        until(app, NOW + 15 * MIN)
        self.assertEqual(notes(app), [])

    def test_keypad_lock_cannot_be_paired_with_a_recent_manual_unlock(self):
        app = make_yale_app()
        fire(app, LOCK, lock("unlocked", NOW))
        app.now += timedelta(seconds=2)
        fire(app, LOCK, lock("locked", app.now))
        app.now += timedelta(seconds=4)
        fire(app, OPERATOR, operator("Kristine", app.now))
        until(app, NOW + 15 * MIN)
        self.assertEqual(notes(app), [])
        self.assertEqual(app.checks, {})

    def test_delayed_keypad_lock_operator_cannot_name_a_rapid_manual_unlock(self):
        app = make_yale_app()
        app.states[LOCK] = lock("unlocked", NOW - MIN)
        fire(app, LOCK, lock("locked", NOW))
        app.now += timedelta(seconds=2)
        fire(app, LOCK, lock("unlocked", app.now))
        app.now += timedelta(seconds=2)
        fire(app, OPERATOR, operator("Kristine", app.now))
        until(app, NOW + 16 * MIN)
        self.assertEqual(notes(app), [])
        self.assertEqual(app.checks, {})

    def test_metadata_only_operator_update_cannot_name_a_manual_unlock(self):
        app = make_yale_app()
        fire(app, OPERATOR, operator("Kristine", NOW))
        app.now += MIN
        fire(app, LOCK, lock("unlocked", app.now))
        app.now += timedelta(seconds=4)
        updated = operator("Kristine", app.now)
        updated["attributes"]["entity_picture"] = "/local/person.png"
        fire(app, OPERATOR, updated)
        until(app, NOW + 16 * MIN)
        self.assertEqual(notes(app), [])
        self.assertEqual(app.checks, {})

    def test_operator_callback_can_arrive_before_the_unlock_callback(self):
        app = make_yale_app()
        app.now += timedelta(seconds=4)
        fire(app, OPERATOR, operator("Kristine", app.now))
        fire(app, LOCK, lock("unlocked", NOW))
        until(app, NOW + 15 * MIN)
        self.assertEqual(len(notes(app)), 1)

    def test_stale_operator_and_initial_snapshot_are_never_reused(self):
        for source in ("initial", "observed"):
            with self.subTest(source=source):
                app = make_yale_app()
                if source == "initial":
                    app._cache[OPERATOR] = operator("Kristine", NOW)
                else:
                    fire(app, OPERATOR, operator("Kristine", NOW))
                app.now += MIN
                fire(app, LOCK, lock("unlocked", app.now))
                until(app, NOW + 16 * MIN)
                self.assertEqual(notes(app), [])

    def test_operator_too_late_or_old_attribute_replay_is_ignored(self):
        for timestamp, delivered in ((NOW + timedelta(seconds=21), NOW + timedelta(seconds=21)),
                                     (NOW + timedelta(seconds=4), NOW + MIN)):
            with self.subTest(timestamp=timestamp, delivered=delivered):
                app = make_yale_app()
                fire(app, LOCK, lock("unlocked", NOW))
                app.now = delivered
                fire(app, OPERATOR, operator("Kristine", timestamp))
                until(app, NOW + 15 * MIN)
                self.assertEqual(notes(app), [])

    def test_method_only_operator_change_counts_and_duplicates_do_not(self):
        app = make_yale_app()
        fire(app, OPERATOR, operator("Kristine", NOW, "remote"))
        unlock(app)
        saved_lock, saved_operator = app.states[LOCK], app.states[OPERATOR]
        app._on_source(LOCK, "all", lock("locked", NOW - MIN), saved_lock, {})
        fire(app, OPERATOR, saved_operator)
        self.assertEqual(len(app.checks), 1)
        until(app, NOW + 15 * MIN)
        self.assertEqual(len(notes(app)), 1)

    def test_config_maps_all_residents_and_first_word_matches_without_storing_account_name(self):
        app = make_yale_app()
        self.assertEqual({item["person"] for item in app.yale_operators.values()}, set(PEOPLE))
        unlock(app, name="Kristine " + "account suffix")
        until(app, NOW + 15 * MIN)
        self.assertEqual(len(notes(app)), 1)
        self.assertNotIn("account suffix", app.state_file.read_text())
        self.assertNotIn("account suffix", json.dumps(app.published))
        remapped = make_yale_app(yale={"operators": {"Resident": "person.kristine"}})
        unlock(remapped, name="Resident")
        until(remapped, NOW + 15 * MIN)
        self.assertEqual(notes(remapped)[0]["title"], "Resident's code used, not home")

    def test_independent_residents_only_cancel_the_person_who_arrives(self):
        app = make_yale_app()
        unlock(app, name="Kristine")
        app.now = NOW + MIN
        fire(app, LOCK, lock("locked", app.now))
        unlock(app, name="Claudia", at=NOW + 2 * MIN)
        until(app, NOW + 10 * MIN)
        fire(app, "person.kristine", full("home"))
        until(app, NOW + 17 * MIN)
        self.assertEqual([row["title"] for row in notes(app)], ["Claudia's code used, not home"])

    def test_repeated_unlocks_by_same_person_are_distinct(self):
        app = make_yale_app()
        unlock(app)
        app.now = NOW + MIN
        fire(app, LOCK, lock("locked", app.now))
        app.now += timedelta(seconds=4)
        fire(app, OPERATOR, operator("Auto Lock", app.now, "autorelock"))
        unlock(app, at=NOW + 2 * MIN)
        until(app, NOW + 17 * MIN)
        self.assertEqual(len(notes(app)), 2)
        self.assertEqual(len({row["id"] for row in notes(app)}), 2)

    def test_completed_note_and_done_survive_reload_without_replaying_unlock(self):
        app = make_yale_app()
        unlock(app)
        until(app, NOW + 15 * MIN)
        original, = notes(app)
        restored = make_yale_app(states=app.states, state_dir=app.state_dir, now=app.now)
        self.assertEqual(notes(restored), [original])
        self.assertEqual(restored.checks, {})
        restored._on_done("house_upcoming_done", {"row": original["id"]}, {})
        self.assertEqual(notes(restored), [])
        again = make_yale_app(states=app.states, state_dir=app.state_dir, now=app.now + MIN)
        self.assertEqual(notes(again), [])
        again._on_source(LOCK, "all", lock("locked", NOW - MIN), app.states[LOCK], {})
        fire(again, OPERATOR, app.states[OPERATOR])
        until(again, NOW + 40 * MIN)
        self.assertEqual(notes(again), [])
        again._on_undo("house_upcoming_undo", {"row": original["id"]}, {})
        self.assertEqual(notes(again), [original])

    def test_pending_check_is_dropped_on_reload(self):
        app = make_yale_app()
        unlock(app)
        restored = make_yale_app(states=app.states, state_dir=app.state_dir, now=NOW + 10 * MIN)
        until(restored, NOW + 20 * MIN)
        self.assertEqual(restored.checks, {})
        self.assertEqual(notes(restored), [])

    def test_note_stays_until_done_even_after_presence_recovers_and_day_changes(self):
        app = make_yale_app()
        unlock(app)
        until(app, NOW + 15 * MIN)
        original, = notes(app)
        fire(app, "person.kristine", full("home"))
        app.now += timedelta(days=1)
        app._on_tick({})
        self.assertEqual(notes(app), [original])
        app._on_done("house_upcoming_done", {"row": original["id"]}, {})
        self.assertEqual(notes(app), [])


if __name__ == "__main__":
    unittest.main()
