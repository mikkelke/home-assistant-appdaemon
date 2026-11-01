import json
import unittest
from pathlib import Path

import test_wakeup_snooze as fixtures


class SnoozeQuiet(unittest.TestCase):
    make_app = fixtures.WakeSession.make_app

    def test_closed_snooze_publishes_quiet_state_before_any_room_commands(self):
        app = self.make_app()
        seen = []
        app.call_service = lambda service, **kw: seen.append(app.set_state.call_args)
        app._start_snooze(5, "closed")
        self.assertTrue(seen)
        for publication in seen:
            self.assertIsNotNone(publication)
            self.assertEqual(publication.kwargs["state"], "snoozing")
            self.assertEqual(publication.kwargs["attributes"]["snooze_curtain"], "closed")

    def test_restart_reestablishes_quiet_before_stopping_media(self):
        first = self.make_app()
        first._start_snooze(5, "closed")
        app = self.make_app(json.loads(Path(first.state_file).read_text()))
        seen = []
        app.call_service = lambda service, **kw: seen.append(app.set_state.call_args)
        app._restore_wake_session()
        self.assertTrue(seen)
        self.assertTrue(all(publication is not None and publication.kwargs["state"] == "snoozing" for publication in seen))

    def test_every_exit_publishes_release_including_restart(self):
        for end in ("cancel", "dismiss", "wake", "return", "keep"):
            with self.subTest(end=end):
                app = self.make_app()
                app._start_snooze(5, "closed")
                if end == "cancel":
                    app._cancel_snooze()
                elif end == "dismiss":
                    app._dismiss_wake()
                elif end == "wake":
                    app._set_alarm_time_to_now_slot = lambda: None
                    app._on_wake_now(None, None, None, "pressed", {})
                elif end == "return":
                    app._snooze_return(app.scheduled[0][2])
                else:
                    app._start_snooze(5, "open")
                publication = app.set_state.call_args.kwargs
                self.assertFalse(publication["state"] == "snoozing" and publication["attributes"]["snooze_curtain"] == "closed")
                restarted = self.make_app(json.loads(Path(app.state_file).read_text()))
                restarted._restore_wake_session()
                publication = restarted.set_state.call_args.kwargs
                self.assertFalse(publication["state"] == "snoozing" and publication["attributes"]["snooze_curtain"] == "closed")
