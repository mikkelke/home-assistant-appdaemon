import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

from test_radio_watchdog_seeding import make_app


BEDROOM = "media_player.bedroom_2"
BATHROOM = "media_player.bathroom_2"
KITCHEN = "media_player.kitchen_2"


def watchdog():
    app = make_app([BEDROOM, BATHROOM, KITCHEN])
    app._suppressed_until = {}
    app._stop_generations = {}
    app.enabled = True
    app.min_play_minutes = 10
    app.cooldown_minutes = 60
    app.check_delay_s = 12
    app.run_in = Mock()
    app.call_service = AsyncMock()
    app.fire_event = AsyncMock()
    app._notifier = Mock(notify=AsyncMock())
    app.friendly_name = lambda player: player
    for player in app.players:
        app._playing_since[player] = datetime.now() - timedelta(minutes=20)
        app._last_content[player] = "https://dr.dk/radio.m3u8"
    return app


class IntentionalStop(unittest.TestCase):
    def test_stop_only_forgets_requested_wake_players(self):
        app = watchdog()
        app.suppress_restart([BEDROOM, BATHROOM, "media_player.unknown"])
        self.assertEqual(set(app._playing_since), {KITCHEN})
        self.assertEqual(set(app._last_content), {KITCHEN})
        self.assertEqual(set(app._suppressed_until), {BEDROOM, BATHROOM})
        app._on_player(BEDROOM, "state", "playing", "idle", {})
        app.run_in.assert_not_called()

    def test_queued_retry_cannot_restart_after_suppression_expires(self):
        app = watchdog()
        app.get_state = AsyncMock(return_value="idle")
        app.suppress_restart([BEDROOM])
        app._suppressed_until[BEDROOM] = datetime.now() - timedelta(seconds=1)
        asyncio.run(app._check_dead_async(BEDROOM))
        app.call_service.assert_not_awaited()

    def test_stop_during_async_group_read_invalidates_retry(self):
        app = watchdog()

        async def get_state(player, attribute=None):
            if attribute == "group_members":
                app.suppress_restart([player])
                app._suppressed_until[player] = datetime.now() - timedelta(seconds=1)
                return [player]
            return "idle"

        app.get_state = get_state
        asyncio.run(app._check_dead_async(BEDROOM))
        app.call_service.assert_not_awaited()
        self.assertNotIn(BEDROOM, app._last_attempt)

    def test_unrelated_radio_still_recovers(self):
        app = watchdog()
        app.suppress_restart([BEDROOM, BATHROOM])

        async def get_state(player, attribute=None):
            return [player] if attribute == "group_members" else "idle"

        app.get_state = get_state
        asyncio.run(app._check_dead_async(KITCHEN))
        app.call_service.assert_awaited_once_with(
            "media_player/play_media", entity_id=KITCHEN,
            media_content_id="https://dr.dk/radio.m3u8", media_content_type="music",
        )

    def test_deliberate_stop_skips_stale_retry_notification(self):
        app = watchdog()
        app.get_state = AsyncMock(return_value="idle")
        app.suppress_restart([BEDROOM])
        asyncio.run(app._verify_async(BEDROOM, "https://dr.dk/radio.m3u8"))
        app._notifier.notify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
