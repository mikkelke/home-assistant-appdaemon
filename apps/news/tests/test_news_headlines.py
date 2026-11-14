# tests/test_news_headlines.py - NewsHeadlines: parsing DR's RSS feed, fetching it and the published entity.
# All headlines are made up.
# Run from repo root: python3 -m unittest discover -s apps/news/tests

import copy
import io
import sys
import types
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
        sys.modules[name] = types.ModuleType(name)
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import news_headlines as nh  # noqa: E402

YAML = Path(__file__).resolve().parents[1] / "news_headlines.yaml"
CONFIG = yaml.safe_load(YAML.read_text(encoding="utf-8"))["NewsHeadlines"]
NOW = datetime(2026, 11, 14, 15, 30, 7, tzinfo=timezone.utc)  # 16:30:07 in Copenhagen
FETCHED = "2026-11-14T16:30:07+01:00"
AGAIN = "DR headlines are fetched again"

SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
    <channel>
        <title>Test feed</title>
        <lastBuildDate>Sat, 14 Nov 2026 16:39:15 GMT</lastBuildDate>
        <item>
            <title><![CDATA[Ny cykelsti åbner ved havnen]]></title>
            <link>https://news.example/ny-cykelsti</link>
            <guid isPermaLink="false">urn:test:1</guid>
            <pubDate>Sat, 14 Nov 2026 14:04:15 GMT</pubDate>
            <description><![CDATA[Not used.]]></description>
            <media:content url="https://news.example/1.jpg" medium="image"/>
        </item>
        <item>
            <link>https://news.example/uden-titel</link>
            <pubDate>Sat, 14 Nov 2026 12:00:00 GMT</pubDate>
        </item>
        <item>
            <title>Plain title without CDATA</title>
            <pubDate>Fri, 13 Nov 2026 23:41:27 GMT</pubDate>
        </item>
        <item>
            <title><![CDATA[
                Havnebussen sejler igen: 'Det er godt' & fint
            ]]></title>
            <pubDate>Fri, 13 Nov 2026 09:36:59 +0100</pubDate>
        </item>
        <item>
            <title><![CDATA[Sommerfest fik besøg af 3000 gæster]]></title>
            <pubDate>Mon, 15 Jun 2026 21:30:00 GMT</pubDate>
        </item>
    </channel>
