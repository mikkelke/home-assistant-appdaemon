import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hassmod
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import control  # noqa: E402

ERROR_SENSOR = "sensor.rober2_vacuum_error"
PAUSED = "input_boolean.rober2_automation_paused"
STOPPED_DATA = {"data": {"importance": "high", "channel": "alerts"}}
RESOLVED_DATA = {"data": {"importance": "default", "channel": "alerts"}}


class ProblemRecipientsTests(unittest.TestCase):
    def test_residents_at_home_in_resident_order(self):
        states = {"kristine": "home", "claudia": "home", "mikkel": "home"}
        self.assertEqual(control.problem_recipients(states, []), ["mikkel", "claudia", "kristine"])

    def test_told_residents_are_left_out(self):
        states = {"mikkel": "home", "claudia": "home", "kristine": "home"}
        self.assertEqual(control.problem_recipients(states, ["claudia"]), ["mikkel", "kristine"])

    def test_only_the_home_state_counts(self):
        for state in ("not_home", "Office", "unknown", "unavailable", None):
            with self.subTest(state=state):
                self.assertEqual(control.problem_recipients({"mikkel": state}, []), [])

    def test_missing_residents_are_not_home(self):
        self.assertEqual(control.problem_recipients({}, []), [])
        self.assertEqual(control.problem_recipients({"mikkel": "home"}, []), ["mikkel"])


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.failing = set()

    async def notify(self, title, message, target, data=None):
        await asyncio.sleep(0)
        self.sent.append({"title": title, "message": message, "target": list(target), "data": data})
        return 0 if self.failing.intersection(target) else len(target)


class ProblemCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state_file = self.dir / "problems.json"
        self.states = {
            "person.mikkel": "not_home",
            "person.claudia": "not_home",
            "person.kristine": "not_home",
            ERROR_SENSOR: "none",
            PAUSED: "off",
        }
        self.notifier = FakeNotifier()
        self.apps = []
        self.app = self.start_app()

    def tearDown(self):
        for app in self.apps:
            errors = [call for call in app.log.call_args_list if call.kwargs.get("level") == "ERROR"]
            self.assertEqual(errors, [])

    def start_app(self):
        app = control.Rober2Control.__new__(control.Rober2Control)
        app.args = {"map_save_dir": str(self.dir / "maps"), "problem_state_file": str(self.state_file)}
        app.get_app = Mock(return_value=self.notifier)
        app.get_state = AsyncMock(side_effect=lambda entity, **kwargs: self.states.get(entity))
        for name in ("log", "log_state_conclusion", "listen_state", "run_in", "run_daily"):
            setattr(app, name, Mock())
        for name in ("call_service", "_set_narrative", "_report_house_event", "stop_cleaning", "save_map_image"):
            setattr(app, name, AsyncMock())
        app.initialize()
        self.apps.append(app)
        return app

    def sent(self):
        return [(push["title"], push["target"]) for push in self.notifier.sent]

    def home(self, *names):
        for name in ("mikkel", "claudia", "kristine"):
            self.states[f"person.{name}"] = "home" if name in names else "not_home"

    async def arrive(self, name, app=None):
        self.states[f"person.{name}"] = "home"
        await (app or self.app).handle_problem_arrival(f"person.{name}", "state", "not_home", "home", {})

    async def error(self, new, old="none"):
        self.states[ERROR_SENSOR] = new
        await self.app.handle_vacuum_error(ERROR_SENSOR, "state", old, new, {})

    def stored(self):
        return json.loads(self.state_file.read_text())


