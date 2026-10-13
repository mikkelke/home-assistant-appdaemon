# tests/test_music_attribution.py - MusicAttribution: who started the music, one row per session.
# Run from repo root: python3 -m unittest discover -s apps/sonos/tests
# Imports the real module by stubbing the appdaemon package (not installed locally).

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

import music_attribution as ma  # noqa: E402

ROOMS = (
    "living_room",
    "kitchen",
    "dining_room",
    "bedroom",
    "bathroom",
    "guest_bathroom",
    "kristines_room",
    "claudias_room",
    "rooftop",
)
RAW_TO_BASE = {f"media_player.{room}_2": f"media_player.{room}" for room in ROOMS}

MIKKEL = "aaaa0000000000000000000000000001"
KRISTINE = "aaaa0000000000000000000000000002"
CLAUDIA = "aaaa0000000000000000000000000003"
APPDAEMON = "aaaa00000000000000000000000000ad"
ROOFTOP_TABLET = "aaaa0000000000000000000000000004"

SPOTIFY = "x-sonos-vli:RINCON_74CA6064C9BF01400:2,spotify:6eff5f32d1a6e15f7758545aec6d0e0c"
MA_STREAM = "http://10.21.0.5:8097/single/cGCeFG6t/RINCON_F0F6C1A525A201400.flac"
ACCOUNTS = {
    "Mikkel": "media_player.spotify_mikkel",
    "Kristine": "media_player.spotify_kristine",
    "Claudia": "media_player.spotify_claudia",
}


def raw(room):
    return f"media_player.{room}_2"


def make_app(spotify_accounts=None):
    """MusicAttribution with a fake AppDaemon around it, without running initialize()."""
    app = ma.MusicAttribution.__new__(ma.MusicAttribution)
    app.raw_to_base = dict(RAW_TO_BASE)
    app.base_to_raw = {base: r for r, base in RAW_TO_BASE.items()}
    app.residents = ["Mikkel", "Kristine", "Claudia"]
    app.house_user_ids = {APPDAEMON}
    app.spotify_accounts = dict(spotify_accounts or {})
    app.settle_s = 5.0
    app.spotify_wait_s = 60.0
    app.spotify_poll_s = 5.0
    app.ledger = ma.PlaybackLedger(idle_s=1200, call_window_s=30)
    app._person_by_user_id = {}

    app.clock = 100000.0
    app.states = {}
    app.timers = []
    app.events = []

    def get_state(entity=None, attribute=None, **kw):
        if entity == "person":
            return {e: s for e, s in app.states.items() if e.startswith("person.")}
        state = app.states.get(entity)
        if state is None:
            return None
        if attribute == "all":
            return state
        if attribute:
            return state["attributes"].get(attribute)
        return state["state"]

    def run_in(callback, delay, **kwargs):
        app.timers.append((app.clock + delay, callback, kwargs))

    app.get_state = get_state
    app.get_now = lambda: datetime.fromtimestamp(app.clock, tz=timezone.utc)
    app.run_in = run_in
    app.fire_event = lambda name, **kw: app.events.append((name, kw))
    app.log = lambda *a, **k: None

    app.states["person.mikkel"] = {"state": "home", "attributes": {"friendly_name": "Mikkel", "user_id": MIKKEL}}
    app.states["person.kristine"] = {"state": "home", "attributes": {"friendly_name": "Kristine", "user_id": KRISTINE}}
    app.states["person.claudia"] = {"state": "home", "attributes": {"friendly_name": "Claudia", "user_id": CLAUDIA}}
    app.states["person.appdaemon"] = {"state": "unknown", "attributes": {"friendly_name": "AppDaemon", "user_id": APPDAEMON}}
    app.states["person.rooftop"] = {"state": "home", "attributes": {"friendly_name": "Rooftop", "user_id": ROOFTOP_TABLET}}
    for r in RAW_TO_BASE:
        app.states[r] = {"state": "idle", "attributes": {"group_members": [r]}}
    return app


def advance(app, seconds):
    """Move the fake clock, running every timer that comes due on the way."""
    end = app.clock + seconds
    while True:
        due = sorted((t for t in app.timers if t[0] <= end), key=lambda t: t[0])
        if not due:
            break
        when, callback, kwargs = due[0]
        app.timers.remove(due[0])
        app.clock = max(app.clock, when)
        callback(kwargs)
    app.clock = end


