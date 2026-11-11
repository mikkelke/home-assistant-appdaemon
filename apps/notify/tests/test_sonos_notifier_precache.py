# Unit tests for SonosNotifier's choice of voice and its pre-rendered announcements, and for the
# precache_messages list: it has to hold every sentence an app hands to notify().
# Run from repo root: python3 -m unittest discover -s apps/notify/tests -q

import ast
import sys
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import yaml

from test_sonos_notifier import (
    AUDIO_CONVERSION, DAYTIME, EXPLICIT, _logged, _logged_at, _new_app, _plays, _renders, _say_url_response,
)

APPS = Path(__file__).resolve().parents[2]
for app_dir in ("sonos", "intercom", "weather"):
    sys.path.insert(0, str(APPS / app_dir))

import assist_tts_failover as atf  # noqa: E402
import intercom  # noqa: E402
import weather_opening_alert  # noqa: E402


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("!secret", lambda loader, node: "secret:" + loader.construct_scalar(node))


def config(relative, app):
    """The settings of one app in a yaml file of apps/."""
    return yaml.load((APPS / relative).read_text(encoding="utf-8"), Loader=Loader)[app]


NOTIFIER = config("notify/sonos_notifier.yaml", "SonosNotifier")
FAILOVER = config("sonos/assist_tts_failover.yaml", "AssistTtsFailover")
PRIMARY = {"tts_platform": "acer_tts", "options": {"voice": "af_heart"}, "language": "en"}
FALLBACK = {"tts_platform": "piper", "options": {"voice": "en_US-hfc_female-medium"}, "language": "en_US"}


def failover(healthy=None):
    """The real AssistTtsFailover as the shipped config sets it up, with the given verdict."""
    app = atf.AssistTtsFailover.__new__(atf.AssistTtsFailover)
    app.args = FAILOVER
    app.verdict = SimpleNamespace(healthy=healthy)
    return app


def with_failover(app, failover_app):
    app.get_app = lambda name: failover_app if name == "AssistTtsFailover" else None
    return app


def announce(app, message="Hello", **kwargs):
    app.notify(message, target_speakers=[EXPLICIT], **kwargs)
    return _renders(app)[-1]


def legacy(message="Hello"):
    """The render of a notifier that has no AssistTtsFailover to ask."""
    return {
        "message": message,
        "chime_path": "/config/www/chimes/alert.mp3",
        "cache": True,
        "offset": -300,
        "tts_platform": "piper",
        "audio_conversion": AUDIO_CONVERSION,
        "return_response": True,
    }


def voiced(voice, message="Hello"):
    return {**legacy(message), **voice}