class ErrorPushTests(ProblemCase):
    async def test_error_while_cleaning_reaches_only_residents_at_home(self):
        self.home("mikkel", "claudia")
        self.app.cleaning = True
        self.app.current_room = "22"
        await self.error("robot_trapped")
        self.assertEqual(self.sent(), [
            ("Rober2 Error - Cleaning Stopped", ["mikkel"]),
            ("Rober2 Error - Cleaning Stopped", ["claudia"]),
        ])
        self.assertEqual(
            self.notifier.sent[0]["message"],
            "Critical error detected: Robot Trapped\nRoom: kitchen\nCleaning has been stopped.",
        )
        self.assertEqual(self.notifier.sent[0]["data"], STOPPED_DATA)
        self.app.stop_cleaning.assert_awaited_once()
        logged = [c.args[0] for c in self.app.log.call_args_list if c.kwargs.get("level") == "ERROR"]
        self.assertEqual(logged, ["Critical/blocking error 'robot_trapped' detected during cleaning - stopping cleaning for room kitchen"])
        self.app.log.reset_mock()

    async def test_error_while_idle_reaches_only_residents_at_home(self):
        self.home("kristine")
        await self.error("wheels_jammed")
        self.assertEqual(self.sent(), [("Rober2 Error Detected", ["kristine"])])
        self.assertEqual(
            self.notifier.sent[0]["message"],
            "Critical error detected: Wheels Jammed\nAutomation is paused.",
        )
        self.assertEqual(self.notifier.sent[0]["data"], STOPPED_DATA)

    async def test_nobody_home_sends_nothing_then_each_arrival_is_told_once(self):
        await self.error("robot_trapped")
        self.assertEqual(self.sent(), [])

        await self.arrive("claudia")
        self.assertEqual(self.sent(), [("Rober2 Error Detected", ["claudia"])])

        await self.arrive("kristine")
        self.assertEqual(self.sent(), [
            ("Rober2 Error Detected", ["claudia"]),
            ("Rober2 Error Detected", ["kristine"]),
        ])

        self.states["person.claudia"] = "not_home"
        await self.arrive("claudia")
        self.assertEqual(len(self.sent()), 2)

    async def test_resolved_push_goes_only_to_those_told(self):
        await self.error("robot_trapped")
        await self.arrive("claudia")
        await self.arrive("kristine")
        self.notifier.sent.clear()

        await self.error("none", old="robot_trapped")
        self.assertEqual(self.sent(), [("Rober2 Error Resolved", ["claudia", "kristine"])])
        self.assertEqual(
            self.notifier.sent[0]["message"],
            "Error 'Robot Trapped' has been cleared.\nAutomation resuming.",
        )
        self.assertEqual(self.notifier.sent[0]["data"], RESOLVED_DATA)

    async def test_resolved_push_reaches_a_told_resident_who_has_left(self):
        self.home("mikkel")
        await self.error("robot_trapped")
        self.home()
        self.notifier.sent.clear()
        await self.error("none", old="robot_trapped")
        self.assertEqual(self.sent(), [("Rober2 Error Resolved", ["mikkel"])])

    async def test_nobody_told_means_no_resolved_push(self):
        await self.error("robot_trapped")
        await self.error("none", old="robot_trapped")
        self.assertEqual(self.sent(), [])

    async def test_arrival_after_the_error_cleared_gets_nothing(self):
        await self.error("robot_trapped")
        await self.error("none", old="robot_trapped")
        await self.arrive("mikkel")
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.stored(), {})

    async def test_non_critical_error_pushes_nothing_when_it_starts_or_clears(self):
        self.home("mikkel")
        await self.error("mop_pad_missing")
        await self.error("none", old="mop_pad_missing")
        self.assertEqual(self.sent(), [])

    async def test_a_different_error_is_a_new_episode(self):
        self.home("mikkel")
        await self.error("robot_trapped")
        await self.error("wheels_jammed", old="robot_trapped")
        await self.arrive("claudia")
        self.assertEqual(self.sent(), [
            ("Rober2 Error Detected", ["mikkel"]),
            ("Rober2 Error Detected", ["mikkel"]),
            ("Rober2 Error Detected", ["claudia"]),
        ])
        self.assertIn("Robot Trapped", self.notifier.sent[0]["message"])
        self.assertIn("Wheels Jammed", self.notifier.sent[1]["message"])
        self.notifier.sent.clear()
        await self.error("none", old="wheels_jammed")
        self.assertEqual(self.sent(), [("Rober2 Error Resolved", ["mikkel", "claudia"])])

    async def test_the_same_problem_again_does_not_repeat_to_those_told(self):
        self.home("mikkel")
        for _ in range(2):
            await self.app._open_problem("error", "T", "M")
        self.assertEqual(self.sent(), [("T", ["mikkel"])])
        await self.arrive("claudia")
        await self.app._open_problem("error", "T", "M")
        self.assertEqual(self.sent(), [("T", ["mikkel"]), ("T", ["claudia"])])

    async def test_failed_delivery_is_not_told_and_is_retried_when_someone_arrives(self):
        self.home("mikkel")
        self.notifier.failing.add("mikkel")
        await self.error("robot_trapped")
        self.assertEqual(self.stored()["error"]["told"], [])

        self.notifier.failing.clear()
        await self.arrive("claudia")
        self.assertEqual(self.stored()["error"]["told"], ["mikkel", "claudia"])

    async def test_failed_delivery_gets_no_resolved_push(self):
        self.home("mikkel", "claudia")
        self.notifier.failing.add("mikkel")
        await self.error("robot_trapped")
        self.notifier.sent.clear()
        await self.error("none", old="robot_trapped")
        self.assertEqual(self.sent(), [("Rober2 Error Resolved", ["claudia"])])

    async def test_only_arrivals_trigger_pushes(self):
        await self.error("robot_trapped")
        self.home("mikkel")
        handler = self.app.handle_problem_arrival
        await handler("person.mikkel", "state", "home", "home", {})
        await handler("person.mikkel", "state", "home", "not_home", {})
        await handler("person.mikkel", "state", "not_home", "Office", {})
        self.assertEqual(self.sent(), [])
        await handler("person.mikkel", "state", "unknown", "home", {})
        self.assertEqual(self.sent(), [("Rober2 Error Detected", ["mikkel"])])

    async def test_arrival_and_a_starting_problem_at_once_push_once(self):
        self.home("claudia")
        await asyncio.gather(
            self.app._open_problem("error", "T", "M"),
            self.app.handle_problem_arrival("person.claudia", "state", "not_home", "home", {}),
        )
        self.assertEqual(self.sent(), [("T", ["claudia"])])