def set_speaker(app, room, state, content=None, group=None, source=None, title=None, artist=None):
    entity = raw(room)
    old = app.states[entity]["state"]
    attrs = {"group_members": [raw(g) for g in (group or [room])]}
    if content:
        attrs["media_content_id"] = content
    if source:
        attrs["source"] = source
    if title:
        attrs["media_title"] = title
        attrs["media_artist"] = artist or ""
    app.states[entity] = {"state": state, "attributes": attrs}
    app._on_speaker(entity, "state", old, state, {})


def call(app, service, user_id=None, parent_id=None, domain="media_player", **service_data):
    app._on_call(
        "call_service",
        {
            "domain": domain,
            "service": service,
            "service_data": service_data,
            "metadata": {"context": {"id": "x", "user_id": user_id, "parent_id": parent_id}},
        },
        {},
    )


def rows(app):
    return [kw for name, kw in app.events if name == "house_events_report"]


def spotify_account(app, person, state, source=None, title=None, artist=None):
    attrs = {}
    if source:
        attrs["source"] = source
    if title:
        attrs["media_title"] = title
        attrs["media_artist"] = artist or ""
    app.states[ACCOUNTS[person]] = {"state": state, "attributes": attrs}


class UserResolution(unittest.TestCase):
    def test_person_entities_name_the_residents(self):
        app = make_app()
        self.assertEqual(app._resident_for(KRISTINE), "Kristine")
        self.assertEqual(app._resident_for(CLAUDIA), "Claudia")

    def test_non_resident_persons_are_not_named(self):
        app = make_app()
        self.assertIsNone(app._resident_for(APPDAEMON))
        self.assertIsNone(app._resident_for(ROOFTOP_TABLET))
        self.assertIsNone(app._resident_for("deadbeef"))

    def test_person_linked_after_start_is_found(self):
        app = make_app()
        self.assertIsNone(app._resident_for("abc123"))
        app.states["person.guest"] = {"state": "home", "attributes": {"friendly_name": "Mikkel", "user_id": "ABC-123"}}
        self.assertEqual(app._resident_for("abc123"), "Mikkel")

    def test_call_origin(self):
        names = {MIKKEL: "Mikkel"}.get
        self.assertEqual(ma.call_origin(MIKKEL, None, {APPDAEMON}, names), ("person", "Mikkel"))
        self.assertEqual(ma.call_origin(ROOFTOP_TABLET, None, {APPDAEMON}, names), ma.SOMEONE)
        self.assertEqual(ma.call_origin(None, None, {APPDAEMON}, names), ma.SOMEONE)

    def test_dashed_user_id_matches(self):
        self.assertEqual(ma.normalize_user_id("AAAA0000-0000-0000-0000-0000000000AD"), APPDAEMON)


class HouseExclusion(unittest.TestCase):
    def test_appdaemon_account_is_the_house(self):
        self.assertEqual(ma.call_origin(APPDAEMON, None, {APPDAEMON}, lambda uid: None), ma.HOUSE)

    def test_automation_context_is_the_house(self):
        self.assertEqual(ma.call_origin(None, "01J9PARENT", {APPDAEMON}, lambda uid: None), ma.HOUSE)

    def test_house_start_gets_no_row(self):
        app = make_app()
        call(app, "play_media", user_id=APPDAEMON, entity_id=["media_player.bedroom"], media_content_id="x")
        advance(app, 4)
        set_speaker(app, "bedroom", "playing", MA_STREAM)
        advance(app, 10)
        self.assertEqual(rows(app), [])

    def test_automation_start_gets_no_row(self):
        app = make_app()
        call(app, "media_play", parent_id="01J9PARENT", entity_id=["media_player.kitchen"])
        advance(app, 2)
        set_speaker(app, "kitchen", "playing", MA_STREAM)
        advance(app, 10)
        self.assertEqual(rows(app), [])

    def test_announcements_and_joins_are_not_starts(self):
        self.assertIsNone(ma.start_call_targets("media_player", "play_media", {"entity_id": ["a"], "announce": True}))
        self.assertIsNone(ma.start_call_targets("media_player", "join", {"entity_id": ["a"], "group_members": ["b"]}))
        self.assertIsNone(ma.start_call_targets("music_assistant", "play_announcement", {"entity_id": ["a"]}))
        self.assertIsNone(ma.start_call_targets("chime_tts", "say", {"entity_id": ["a"]}))
        self.assertEqual(ma.start_call_targets("media_player", "media_play", {"entity_id": "a, b"}), ["a", "b"])
        self.assertEqual(ma.start_call_targets("music_assistant", "play_media", {"entity_id": ["a"]}), ["a"])

    def test_house_join_does_not_steal_a_resident_start(self):
        app = make_app()
        call(app, "play_media", user_id=CLAUDIA, entity_id=["media_player.kitchen"], media_content_id="x")
        advance(app, 2)
        call(app, "join", user_id=APPDAEMON, entity_id=["media_player.kitchen"], group_members=["media_player.dining_room"])
        advance(app, 2)
        set_speaker(app, "kitchen", "playing", MA_STREAM, group=["kitchen", "dining_room"])
        set_speaker(app, "dining_room", "playing", MA_STREAM, group=["kitchen", "dining_room"])
        advance(app, 10)
        self.assertEqual([r["text"] for r in rows(app)], ["Claudia started music in Kitchen + 1 more"])