class VoiceSelection(unittest.TestCase):
    def test_a_healthy_primary_voices_the_announcement(self):
        app = with_failover(_new_app(now=DAYTIME), failover(True))
        self.assertEqual(announce(app), voiced(PRIMARY))

    def test_unknown_health_counts_as_the_primary(self):
        app = with_failover(_new_app(now=DAYTIME), failover(None))
        self.assertEqual(announce(app), voiced(PRIMARY))

    def test_a_down_primary_hands_the_announcement_to_the_fallback_voice(self):
        app = with_failover(_new_app(now=DAYTIME), failover(False))
        self.assertEqual(announce(app), voiced(FALLBACK))
        self.assertEqual(len(_plays(app)), 1)

    def test_the_voice_is_named_in_the_log(self):
        app = with_failover(_new_app(now=DAYTIME), failover(False))
        announce(app)
        self.assertTrue(_logged(app, "tts_platform='piper'"))

    def test_without_the_failover_app_the_configured_platform_is_sent_as_before(self):
        app = _new_app(now=DAYTIME)
        render = announce(app)
        self.assertEqual(render, legacy())
        self.assertNotIn("options", render)
        self.assertNotIn("language", render)
        self.assertFalse(_logged_at(app, "AssistTtsFailover", "WARNING"))

    def test_an_app_that_cannot_be_fetched_falls_back_to_the_configured_platform(self):
        app = _new_app(now=DAYTIME)

        def get_app(name):
            raise RuntimeError("no such app")

        app.get_app = get_app
        self.assertEqual(announce(app), legacy())
        self.assertEqual(len(_plays(app)), 1)
        self.assertTrue(_logged_at(app, "no such app", "WARNING"))

    def test_an_app_whose_voice_cannot_be_read_falls_back_to_the_configured_platform(self):
        unready = failover(True)
        del unready.verdict
        app = with_failover(_new_app(now=DAYTIME), unready)
        self.assertEqual(announce(app), legacy())
        self.assertEqual(len(_plays(app)), 1)
        self.assertTrue(_logged_at(app, "AssistTtsFailover.announcement_voice() failed", "WARNING"))

    def test_an_app_with_no_voice_falls_back_to_the_configured_platform(self):
        for answer in (None, {}, "piper", ["piper"]):
            with self.subTest(answer=answer):
                app = with_failover(_new_app(now=DAYTIME), SimpleNamespace(announcement_voice=lambda: answer))
                self.assertEqual(announce(app), legacy())

    def test_no_configured_platform_and_no_failover_sends_none(self):
        app = _new_app(now=DAYTIME)
        app.tts_platform = None
        self.assertNotIn("tts_platform", announce(app))

    def test_a_tts_platform_from_the_caller_wins_and_the_failover_is_not_asked(self):
        asked = []
        app = _new_app(now=DAYTIME)
        app.get_app = lambda name: asked.append(name)
        render = announce(app, tts_platform="other")
        self.assertEqual(render, {**legacy(), "tts_platform": "other"})
        self.assertEqual(asked, [])

    def test_the_failover_is_asked_for_every_render_so_reload_order_does_not_matter(self):
        app = _new_app(now=DAYTIME)
        self.assertEqual(announce(app, "First"), legacy("First"))
        with_failover(app, failover(True))
        self.assertEqual(announce(app, "Second"), voiced(PRIMARY, "Second"))
        with_failover(app, failover(False))
        self.assertEqual(announce(app, "Third"), voiced(FALLBACK, "Third"))
        app.get_app = lambda name: None
        self.assertEqual(announce(app, "Fourth"), legacy("Fourth"))


def drain(app):
    """Run the scheduled steps in order, as the scheduler would; the delays they were scheduled with."""
    delays = []
    while app.run_in_calls:
        callback, delay = app.run_in_calls.pop(0)
        delays.append(delay)
        callback({})
    return delays


def precaching(messages=("Alpha", "Bravo"), failover_app=None):
    """An app in quiet hours with messages to pre-render."""
    app = _new_app()
    app.precache_messages = list(messages)
    if failover_app is not None:
        with_failover(app, failover_app)
    return app


def summaries(app):
    return [str(args[0]) for args, kwargs in app.log_calls if "Pre-rendered announcements" in str(args)]