class DockedFailureTests(ProblemCase):
    """Three failed tries in a room end in a stuck or unreachable push; both docking branches send it."""

    def dock_pretty(self):
        return self.app._dock_pretty_name()

    def restart_clean(self):
        self.state_file.unlink(missing_ok=True)
        self.notifier.sent.clear()
        self.app = self.start_app()

    async def dock_after_three_tries(self, room_id, left_dock, tracked):
        app = self.app
        app._robot_has_left_dock = left_dock
        app._room_interrupt_counts = {room_id: 2}
        app.peak_progress = 0
        if tracked:
            app.current_room = room_id
            app.room_start_time = datetime.now() - timedelta(seconds=120)
        else:
            app.current_room = None
            app._last_attempted_room_id = room_id
            app._last_segment_command_time = datetime.now() - timedelta(seconds=60)
        await app.handle_vacuum_state("vacuum.rober2", "state", "cleaning", "docked", {})

    def expected(self):
        dock = self.dock_pretty()
        return {
            "dock": (
                "stuck",
                f"Rober2 - Stuck in {dock}",
                f"Robot got stuck trying to clean {dock} after 3 attempts.",
            ),
            "exit": (
                "stuck",
                f"Rober2 - Stuck trying to leave {dock}",
                f"Robot cannot leave {dock} after 3 attempts. Something is blocking the exit.",
            ),
            "room": (
                "unreachable:hallway",
                "Rober2 - Room unreachable",
                "Could not reach hallway after 3 attempts. Skipping for this session.",
            ),
        }

    def cases(self):
        return (("dock", "25", False), ("exit", "22", False), ("room", "17", True))

    async def test_failure_reaches_only_residents_at_home(self):
        for tracked in (True, False):
            for name, room_id, left_dock in self.cases():
                with self.subTest(case=name, tracked=tracked):
                    self.restart_clean()
                    self.home("claudia")
                    kind, title, message = self.expected()[name]
                    await self.dock_after_three_tries(room_id, left_dock, tracked)
                    self.assertEqual(self.sent(), [(title, ["claudia"])])
                    self.assertEqual(self.notifier.sent[0]["message"], message)
                    self.assertEqual(self.app._problems[kind]["told"], ["claudia"])

    async def test_failure_with_nobody_home_waits_for_an_arrival(self):
        for tracked in (True, False):
            for name, room_id, left_dock in self.cases():
                with self.subTest(case=name, tracked=tracked):
                    self.restart_clean()
                    self.home()
                    kind, title, message = self.expected()[name]
                    await self.dock_after_three_tries(room_id, left_dock, tracked)
                    self.assertEqual(self.sent(), [])
                    await self.arrive("mikkel")
                    await self.arrive("kristine")
                    self.assertEqual(self.sent(), [(title, ["mikkel"]), (title, ["kristine"])])
                    self.assertEqual(self.notifier.sent[1]["message"], message)

    async def test_stuck_pauses_the_automation_and_ends_when_the_pause_is_reset(self):
        await self.dock_after_three_tries("25", False, True)
        self.app.call_service.assert_any_await("input_boolean/turn_on", entity_id=PAUSED)
        await self.arrive("mikkel")
        self.assertEqual(len(self.sent()), 1)

        self.states[PAUSED] = "off"
        await self.app.handle_automation_paused_reset(PAUSED, "state", "on", "off", {})
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.stored(), {})

        await self.arrive("claudia")
        self.assertEqual(len(self.sent()), 1)

    async def test_pause_turning_on_does_not_end_the_stuck_episode(self):
        await self.dock_after_three_tries("25", False, True)
        await self.app.handle_automation_paused_reset(PAUSED, "state", "off", "on", {})
        await self.arrive("mikkel")
        self.assertEqual(len(self.sent()), 1)

    async def test_unreachable_room_ends_when_that_room_is_cleaned(self):
        await self.dock_after_three_tries("17", True, True)
        await self.app.mark_room_cleaned("22")
        await self.arrive("mikkel")
        self.assertEqual(len(self.sent()), 1)

        await self.app.mark_room_cleaned("17")
        self.assertEqual(self.stored(), {})
        await self.arrive("claudia")
        self.assertEqual(len(self.sent()), 1)

    async def test_stuck_and_error_episodes_end_independently(self):
        await self.dock_after_three_tries("25", False, True)
        await self.error("robot_trapped")
        await self.error("none", old="robot_trapped")
        self.assertEqual(list(self.stored()), ["stuck"])
        await self.arrive("mikkel")
        self.assertEqual([title for title, _ in self.sent()], [f"Rober2 - Stuck in {self.dock_pretty()}"])