class HaStarts(unittest.TestCase):
    def test_dashboard_play_names_the_resident(self):
        app = make_app()
        call(app, "play_media", user_id=KRISTINE, entity_id=["media_player.kitchen"], media_content_id="x")
        advance(app, 4)
        set_speaker(app, "kitchen", "playing", MA_STREAM)
        advance(app, 10)
        self.assertEqual(rows(app), [{"text": "Kristine started music in Kitchen", "icon": "mdi:music", "by": "Kristine"}])

    def test_raw_entity_target_counts(self):
        app = make_app()
        call(app, "media_play", user_id=MIKKEL, entity_id=["media_player.bedroom_2"])
        advance(app, 4)
        set_speaker(app, "bedroom", "playing", MA_STREAM)
        advance(app, 10)
        self.assertEqual(rows(app)[0]["text"], "Mikkel started music in Bedroom")

    def test_group_entity_target_is_expanded(self):
        app = make_app()
        app.states["media_player.sonos"] = {
            "state": "idle",
            "attributes": {"entity_id": ["media_player.kitchen", "media_player.dining_room"]},
        }
        call(app, "media_play", user_id=CLAUDIA, entity_id=["media_player.sonos"])
        advance(app, 4)
        set_speaker(app, "dining_room", "playing", MA_STREAM)
        advance(app, 10)
        self.assertEqual(rows(app)[0]["by"], "Claudia")

    def test_other_account_is_someone(self):
        app = make_app()
        call(app, "media_play", user_id=ROOFTOP_TABLET, entity_id=["media_player.rooftop"])
        advance(app, 3)
        set_speaker(app, "rooftop", "playing", MA_STREAM)
        advance(app, 10)
        self.assertEqual(rows(app), [{"text": "Someone started music in Rooftop", "icon": "mdi:music"}])

    def test_call_outside_the_window_is_ignored(self):
        app = make_app()
        call(app, "media_play", user_id=MIKKEL, entity_id=["media_player.kitchen"])
        advance(app, 45)
        set_speaker(app, "kitchen", "playing", MA_STREAM)
        advance(app, 10)
        self.assertEqual(rows(app)[0]["text"], "Someone started music in Kitchen")

    def test_call_on_another_speaker_is_ignored(self):
        app = make_app()
        call(app, "media_play", user_id=MIKKEL, entity_id=["media_player.bedroom"])
        advance(app, 2)
        set_speaker(app, "kitchen", "playing", MA_STREAM)
        advance(app, 10)
        self.assertNotIn("by", rows(app)[0])

    def test_tv_audio_gets_no_row(self):
        app = make_app()
        set_speaker(app, "living_room", "playing", "x-sonos-htastream:RINCON_74CA6064C9BF01400:spdif", source="TV")
        advance(app, 10)
        self.assertEqual(rows(app), [])

    def test_airplay_is_someone(self):
        app = make_app()
        set_speaker(app, "bathroom", "playing", "x-sonos-vli:RINCON_F0F6C1A5264A01400:1,airplay:164e173d3f26")
        advance(app, 10)
        self.assertEqual(
            rows(app), [{"text": "Someone started AirPlay in Bathroom", "icon": "mdi:airplay", "audience": "admin"}]
        )