class Precache(unittest.TestCase):
    def test_every_message_is_rendered_with_each_voice_exactly_as_notify_renders_it(self):
        expected = []
        for healthy in (True, False):
            notified = with_failover(_new_app(now=DAYTIME), failover(healthy))
            expected += [announce(notified, message) for message in ("Alpha", "Bravo")]
        app = precaching(failover_app=failover(None))
        app.precache()
        drain(app)
        self.assertEqual(_renders(app), expected)
        self.assertEqual([render["tts_platform"] for render in _renders(app)],
                         ["acer_tts", "acer_tts", "piper", "piper"])

    def test_nothing_is_played_and_quiet_hours_do_not_matter(self):
        app = precaching(failover_app=failover(None))
        app.precache()
        drain(app)
        self.assertEqual(len(_renders(app)), 4)
        self.assertEqual(_plays(app), [])
        self.assertEqual([service for service, _ in app.calls], ["chime_tts/say_url"] * 4)

    def test_calling_it_renders_nothing_itself(self):
        app = precaching()
        app.precache()
        self.assertEqual(app.calls, [])
        self.assertEqual(len(app.run_in_calls), 1)

    def test_one_render_at_a_time_precache_gap_s_apart(self):
        app = precaching(failover_app=failover(None))
        app.precache_gap_s = 1.5
        app.precache()
        self.assertEqual(drain(app), [0, 1.5, 1.5, 1.5])
        self.assertEqual(len(_renders(app)), 4)

    def test_a_run_ends_with_one_summary_line(self):
        app = precaching(failover_app=failover(None))
        app.precache()
        drain(app)
        self.assertEqual(summaries(app), ["Pre-rendered announcements: 4 rendered, 0 failed"])
        self.assertTrue(_logged_at(app, "Pre-rendered announcements", "INFO"))

    def test_failed_renders_are_counted_and_the_run_goes_on(self):
        app = precaching(failover_app=failover(None))
        responses = iter([_say_url_response(), _say_url_response(url=None, success=False),
                          RuntimeError("unreachable"), _say_url_response()])
        original = app.call_service

        def call_service(service, **kwargs):
            app.say_url_response = next(responses)
            return original(service, **kwargs)

        app.call_service = call_service
        app.precache()
        drain(app)
        self.assertEqual(summaries(app), ["Pre-rendered announcements: 2 rendered, 2 failed"])
        self.assertTrue(_logged_at(app, "Pre-rendered announcements", "WARNING"))

    def test_a_render_that_raises_is_counted_and_logged_and_the_run_goes_on(self):
        app = precaching()

        def broken(url):
            raise ValueError("bad url")

        app._clip_url = broken
        app.precache()
        drain(app)
        self.assertEqual(summaries(app), ["Pre-rendered announcements: 0 rendered, 2 failed"])
        self.assertTrue(_logged_at(app, "Pre-rendering 'Alpha' failed: bad url", "ERROR"))

    def test_only_the_fallback_is_rendered_while_the_primary_is_down(self):
        app = precaching(failover_app=failover(False))
        app.precache()
        drain(app)
        self.assertEqual(_renders(app), [voiced(FALLBACK, "Alpha"), voiced(FALLBACK, "Bravo")])

    def test_without_the_failover_app_the_configured_platform_is_rendered_alone(self):
        app = precaching()
        app.precache()
        drain(app)
        self.assertEqual(_renders(app), [legacy("Alpha"), legacy("Bravo")])

    def test_a_failover_app_that_fails_leaves_the_configured_platform(self):
        def broken():
            raise RuntimeError("boom")

        app = precaching()
        app.get_app = lambda name: SimpleNamespace(precache_voices=broken)
        app.precache()
        drain(app)
        self.assertEqual(_renders(app), [legacy("Alpha"), legacy("Bravo")])
        self.assertTrue(_logged_at(app, "precache_voices() failed", "WARNING"))

    def test_a_call_during_a_run_starts_the_list_over_without_a_second_chain(self):
        app = precaching(failover_app=failover(None))
        app.precache()
        callback, delay = app.run_in_calls.pop(0)
        callback({})
        self.assertEqual(len(_renders(app)), 1)
        self.assertEqual([delay for _, delay in app.run_in_calls], [2.0])
        app.precache()
        self.assertEqual([delay for _, delay in app.run_in_calls], [2.0])
        drain(app)
        self.assertEqual(len(_renders(app)), 5)
        self.assertEqual(summaries(app), ["Pre-rendered announcements: 4 rendered, 0 failed"])

    def test_a_finished_run_can_be_followed_by_another(self):
        app = precaching()
        for _ in range(2):
            app.precache()
            drain(app)
        self.assertEqual(len(_renders(app)), 4)
        self.assertEqual(len(summaries(app)), 2)

    def test_no_messages_means_no_run(self):
        app = precaching(messages=())
        app.precache()
        self.assertEqual((app.run_in_calls, app.calls), ([], []))

    def test_the_scheduled_callback_starts_a_run(self):
        app = precaching()
        app._start_precache({})
        self.assertEqual([(callback, delay) for callback, delay in app.run_in_calls],
                         [(app._precache_step, 0)])


class Initialize(unittest.TestCase):
    def initialized(self, **changes):
        app = _new_app(now=DAYTIME)
        app.args = {**NOTIFIER, **changes}
        app.initialize()
        return app

    def test_the_first_run_is_scheduled_a_minute_after_startup(self):
        app = self.initialized()
        self.assertEqual(app.run_in_calls, [(app._start_precache, 60.0)])
        self.assertEqual(app.precache_messages, NOTIFIER["precache_messages"])
        self.assertEqual(app.precache_gap_s, 2.0)

    def test_the_delay_and_the_gap_come_from_the_config(self):
        app = self.initialized(precache_delay_s=5, precache_gap_s=0.5)
        self.assertEqual(app.run_in_calls, [(app._start_precache, 5.0)])
        app.run_in_calls.clear()
        app.precache()
        self.assertEqual(drain(app)[:3], [0, 0.5, 0.5])

    def test_no_messages_schedule_nothing(self):
        for value in (None, []):
            with self.subTest(value=value):
                self.assertEqual(self.initialized(precache_messages=value).run_in_calls, [])

    def test_blanks_and_repeats_are_dropped_and_the_order_is_kept(self):
        app = self.initialized(precache_messages=["B", "A", "", None, "B"])
        self.assertEqual(app.precache_messages, ["B", "A"])

    def test_a_setup_that_stops_early_schedules_nothing_but_can_still_be_asked_to_render(self):
        args = {key: value for key, value in NOTIFIER.items() if key != "tts_group_all"}
        app = _new_app(now=DAYTIME)
        app.args = args
        app.initialize()
        self.assertEqual(app.run_in_calls, [])
        app.precache()
        self.assertEqual(len(app.run_in_calls), 1)