class ReloadTests(ProblemCase):
    async def test_reload_keeps_the_pending_episode(self):
        await self.error("robot_trapped")
        reloaded = self.start_app()
        self.assertEqual(reloaded._problems, self.app._problems)
        await reloaded._drop_resolved_problems({})
        self.assertEqual(list(reloaded._problems), ["error"])

        await self.arrive("claudia", app=reloaded)
        self.assertEqual(self.sent(), [("Rober2 Error Detected", ["claudia"])])
        self.assertEqual(
            self.notifier.sent[0]["message"],
            "Critical error detected: Robot Trapped\nAutomation is paused.",
        )
        self.assertEqual(self.notifier.sent[0]["data"], STOPPED_DATA)

    async def test_reload_remembers_who_was_told(self):
        self.home("mikkel")
        await self.error("robot_trapped")
        reloaded = self.start_app()
        await self.arrive("mikkel", app=reloaded)
        self.assertEqual(len(self.sent()), 1)

        self.app = reloaded
        self.notifier.sent.clear()
        await self.error("none", old="robot_trapped")
        self.assertEqual(self.sent(), [("Rober2 Error Resolved", ["mikkel"])])

    async def test_reload_after_the_error_cleared_drops_the_episode(self):
        await self.error("robot_trapped")
        self.states[ERROR_SENSOR] = "none"
        reloaded = self.start_app()
        await reloaded._drop_resolved_problems({})
        self.assertEqual(reloaded._problems, {})
        self.assertEqual(self.stored(), {})
        await self.arrive("mikkel", app=reloaded)
        self.assertEqual(self.sent(), [])

    async def test_unavailable_error_sensor_counts_as_fine_on_startup(self):
        await self.error("robot_trapped")
        self.states[ERROR_SENSOR] = "unavailable"
        reloaded = self.start_app()
        await reloaded._drop_resolved_problems({})
        self.assertEqual(reloaded._problems, {})

    async def test_reload_keeps_a_stuck_episode_only_while_the_pause_is_on(self):
        await self.app._open_problem("stuck", "Rober2 - Stuck in Kitchen", "Stuck.")
        self.states[PAUSED] = "on"
        reloaded = self.start_app()
        await reloaded._drop_resolved_problems({})
        self.assertEqual(list(reloaded._problems), ["stuck"])

        self.states[PAUSED] = "off"
        reloaded = self.start_app()
        await reloaded._drop_resolved_problems({})
        self.assertEqual(reloaded._problems, {})

    async def test_reload_keeps_an_unreachable_room_episode(self):
        await self.app._open_problem("unreachable:hallway", "Rober2 - Room unreachable", "Hallway.")
        reloaded = self.start_app()
        await reloaded._drop_resolved_problems({})
        self.assertEqual(list(reloaded._problems), ["unreachable:hallway"])

    async def test_startup_schedules_the_check_and_listens_to_the_residents(self):
        self.app.run_in.assert_any_call(self.app._drop_resolved_problems, 3)
        listened = [call.args for call in self.app.listen_state.call_args_list]
        for name in ("mikkel", "claudia", "kristine"):
            self.assertIn((self.app.handle_problem_arrival, f"person.{name}"), listened)

    async def test_no_state_file_is_written_while_nothing_is_pending(self):
        await self.arrive("mikkel")
        await self.app._drop_resolved_problems({})
        await self.error("none", old="robot_trapped")
        self.assertFalse(self.state_file.exists())

    async def test_unreadable_state_file_starts_empty(self):
        for content in ("{not json", "[]", '{"error": {"title": "T"}}'):
            with self.subTest(content=content):
                self.state_file.write_text(content)
                reloaded = self.start_app()
                self.assertEqual(reloaded._problems, {})
                warnings = [c for c in reloaded.log.call_args_list if c.kwargs.get("level") == "WARNING"]
                self.assertEqual(len(warnings), 1)

    async def test_saved_names_outside_the_residents_are_ignored(self):
        self.state_file.write_text(json.dumps(
            {"error": {"title": "T", "message": "M", "data": None, "told": ["mikkel", "guest"]}}
        ))
        self.assertEqual(self.start_app()._problems["error"]["told"], ["mikkel"])


class SendNotificationTests(ProblemCase):
    async def test_returns_the_number_of_devices_reached(self):
        self.assertEqual(await self.app.send_notification("T", "M", target=["mikkel"]), 1)
        self.notifier.failing.add("mikkel")
        self.assertEqual(await self.app.send_notification("T", "M", target=["mikkel"]), 0)

    async def test_returns_zero_without_a_notifier_or_when_it_raises(self):
        self.app.mobile_notifier = None
        self.assertEqual(await self.app.send_notification("T", "M"), 0)

        self.app.mobile_notifier = Mock(notify=AsyncMock(side_effect=RuntimeError("boom")))
        self.assertEqual(await self.app.send_notification("T", "M"), 0)
        self.app.log.reset_mock()


if __name__ == "__main__":
    unittest.main()