class Sessions(unittest.TestCase):
    def test_joining_a_playing_group_is_not_a_session(self):
        app = make_app()
        set_speaker(app, "living_room", "playing", SPOTIFY)
        advance(app, 30)
        self.assertEqual(len(rows(app)), 1)
        set_speaker(app, "kitchen", "playing", SPOTIFY, group=["kitchen"])
        app.states[raw("kitchen")]["attributes"]["group_members"] = [raw("living_room"), raw("kitchen")]
        advance(app, 10)
        self.assertEqual(len(rows(app)), 1)

    def test_group_starting_together_is_one_session(self):
        app = make_app()
        group = ["living_room", "kitchen", "dining_room"]
        set_speaker(app, "kitchen", "playing", SPOTIFY, group=group)
        app.clock += 0.05
        set_speaker(app, "living_room", "playing", SPOTIFY, group=group)
        set_speaker(app, "dining_room", "playing", SPOTIFY, group=group)
        advance(app, 10)
        self.assertEqual([r["text"] for r in rows(app)], ["Someone started Spotify in Living room + 2 more"])

    def test_resume_inside_idle_window_is_same_session(self):
        app = make_app()
        set_speaker(app, "kitchen", "playing", SPOTIFY)
        advance(app, 600)
        set_speaker(app, "kitchen", "paused", SPOTIFY)
        advance(app, 19 * 60)
        set_speaker(app, "kitchen", "playing", SPOTIFY)
        advance(app, 10)
        self.assertEqual(len(rows(app)), 1)

    def test_play_after_idle_window_is_new_session(self):
        app = make_app()
        set_speaker(app, "kitchen", "playing", SPOTIFY)
        advance(app, 600)
        set_speaker(app, "kitchen", "paused", SPOTIFY)
        advance(app, 21 * 60)
        set_speaker(app, "kitchen", "playing", SPOTIFY)
        advance(app, 10)
        self.assertEqual(len(rows(app)), 2)

    def test_coordinator_playing_recently_continues_the_session(self):
        app = make_app()
        set_speaker(app, "living_room", "playing", SPOTIFY, group=["living_room", "kitchen"])
        advance(app, 600)
        set_speaker(app, "living_room", "paused", SPOTIFY, group=["living_room", "kitchen"])
        advance(app, 300)
        set_speaker(app, "kitchen", "playing", SPOTIFY, group=["living_room", "kitchen"])
        advance(app, 10)
        self.assertEqual(len(rows(app)), 1)

    def test_blip_before_settle_does_not_count(self):
        app = make_app()
        set_speaker(app, "living_room", "playing", SPOTIFY)
        advance(app, 0.3)
        set_speaker(app, "living_room", "paused", SPOTIFY)
        advance(app, 12)
        self.assertEqual(rows(app), [])
        set_speaker(app, "living_room", "playing", SPOTIFY)
        advance(app, 10)
        self.assertEqual(len(rows(app)), 1)

    def test_back_from_unavailable_is_not_a_start(self):
        app = make_app()
        set_speaker(app, "kitchen", "unavailable")
        set_speaker(app, "kitchen", "playing", SPOTIFY)
        advance(app, 10)
        self.assertEqual(rows(app), [])

    def test_seed_counts_recent_playback(self):
        ledger = ma.PlaybackLedger(idle_s=1200)
        ledger.seed(raw("kitchen"), "paused", 1000.0)
        self.assertFalse(ledger.transition(raw("kitchen"), "paused", "paused", 1100.0))
        self.assertTrue(ledger.transition(raw("kitchen"), "paused", "playing", 1500.0))
        self.assertEqual(ledger.settle(raw("kitchen"), 1500.0, [raw("kitchen")]), "resume")

    def test_seed_of_playing_speaker_makes_joiners_join(self):
        ledger = ma.PlaybackLedger(idle_s=1200)
        ledger.seed(raw("living_room"), "playing", 1000.0)
        ledger.transition(raw("kitchen"), "idle", "playing", 9000.0)
        self.assertEqual(ledger.settle(raw("kitchen"), 9000.0, [raw("living_room"), raw("kitchen")]), "join")

    def test_stale_run(self):
        ledger = ma.PlaybackLedger(idle_s=1200)
        ledger.transition(raw("kitchen"), "idle", "playing", 10.0)
        ledger.transition(raw("kitchen"), "playing", "idle", 11.0)
        self.assertEqual(ledger.settle(raw("kitchen"), 10.0, [raw("kitchen")]), "stale")