Site = namedtuple("Site", "path key leaves literals dynamic")

REVIEWED = {
    ("appliances/dryer_monitor.py", "msg"): "announce_message of dryer.yaml, else the literal the scan finds",
    ("appliances/washer_monitor.py", "self._finish_announcement(programme)"): "announce_message or drum clean",
    ("appliances/washer_monitor.py", "self._finish_announcement(confirmed_prog)"): "announce_message or drum clean",
    ("intercom/intercom.py", "courier_words"): "courier_message() and COURIER_AT_THE_DOOR",
    ("intercom/intercom.py", "combined_message"): "a door's ring and its opening",
    ("intercom/intercom.py", "info['message']"): "a door's ring",
    ("intercom/intercom.py", "followup"): "None at every call site; the opening is spoken inside the ring's sentence",
    ("tv/living_room_tv_control.py", "f'TV moving to {announce_phrase}'"): "one phrase per lift button",
    ("weather/weather_opening_alert.py", "rooftop_tts_message(names)"): "one per terrace door, and both",
}


def message_argument(call):
    """The expression handed over as message= to SonosNotifier.notify, or None for any other call.

    notify may be called, passed on to submit_to_executor, or reached through FireSafety's _announce.
    A call with a title is a MobileNotifier push.
    """
    if not isinstance(call, ast.Call):
        return None
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    if "message" not in keywords or "title" in keywords:
        return None
    name = call.func.attr if isinstance(call.func, ast.Attribute) else ""
    handed_over = any(isinstance(arg, ast.Attribute) and arg.attr == "notify" for arg in call.args)
    return keywords["message"] if name in ("notify", "_announce") or handed_over else None


def leaves(node, function, seen=()):
    """The expressions a message can come from: both branches of a conditional, what a local name is assigned."""
    if isinstance(node, ast.IfExp):
        return leaves(node.body, function, seen) + leaves(node.orelse, function, seen)
    if isinstance(node, ast.Name) and function is not None and node.id not in seen:
        values = [assign.value for assign in ast.walk(function)
                  if isinstance(assign, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == node.id for target in assign.targets)]
        if values:
            return [leaf for value in values for leaf in leaves(value, function, seen + (node.id,))]
    return [node]


def announcement_sites():
    """Every call in apps/ outside the tests that hands a sentence to SonosNotifier.notify."""
    sites = []
    for path in sorted(APPS.rglob("*.py")):
        relative = path.relative_to(APPS)
        if "tests" in relative.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for call in ast.walk(tree):
            message = message_argument(call)
            if message is None:
                continue
            function = call
            while function is not None and not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = parents.get(function)
            found = leaves(message, function)
            literals = [leaf.value for leaf in found
                        if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)]
            sites.append(Site(relative.as_posix(), ast.unparse(message), found, literals,
                              len(literals) < len(found)))
    return sites


def evaluate(expression, **names):
    """The value of a source expression (an f-string or a subscript) for the given local names."""
    return eval(compile(ast.Expression(expression), "<source>", "eval"), {"__builtins__": {}}, names)


def leaf_of(sites, path, key):
    (site,) = [site for site in sites if (site.path, site.key) == (path, key)]
    (leaf,) = site.leaves
    return leaf


def default_of(path, key):
    """The default in the source's self.args.get(key, default)."""
    tree = ast.parse((APPS / path).read_text(encoding="utf-8"))
    (call,) = [node for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
               and len(node.args) == 2 and isinstance(node.args[0], ast.Constant) and node.args[0].value == key]
    return ast.literal_eval(call.args[1])


