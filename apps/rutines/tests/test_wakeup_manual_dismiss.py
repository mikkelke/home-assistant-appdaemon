import unittest

from test_wakeup_blind_yield import FakeOwner, real_fire_app


def fired(manual):
    app = real_fire_app(FakeOwner(granted=True, hold=False), cover_pos=100)
    app._manual_fire_in_progress = manual
    app._late_catchup_in_progress = not manual
    app.get_state = lambda entity, **kw: {"input_datetime.wakeup_bedroom": "06:00:00",
                                          "input_boolean.wakeup_bedroom": "on"}.get(entity)
    app.published = []
    app.notifications = []
    app._publish_wake_session = lambda: app.published.append((app._session_status, app._session_reason))
    app._queue_wake_notification = app.notifications.append
    app._alarm_fire({})
    return app


class ManualWakeNowStartsDismissed(unittest.TestCase):
    def test_manual_press_never_rings_and_keeps_the_radio(self):
        app = fired(manual=True)
        self.assertEqual(app.published, [("dismissed", "radio_kept")])
        self.assertEqual(app.notifications, ["clear"])
        self.assertTrue(app._radio_playing)

    def test_scheduled_alarm_still_rings(self):
        app = fired(manual=False)
        self.assertEqual(app.published, [("ringing", "")])
        self.assertEqual(app.notifications, ["ringing"])


if __name__ == "__main__":
    unittest.main()