</rss>
"""
SAMPLE_ITEMS = [
    {"title": "Ny cykelsti åbner ved havnen", "at": "2026-11-14T15:04:15+01:00"},
    {"title": "Plain title without CDATA", "at": "2026-11-14T00:41:27+01:00"},
    {"title": "Havnebussen sejler igen: 'Det er godt' & fint", "at": "2026-11-13T09:36:59+01:00"},
    {"title": "Sommerfest fik besøg af 3000 gæster", "at": "2026-06-15T23:30:00+02:00"},
]


def item(title="Headline", date="Sat, 14 Nov 2026 14:04:15 GMT"):
    """One <item>; a None title or date leaves that element out."""
    title = "" if title is None else f"<title><![CDATA[{title}]]></title>"
    date = "" if date is None else f"<pubDate>{date}</pubDate>"
    return f"<item>{title}{date}</item>"


def feed(*items):
    return ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>t</title>'
            + "".join(items) + "</channel></rss>")


class ParseFeed(unittest.TestCase):
    def parse(self, xml_text, max_items=20):
        return nh.parse_feed(xml_text, max_items, nh.TZ)

    def titles(self, xml_text, max_items=20):
        return [entry["title"] for entry in self.parse(xml_text, max_items)]

    def test_titles_and_local_times_come_out_in_feed_order(self):
        self.assertEqual(self.parse(SAMPLE), SAMPLE_ITEMS)

    def test_bytes_parse_like_text(self):
        self.assertEqual(self.parse(SAMPLE.encode("utf-8")), SAMPLE_ITEMS)

    def test_gmt_becomes_copenhagen_time_with_the_offset_of_that_date(self):
        for date, expected in (
            ("Sat, 14 Nov 2026 14:04:15 GMT", "2026-11-14T15:04:15+01:00"),
            ("Mon, 15 Jun 2026 21:30:00 GMT", "2026-06-15T23:30:00+02:00"),
            ("Sun, 25 Oct 2026 00:30:00 GMT", "2026-10-25T02:30:00+02:00"),
            ("Sun, 25 Oct 2026 01:30:00 GMT", "2026-10-25T02:30:00+01:00"),
            ("Fri, 13 Nov 2026 23:41:27 GMT", "2026-11-14T00:41:27+01:00"),
            ("Fri, 13 Nov 2026 09:36:59 +0100", "2026-11-13T09:36:59+01:00"),
            ("Fri, 13 Nov 2026 09:36:59 -0500", "2026-11-13T15:36:59+01:00"),
        ):
            with self.subTest(date):
                self.assertEqual(self.parse(feed(item(date=date))), [{"title": "Headline", "at": expected}])

    def test_a_date_with_an_unknown_or_no_zone_is_utc(self):
        for date in ("Sat, 14 Nov 2026 14:04:15 -0000", "Sat, 14 Nov 2026 14:04:15"):
            with self.subTest(date):
                self.assertEqual(self.parse(feed(item(date=date)))[0]["at"], "2026-11-14T15:04:15+01:00")

    def test_cdata_is_plain_text_and_surrounding_whitespace_is_stripped(self):
        self.assertEqual(self.titles(feed(item(title="\n   Padded  \n\t"))), ["Padded"])
        self.assertEqual(self.titles(feed(item(title="Tom & Jerry <b>live</b>"))), ["Tom & Jerry <b>live</b>"])

    def test_an_item_without_a_title_is_left_out(self):
        xml_text = feed(item("First"), item(title=None), item(title=""), item(title=" \n "), item("Last"))
        self.assertEqual(self.titles(xml_text), ["First", "Last"])

    def test_an_item_with_an_unreadable_pubdate_is_left_out(self):
        bad_dates = (None, "", "yesterday", "2026-11-14T14:04:15Z", "Fri, 32 Nov 2026 14:04:15 GMT",
                     "Fri, 13 Nov 2026 25:00:00 GMT", "Fri, 31 Dec 9999 23:30:00 +0000")
        for date in bad_dates:
            with self.subTest(date):
                xml_text = feed(item("First"), item("Broken", date), item("Last"))
                self.assertEqual(self.titles(xml_text), ["First", "Last"])

    def test_the_cap_counts_the_items_that_are_kept(self):
        xml_text = feed(item(title=None), item("A"), item("B", date="nope"), item("C"), item("D"))
        self.assertEqual(self.titles(xml_text, max_items=2), ["A", "C"])

    def test_a_feed_without_items_gives_an_empty_list(self):
        self.assertEqual(self.parse(feed()), [])

    def test_text_that_is_not_xml_raises_parse_error(self):
        for text in ("", "<html><body>Service busy", "not xml at all"):
            with self.subTest(text), self.assertRaises(ET.ParseError):
                self.parse(text)


class FakeResponse:
    def __init__(self, body):
        self.body = body.encode("utf-8")
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]


def serving(body):
    return patch.object(nh.urllib.request, "urlopen", return_value=FakeResponse(body))


class LoadHeadlines(unittest.TestCase):
    def load(self, url=CONFIG["feed"]):
        return nh.load_headlines(url, 20, nh.TZ)

    def test_asks_for_the_feed_with_a_generic_user_agent_and_a_ten_second_timeout(self):
        response = FakeResponse(SAMPLE)
        with patch.object(nh.urllib.request, "urlopen", return_value=response) as urlopen:
            result = nh.load_headlines(CONFIG["feed"], 3, nh.TZ)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, CONFIG["feed"])
        self.assertEqual(request.header_items(), [("User-agent", "home-assistant-appdaemon")])
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 10})
        self.assertEqual(response.read_sizes, [nh.MAX_BYTES])
        self.assertEqual(result, {"ok": True, "items": SAMPLE_ITEMS[:3]})

    def test_a_failed_request_comes_back_as_a_short_reason(self):
        unavailable = urllib.error.HTTPError(CONFIG["feed"], 503, "Service Unavailable", {}, io.BytesIO())
        self.addCleanup(unavailable.close)
        unreachable = urllib.error.URLError(OSError(-2, "Name or service not known"))
        for error, reason in ((unavailable, "HTTPError: HTTP Error 503: Service Unavailable"),
                              (unreachable, "URLError: <urlopen error [Errno -2] Name or service not known>"),
                              (TimeoutError("timed out"), "TimeoutError: timed out"),
                              (ConnectionResetError("reset"), "ConnectionResetError: reset")):
            with self.subTest(reason), patch.object(nh.urllib.request, "urlopen", side_effect=error):
                self.assertEqual(self.load(), {"ok": False, "error": reason})

    def test_a_long_reason_is_cut_short(self):
        with patch.object(nh.urllib.request, "urlopen", side_effect=OSError("x" * 500)):
            result = self.load()
        self.assertEqual((result["ok"], len(result["error"])), (False, 100))

    def test_a_reply_that_is_not_a_feed_is_a_failure(self):
        with serving("<html><body>Service busy"):
            result = self.load()
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("ParseError: "), result["error"])

    def test_a_feed_without_usable_items_is_a_failure(self):
        for body in (feed(), feed(item(title=None), item("Broken", date="nope"))):
            with self.subTest(body), serving(body):
                self.assertEqual(self.load(), {"ok": False, "error": "no usable items in the feed"})

    def test_a_bad_address_is_a_failure_not_an_exception(self):
        with patch.object(nh.urllib.request, "urlopen", side_effect=AssertionError("no request expected")):
            result = self.load("not a url")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("ValueError: "), result["error"])


class Harness:
    """The app as AppDaemon would run it, with its scheduler, executor, clock and entity writes recorded."""

    def __init__(self, **args):
        self.published = []
        self.logs = []
        self.timers = []
        self.events = []
        self.queue = []
        self.app = nh.NewsHeadlines.__new__(nh.NewsHeadlines)
        self.app.args = args
        self.app.log = lambda message, level="INFO": self.logs.append((level, message))
        self.app.get_now = lambda: NOW
        self.app.set_state = lambda entity, state=None, attributes=None, replace=False, check_existence=True: (
            self.published.append((entity, state, copy.deepcopy(attributes), replace)))
        self.app.run_every = lambda callback, start, interval: self.timers.append((callback, start, interval))
        self.app.listen_event = lambda callback, event: self.events.append((callback, event))
        self.app.submit_to_executor = lambda func, *a, callback=None: self.queue.append((func, a, callback))
        self.app.initialize()

    def answer(self, result):
        self.app._on_result(result=result)

    def run_executor(self):
        func, args, callback = self.queue.pop(0)
        callback(result=func(*args))

    def messages(self, level):
        return [message for logged, message in self.logs if logged == level]

    @property
    def last(self):
        entity, state, attributes, replace = self.published[-1]
        return state, attributes


GOOD = {"ok": True, "items": SAMPLE_ITEMS}
BAD = {"ok": False, "error": "URLError: <urlopen error timed out>"}
UNAVAILABLE = "HTTPError: HTTP Error 503: Service Unavailable"


class App(unittest.TestCase):
    def test_start_fetches_at_once_and_then_every_interval(self):
        h = Harness(**CONFIG)
        self.assertEqual(h.queue, [(nh.load_headlines, (CONFIG["feed"], CONFIG["max_items"], nh.TZ),
                                    h.app._on_result)])
        self.assertEqual(h.timers, [(h.app._on_tick, f"now+{CONFIG['interval_s']}", CONFIG["interval_s"])])
        self.assertEqual(h.published, [])
        h.app._on_tick({})
        self.assertEqual(len(h.queue), 2)

    def test_the_yaml_names_the_module_and_class_and_drives_the_app(self):
        self.assertEqual((CONFIG["module"], CONFIG["class"]), ("news_headlines", "NewsHeadlines"))
        self.assertIs(getattr(nh, CONFIG["class"]), nh.NewsHeadlines)
        h = Harness(**CONFIG)
        h.answer(GOOD)
        self.assertEqual(h.published[-1][0], CONFIG["entity"])

    def test_the_defaults_in_the_code_are_the_values_in_the_yaml(self):
        h = Harness()
        self.assertEqual(h.queue[0][1], (CONFIG["feed"], CONFIG["max_items"], nh.TZ))
        self.assertEqual(h.timers[0][1:], (f"now+{CONFIG['interval_s']}", CONFIG["interval_s"]))
        h.answer(GOOD)
        self.assertEqual(h.published[-1][0], CONFIG["entity"])

    def test_a_good_fetch_publishes_the_whole_entity_replacing_the_old_one(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        self.assertEqual(len(h.published), 1)
        entity, state, attributes, replace = h.published[0]
        self.assertEqual((entity, state, replace), ("sensor.news_headlines", SAMPLE_ITEMS[0]["at"], True))
        self.assertEqual(attributes, {"friendly_name": "News headlines", "icon": "mdi:newspaper", "source": "DR",
                                      "fetched": FETCHED, "items": SAMPLE_ITEMS})
        self.assertEqual(h.messages("WARNING"), [])
        self.assertNotIn(AGAIN, h.messages("INFO"))

    def test_a_newer_fetch_replaces_the_items_and_the_state(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        newer = [{"title": "Breaking", "at": "2026-11-14T16:00:00+01:00"}] + SAMPLE_ITEMS
        h.answer({"ok": True, "items": newer})
        self.assertEqual(h.last[0], "2026-11-14T16:00:00+01:00")
        self.assertEqual(h.last[1]["items"], newer)

    def test_a_first_fetch_that_fails_publishes_unknown_with_the_reason(self):
        h = Harness(**CONFIG)
        h.answer(BAD)
        entity, state, attributes, replace = h.published[-1]
        self.assertEqual((entity, state, replace), ("sensor.news_headlines", "unknown", True))
        self.assertEqual(attributes, {"friendly_name": "News headlines", "icon": "mdi:newspaper", "source": "DR",
                                      "fetched": None, "items": [], "error": BAD["error"]})
        self.assertEqual(h.messages("WARNING"), ["DR headlines not fetched: " + BAD["error"]])

    def test_a_failure_keeps_the_last_items_and_warns_once_for_the_whole_streak(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        good_state, good_attributes = h.last
        for reason in (UNAVAILABLE, "TimeoutError: timed out", "ParseError: no element found"):
            h.answer({"ok": False, "error": reason})
            state, attributes = h.last
            self.assertEqual(state, good_state)
            self.assertEqual(attributes, {**good_attributes, "error": reason})
        self.assertEqual(h.messages("WARNING"), ["DR headlines not fetched: " + UNAVAILABLE])

    def test_recovery_clears_the_error_and_logs_info_and_a_new_streak_warns_again(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        h.answer(BAD)
        h.answer(BAD)
        h.answer(GOOD)
        state, attributes = h.last
        self.assertNotIn("error", attributes)
        self.assertEqual(attributes["items"], SAMPLE_ITEMS)
        self.assertEqual(h.messages("INFO").count(AGAIN), 1)
        h.answer(GOOD)
        self.assertEqual(h.messages("INFO").count(AGAIN), 1)
        h.answer(BAD)
        self.assertEqual(len(h.messages("WARNING")), 2)

    def test_a_failure_leaves_fetched_at_the_last_good_fetch(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        h.app.get_now = lambda: datetime(2026, 11, 14, 18, 0, 0, tzinfo=timezone.utc)
        h.answer(BAD)
        self.assertEqual(h.last[1]["fetched"], FETCHED)
        h.answer(GOOD)
        self.assertEqual(h.last[1]["fetched"], "2026-11-14T19:00:00+01:00")

    def test_an_answer_that_is_no_result_counts_as_a_failure(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        for result in (None, {}, {"ok": False}, "boom"):
            h.answer(result)
            self.assertEqual(h.last[1]["error"], "no result")
            self.assertEqual(h.last[1]["items"], SAMPLE_ITEMS)

    def test_a_home_assistant_restart_publishes_the_entity_again(self):
        h = Harness(**CONFIG)
        self.assertEqual(h.events, [(h.app._on_plugin_started, "plugin_started")])
        h.answer(GOOD)
        h.answer(BAD)
        before = h.published[-1]
        h.app._on_plugin_started("plugin_started", {}, {})
        self.assertEqual(len(h.published), 3)
        self.assertEqual(h.published[-1], before)

    def test_a_restart_before_the_first_fetch_publishes_unknown(self):
        h = Harness(**CONFIG)
        h.app._on_plugin_started("plugin_started", {}, {})
        state, attributes = h.last
        self.assertEqual((state, attributes["items"], attributes["fetched"]), ("unknown", [], None))
        self.assertNotIn("error", attributes)

    def test_the_whole_path_from_the_feed_to_the_entity(self):
        h = Harness(**CONFIG)
        with serving(SAMPLE):
            h.run_executor()
        state, attributes = h.last
        self.assertEqual((state, attributes["items"], attributes["fetched"]),
                         (SAMPLE_ITEMS[0]["at"], SAMPLE_ITEMS, FETCHED))
        h.app._on_tick({})
        with patch.object(nh.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            h.run_executor()
        state, attributes = h.last
        self.assertEqual((state, attributes["items"], attributes["error"]),
                         (SAMPLE_ITEMS[0]["at"], SAMPLE_ITEMS, "TimeoutError: timed out"))

    def test_the_configured_cap_reaches_the_published_items(self):
        h = Harness(**{**CONFIG, "max_items": 2})
        with serving(SAMPLE):
            h.run_executor()
        self.assertEqual(h.last[1]["items"], SAMPLE_ITEMS[:2])


if __name__ == "__main__":
    unittest.main()
