"""NewsHeadlines: DR's latest headlines as one entity, for the voice assistant to read out.

The RSS feed is fetched at start and every interval_s, on the executor, and published as an entity; a Home
Assistant restart drops an entity made by set_state, so plugin_started publishes it again.
state       the local time (ISO 8601) of the newest headline; "unknown" until a fetch has worked
attributes  source ("DR"); fetched, the local time of the last good fetch; items, [{title, at}] newest first,
            at most max_items; error, a short reason, while the latest fetch has failed

An item needs a title and a readable RFC 822 pubDate, or it is left out. A fetch that fails, a feed that is not
valid XML and a feed without usable items all keep the last good items. The first failure of a streak is logged
as a warning, the recovery as info.
"""

import urllib.request
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

TZ = ZoneInfo("Europe/Copenhagen")
ENTITY = "sensor.news_headlines"
FEED = "https://www.dr.dk/nyheder/service/feeds/allenyheder"
USER_AGENT = "home-assistant-appdaemon"
TIMEOUT_S = 10
MAX_BYTES = 4_000_000


def _local_iso(pub_date, tz):
    """An RFC 822 date as ISO 8601 in tz, None when it cannot be read. A date without a zone is UTC."""
    try:
        moment = parsedate_to_datetime(pub_date)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(tz).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def parse_feed(xml_text, max_items, tz):
    """The feed's [{title, at}] in feed order, at most max_items; at is the pubDate in tz as ISO 8601.

    An item without a title or with an unreadable pubDate is left out. Raises ET.ParseError for malformed XML.
    """
    items = []
    for item in ET.fromstring(xml_text).iter("item"):
        title = (item.findtext("title") or "").strip()
        at = _local_iso(item.findtext("pubDate"), tz)
        if title and at:
            items.append({"title": title, "at": at})
    return items[:max_items]


def load_headlines(url, max_items, tz):
    """Fetch and parse the feed on the executor. Never raises: the answer is {"ok": True, "items": [...]} or
    {"ok": False, "error": <short reason>}."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            items = parse_feed(response.read(MAX_BYTES), max_items, tz)
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"[:100]}
    if not items:
        return {"ok": False, "error": "no usable items in the feed"}
    return {"ok": True, "items": items}


class NewsHeadlines(hass.Hass):
    def initialize(self):
        self.entity = self.args.get("entity", ENTITY)
        self.feed = self.args.get("feed", FEED)
        self.max_items = int(self.args.get("max_items", 20))
        interval_s = int(self.args.get("interval_s", 900))
        self.headlines = []
        self.fetched = None
        self.failure = None
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.run_every(self._on_tick, f"now+{interval_s}", interval_s)
        self._on_tick({})
        self.log(f"NewsHeadlines: {self.feed} every {interval_s} s into {self.entity}")

    def _on_tick(self, kwargs):
        self.submit_to_executor(load_headlines, self.feed, self.max_items, TZ, callback=self._on_result)

    def _on_plugin_started(self, event_name, data, kwargs):
        self._publish()

    def _on_result(self, result=None, **kwargs):
        result = result if isinstance(result, dict) else {}
        if result.get("ok"):
            if self.failure is not None:
                self.log("DR headlines are fetched again", level="INFO")
            self.headlines, self.failure = result["items"], None
            self.fetched = self.get_now().astimezone(TZ).isoformat(timespec="seconds")
        else:
            reason = result.get("error") or "no result"
            if self.failure is None:
                self.log(f"DR headlines not fetched: {reason}", level="WARNING")
            self.failure = reason
        self._publish()

    def _publish(self):
        attributes = {"friendly_name": "News headlines", "icon": "mdi:newspaper", "source": "DR",
                      "fetched": self.fetched, "items": self.headlines}
        if self.failure is not None:
            attributes["error"] = self.failure
        state = self.headlines[0]["at"] if self.headlines else "unknown"
        self.set_state(self.entity, state=state, attributes=attributes, replace=True, check_existence=False)