class Spotify(unittest.TestCase):
    def start(self, app, room="kitchen", group=None, title="Song", artist="Band"):
        set_speaker(app, room, "playing", SPOTIFY, group=group, source="Spotify Connect", title=title, artist=artist)

    def test_match_names_the_account_owner(self):
        app = make_app(ACCOUNTS)
        for person in ACCOUNTS:
            spotify_account(app, person, "idle")
        self.start(app, group=["kitchen", "dining_room", "rooftop"])
        advance(app, 20)
        self.assertEqual(rows(app), [])
        spotify_account(app, "Kristine", "playing", source="Kitchen + 2")
        advance(app, 10)
        self.assertEqual(
            rows(app), [{"text": "Kristine started Spotify in Kitchen + 2 more", "icon": "mdi:spotify", "by": "Kristine"}]
        )

    def test_row_names_the_group_as_it_stands_at_the_match(self):
        app = make_app(ACCOUNTS)
        for person in ACCOUNTS:
            spotify_account(app, person, "idle")
        self.start(app, room="living_room")
        advance(app, 8)
        group = ["living_room", "kitchen", "dining_room"]
        for room in group:
            app.states[raw(room)]["attributes"]["group_members"] = [raw(g) for g in group]
        spotify_account(app, "Mikkel", "playing", source="Living Room + 2")
        advance(app, 10)
        self.assertEqual(rows(app)[0]["text"], "Mikkel started Spotify in Living room + 2 more")

    def test_match_is_tolerant_of_case_and_punctuation(self):
        keys = {ma.device_key("Claudias room")}
        states = {ACCOUNTS["Claudia"]: {"state": "playing", "attributes": {"source": "Claudia's Room"}}}
        self.assertEqual(ma.spotify_matches(ACCOUNTS, states, keys), ["Claudia"])

    def test_other_device_does_not_match(self):
        states = {ACCOUNTS["Mikkel"]: {"state": "playing", "attributes": {"source": "Mikkels iPhone"}}}
        self.assertEqual(ma.spotify_matches(ACCOUNTS, states, {"kitchen"}), [])

    def test_guest_when_every_resident_is_linked_and_none_matched(self):
        app = make_app(ACCOUNTS)
        for person in ACCOUNTS:
            spotify_account(app, person, "idle")
        spotify_account(app, "Mikkel", "playing", source="Mikkels iPhone", title="Other", artist="Else")
        self.start(app)
        advance(app, 70)
        self.assertEqual(rows(app), [{"text": "A guest started Spotify in Kitchen", "icon": "mdi:spotify"}])

    def test_not_all_accounts_linked_is_someone(self):
        accounts = {k: v for k, v in ACCOUNTS.items() if k != "Claudia"}
        app = make_app(accounts)
        for person in accounts:
            spotify_account(app, person, "idle")
        self.start(app)
        advance(app, 70)
        self.assertEqual(rows(app)[0]["text"], "Someone started Spotify in Kitchen")

    def test_unavailable_account_is_someone(self):
        app = make_app(ACCOUNTS)
        for person in ACCOUNTS:
            spotify_account(app, person, "idle")
        spotify_account(app, "Claudia", "unavailable")
        self.start(app)
        advance(app, 70)
        self.assertEqual(rows(app)[0]["text"], "Someone started Spotify in Kitchen")

    def test_resident_on_the_same_track_is_not_called_a_guest(self):
        app = make_app(ACCOUNTS)
        for person in ACCOUNTS:
            spotify_account(app, person, "idle")
        spotify_account(app, "Kristine", "playing", source="Unknown speaker", title="Song", artist="Band")
        self.start(app)
        advance(app, 70)
        self.assertEqual(rows(app)[0]["text"], "Someone started Spotify in Kitchen")

    def test_two_matching_accounts_is_someone(self):
        app = make_app(ACCOUNTS)
        for person in ACCOUNTS:
            spotify_account(app, person, "idle")
        spotify_account(app, "Mikkel", "playing", source="Kitchen")
        spotify_account(app, "Kristine", "playing", source="Kitchen")
        self.start(app)
        advance(app, 70)
        self.assertEqual(rows(app), [{"text": "Someone started Spotify in Kitchen", "icon": "mdi:spotify"}])

    def test_spotify_stopped_before_the_deadline_is_someone(self):
        app = make_app(ACCOUNTS)
        for person in ACCOUNTS:
            spotify_account(app, person, "idle")
        self.start(app)
        advance(app, 20)
        set_speaker(app, "kitchen", "paused", SPOTIFY)
        advance(app, 50)
        self.assertEqual(rows(app)[0]["text"], "Someone started Spotify in Kitchen")

    def test_no_accounts_reports_someone_at_once(self):
        app = make_app()
        self.start(app)
        advance(app, 6)
        self.assertEqual(rows(app)[0]["text"], "Someone started Spotify in Kitchen")

    def test_guest_rule(self):
        idle = {"state": "idle", "attributes": {}}
        states = {entity: idle for entity in ACCOUNTS.values()}
        residents = list(ACCOUNTS)
        self.assertTrue(ma.spotify_is_guest(residents, ACCOUNTS, states, True, ("song", "band")))
        self.assertFalse(ma.spotify_is_guest(residents, ACCOUNTS, states, False, ("song", "band")))
        self.assertFalse(ma.spotify_is_guest(residents, {}, states, True, None))
        self.assertFalse(ma.spotify_is_guest([], ACCOUNTS, states, True, None))