def intercom_sentences(sites):
    settings = config("intercom/intercom.yaml", "Intercom")
    rings = {door: settings[f"tts_message_{door}"] for door in ("front", "back", "apt")}
    openings = {door: settings[f"door_open_message_{door}"] for door in ("front", "back")}
    sentences = set(rings.values())
    for door in ("front", "back"):
        info = {"message": rings[door], "followup": openings[door]}
        sentences.add(evaluate(leaf_of(sites, "intercom/intercom.py", "combined_message"), info=info))
    for kind in intercom.COURIER_HERE:
        for owner_away in (False, True):
            for opened in (None, *openings.values()):
                sentences.add(intercom.courier_message(kind, owner_away, opened))
    sentences.update(intercom.COURIER_AT_THE_DOOR.values())
    return sentences


def tv_sentences(sites):
    tree = ast.parse((APPS / "tv/living_room_tv_control.py").read_text(encoding="utf-8"))
    phrases = [assign.value.value for assign in ast.walk(tree)
               if isinstance(assign, ast.Assign) and isinstance(assign.value, ast.Constant)
               and any(isinstance(target, ast.Name) and target.id == "announce_phrase" for target in assign.targets)]
    leaf = leaf_of(sites, "tv/living_room_tv_control.py", "f'TV moving to {announce_phrase}'")
    return {evaluate(leaf, announce_phrase=phrase) for phrase in phrases}


def rooftop_sentences():
    openings = config("weather/weather_opening_alert.yaml", "WeatherOpeningAlert")["openings"]
    names = [opening["name"] for opening in openings if opening.get("rooftop")]
    return {weather_opening_alert.rooftop_tts_message([name]) for name in names} | {
        weather_opening_alert.rooftop_tts_message(names)}


def appliance_sentences():
    washer = config("appliances/washer.yaml", "washer_monitor")
    dryer = config("appliances/dryer.yaml", "dryer_monitor")
    source = "appliances/washer_monitor.py"
    return {
        washer.get("announce_message", default_of(source, "announce_message")),
        washer.get("drum_clean_announce_message", default_of(source, "drum_clean_announce_message")),
        dryer.get("announce_message", default_of("appliances/dryer_monitor.py", "announce_message")),
    }


class PrecacheList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = NOTIFIER["precache_messages"]
        cls.sites = announcement_sites()

    def test_the_list_is_clean(self):
        self.assertTrue(self.messages)
        for message in self.messages:
            self.assertIsInstance(message, str)
            self.assertEqual(message, message.strip())
            self.assertTrue(message)
        self.assertEqual(len(self.messages), len(set(self.messages)))

    def test_the_scan_finds_the_callers_it_is_meant_to(self):
        where = {site.path for site in self.sites}
        self.assertLessEqual({"appliances/dishwasher_monitor.py", "appliances/dryer_monitor.py",
                              "appliances/washer_monitor.py", "intercom/intercom.py",
                              "safety/fire_safety.py", "tv/living_room_tv_control.py",
                              "weather/weather_opening_alert.py"}, where)
        literals = {text for site in self.sites for text in site.literals}
        self.assertIn("Dishwasher is ready to be emptied", literals)
        self.assertIn("Smoke is building in the kitchen", literals)
        self.assertIn("Kitchen smoke alarm stopped reporting during the alarm — check the kitchen", literals)

    def test_every_literal_sentence_handed_to_notify_is_listed(self):
        for site in self.sites:
            for text in site.literals:
                with self.subTest(path=site.path, message=text):
                    self.assertIn(text, self.messages, f"{site.path} says {text!r}; add it to precache_messages")

    def test_every_other_call_has_been_reviewed_and_the_reviews_are_still_current(self):
        dynamic = {(site.path, site.key) for site in self.sites if site.dynamic}
        self.assertEqual(dynamic - set(REVIEWED), set(),
                         "a sentence built at run time reaches notify(): list its fixed sentences in "
                         "precache_messages and add the call to REVIEWED")
        self.assertEqual(set(REVIEWED) - dynamic, set(), "REVIEWED names a call that is gone")

    def test_the_templated_sentences_are_listed_as_the_code_builds_them(self):
        for name, sentences in (
            ("intercom", intercom_sentences(self.sites)),
            ("tv lift", tv_sentences(self.sites)),
            ("rooftop rain", rooftop_sentences()),
            ("appliances", appliance_sentences()),
        ):
            self.assertTrue(sentences, name)
            for text in sorted(sentences):
                with self.subTest(source=name, message=text):
                    self.assertIn(text, self.messages)


if __name__ == "__main__":
    unittest.main()
