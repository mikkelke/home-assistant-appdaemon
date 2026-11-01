import unittest
from unittest.mock import MagicMock

import test_wakeup_snooze as fixtures


class SessionActions(unittest.TestCase):
    make_app = fixtures.WakeSession.make_app
    drain_notifications = fixtures.WakeSession.drain_notifications

    def test_late_phone_actions_cannot_control_current_session(self):
        for action, status in (("WAKE_SNOOZE", "ringing"), ("WAKE_DISMISS", "ringing"),
                               ("WAKE_SNOOZE_CANCEL", "snoozing"), ("WAKE_NOW", "snoozing"),
                               ("WAKE_OPTIONS", "ringing")):
            with self.subTest(action=action):
                app = self.make_app()
                app._session_id = "current"
                app._session_status = status
                methods = ("_start_snooze", "_dismiss_wake", "_cancel_snooze", "_on_wake_now", "_queue_wake_notification")
                for method in methods:
                    setattr(app, method, MagicMock())
                app._on_notification_action("mobile_app_notification_action", {"action": action + "|old"}, {})
                for method in methods:
                    getattr(app, method).assert_not_called()
                app._on_notification_action("mobile_app_notification_action", {"action": action + "|current"}, {})
                self.assertEqual(sum(getattr(app, method).call_count for method in methods), 1)

    def test_scripts_reject_stale_ids_and_accept_current_or_missing_id(self):
        for event, method, payload in (("wake_snooze", "_start_snooze", {"minutes": 5, "curtain": "open"}),
                                       ("wake_dismiss", "_dismiss_wake", {}),
                                       ("wake_snooze_cancel", "_cancel_snooze", {})):
            with self.subTest(event=event):
                app = self.make_app()
                app._session_id = "current"
                handler = MagicMock()
                setattr(app, method, handler)
                app._on_wake_action(event, dict(payload, session_id="old"), {})
                handler.assert_not_called()
                for session in ({"session_id": "current"}, {}, {"session_id": ""}):
                    app._on_wake_action(event, dict(payload, **session), {})
                self.assertEqual(handler.call_count, 3)

    def test_ids_are_unique_even_for_two_wakes_at_the_same_clock_time(self):
        app = self.make_app()
        app._start_wake_sequence()
        first = app._session_id
        app._start_wake_sequence()
        self.assertNotEqual(app._session_id, first)
        self.assertEqual(app._state["wake_session"]["session_id"], app._session_id)
        self.assertEqual(app.set_state.call_args.kwargs["attributes"]["session_id"], app._session_id)

    def test_delivered_old_notification_dismiss_cannot_stop_new_wake(self):
        app = self.make_app()
        app._start_wake_sequence()
        self.drain_notifications(app)
        action = app.notifier.notify.await_args.kwargs["data"]["actions"][-1]["action"]
        app._start_wake_sequence()
        app.services.clear()
        app._on_notification_action("mobile_app_notification_action", {"action": action}, {})
        self.assertEqual(app._session_status, "ringing")
        self.assertEqual(app.services, [])

    def test_notifications_embed_the_published_session_id_in_every_action(self):
        app = self.make_app()
        app._session_id = "current"
        for kind in ("ringing", "snoozing"):
            if kind == "snoozing":
                app._start_snooze(5, "open")
            else:
                app._queue_wake_notification(kind)
            self.drain_notifications(app)
            actions = app.notifier.notify.await_args.kwargs["data"]["actions"]
            self.assertTrue(all(action["action"].endswith("|current") for action in actions))