class GroupNaming(unittest.TestCase):
    def test_where_label(self):
        self.assertEqual(ma.where_label(["Kitchen"]), "Kitchen")
        self.assertEqual(ma.where_label(["Kitchen", "Dining room", "Living room", "Rooftop"]), "Kitchen + 3 more")

    def test_room_label(self):
        self.assertEqual(ma.room_label("media_player.claudias_room"), "Claudias room")
        self.assertEqual(ma.room_label("media_player.living_room"), "Living room")

    def test_device_key_strips_group_count(self):
        self.assertEqual(ma.device_key("Kitchen + 3"), "kitchen")
        self.assertEqual(ma.device_key("Living Room+2"), "livingroom")
        self.assertEqual(ma.device_key("Claudia's Room"), "claudiasroom")
        self.assertEqual(ma.device_key(None), "")

    def test_session_text_is_ascii(self):
        self.assertEqual(
            ma.session_text("Kristine", "spotify", "Kitchen + 3 more"), "Kristine started Spotify in Kitchen + 3 more"
        )
        self.assertEqual(ma.session_text("Someone", "other", "Køkken"), "Someone started music in Kkken")

    def test_row_names_the_group_grown_by_then(self):
        app = make_app()
        call(app, "play_media", user_id=MIKKEL, entity_id=["media_player.living_room"], media_content_id="x")
        set_speaker(app, "living_room", "playing", MA_STREAM, group=["living_room"])
        advance(app, 2)
        group = ["living_room", "kitchen", "dining_room", "rooftop"]
        for room in group:
            app.states[raw(room)]["attributes"]["group_members"] = [raw(g) for g in group]
        advance(app, 10)
        self.assertEqual(rows(app)[0]["text"], "Mikkel started music in Living room + 3 more")

    def test_content_kind(self):
        self.assertEqual(ma.content_kind(SPOTIFY), "spotify")
        self.assertEqual(ma.content_kind("x-sonos-vli:RINCON_X:1,airplay:abc"), "airplay")
        self.assertEqual(ma.content_kind("x-sonos-htastream:RINCON_X:spdif"), "tv")
        self.assertEqual(ma.content_kind("x-rincon-stream:RINCON_804AF2CB391401400"), "line_in")
        self.assertEqual(ma.content_kind(MA_STREAM), "other")
        self.assertEqual(ma.content_kind(None, "Spotify Connect"), "spotify")
        self.assertIsNone(ma.content_kind(None))


class Audience(unittest.TestCase):
    def test_private_rooms(self):
        self.assertEqual(ma.audience_for(["kristines_room"]), {"audience_users": ["Kristine"]})
        self.assertEqual(ma.audience_for(["claudias_room"]), {"audience_users": ["Claudia"]})
        self.assertEqual(ma.audience_for(["bedroom"]), {"audience": "admin"})
        self.assertEqual(ma.audience_for(["bedroom", "bathroom"]), {"audience": "admin"})

    def test_mixed_private_rooms_union_their_occupants(self):
        self.assertEqual(ma.audience_for(["bedroom", "claudias_room"]), {"audience_users": ["Claudia"]})
        self.assertEqual(
            ma.audience_for(["claudias_room", "kristines_room"]), {"audience_users": ["Claudia", "Kristine"]}
        )

    def test_any_shared_room_is_public(self):
        self.assertEqual(ma.audience_for(["kitchen"]), {})
        self.assertEqual(ma.audience_for(["bedroom", "kitchen"]), {})
        self.assertEqual(ma.audience_for(["guest_bathroom"]), {})

    def test_row_carries_the_audience(self):
        app = make_app()
        call(app, "media_play", user_id=KRISTINE, entity_id=["media_player.kristines_room"])
        advance(app, 3)
        set_speaker(app, "kristines_room", "playing", MA_STREAM)
        advance(app, 10)
        self.assertEqual(rows(app)[0]["audience_users"], ["Kristine"])


if __name__ == "__main__":
    unittest.main()
