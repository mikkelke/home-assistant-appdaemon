"""
BleScannerWatch - pushes the owner when a Bluetooth scanner that feeds Bermuda (an ESPHome proxy,
a Shelly, the host's own adapter) stops hearing devices. Notify only - never controls a device.

CHECK. Every interval_min minutes the app calls bermuda.dump_devices for the configured devices,
which always include every scanner (the full dump of every device Bermuda knows is many MB). A
scanner device (`_is_scanner`) carries last_seen, the moment its Home Assistant scanner last
delivered any advertisement, on HA's time.monotonic() clock (HA and AppDaemon share the host, so
the clock). A scanner whose last_seen is 0 (never) or more than fresh_s seconds old is deaf.

PUSH. A scanner that is deaf on every check for deaf_min minutes gets one push via MobileNotifier,
target="user", per episode. The episode ends at the first check where it is not deaf: that
is logged, never pushed, and the next deaf spell starts from zero. Deafness is counted in checks,
not read off the clock: the first deaf check starts a streak and every further one adds
interval_min minutes, so with the defaults the push comes at the fourth deaf check and does not
wobble with scheduler jitter. A push that reached nobody is tried again at the next check.
Scanners in `ignore` (lowercase addresses) are never judged.

SKIPPED CHECKS. A dump that fails, times out, or holds no devices or no scanner devices is skipped
with a warning. So is one whose clocks look unrelated: some scanner has a last_seen and none is 0
to a day old, which is warned about once until a check reads fine again. A skipped check is not a
deaf check: it neither extends nor ends a streak.
"""

from __future__ import annotations

import asyncio
import math
import time

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

DUMP_TIMEOUT_S = 30
DAY_S = 86400
TITLE = "Bluetooth scanner deaf"


def _number(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return value if ok else None


def devices_from(resp):
    """{address: device} out of AppDaemon's envelope for bermuda.dump_devices, {} when there is
    none. Only dicts with an `adverts` dict are devices: an error envelope holds dicts that are not."""
    node = resp
    for key in ("result", "response"):
        if isinstance(node, dict) and isinstance(node.get(key), dict):
            node = node[key]
    if not isinstance(node, dict):
        return {}
    return {str(addr): dev for addr, dev in node.items()
            if isinstance(dev, dict) and isinstance(dev.get("adverts"), dict)}


def read_dump(devices, now):
    """(scanners, ages, mismatch) for one dump. `now` is the monotonic clock. scanners is {address:
    name} of every scanner device and ages {address: seconds since its last_seen, None when it was
    never seen}. mismatch is true when some scanner has a last_seen and none is 0 to a day old,
    which is what unrelated clocks look like."""
    scanners, ages = {}, {}
    for addr, dev in devices.items():
        if dev.get("_is_scanner") is not True:
            continue
        address = addr.lower()
        scanners[address] = str(dev.get("name") or addr)
        stamp = _number(dev.get("last_seen"))
        ages[address] = now - stamp if stamp is not None and stamp > 0 else None
    known = [age for age in ages.values() if age is not None]
    return scanners, ages, bool(known) and not any(0 <= age <= DAY_S for age in known)


def is_deaf(age, fresh_s):
    return age is None or not 0 <= age <= fresh_s


def _age_text(age):
    return "never" if age is None else f"{age:.0f} s"


class BleScannerWatch(hass.Hass):
    def initialize(self):
        a = self.args
        self.interval_min = float(a.get("interval_min", 10))
        self.fresh_s = float(a.get("fresh_s", 120))
        self.deaf_min = float(a.get("deaf_min", 30))
        self.ignore = {str(address).lower() for address in a.get("ignore") or []}
        if self.interval_min <= 0 or self.fresh_s <= 0 or self.deaf_min < 0:
            raise ValueError("BleScannerWatch: interval_min and fresh_s must be positive and deaf_min not negative")

        self._notifier = self.get_app("MobileNotifier")
        self._deaf = {}
        self._clock_warned = False

        self.run_every(self._tick, "now+60", round(self.interval_min * 60))
        self.log(f"BleScannerWatch initialized: a check every {self.interval_min:g} min, a scanner is deaf when it "
                 f"heard no device in {self.fresh_s:g} s, pushed after {self.deaf_min:g} min deaf, "
                 f"{len(self.ignore)} ignored", level="INFO")

    def _tick(self, kwargs):
        self.create_task(self._check())

    # ---------- evaluation ----------

    async def _check(self):
        try:
            resp = await asyncio.wait_for(
                self.call_service("bermuda/dump_devices", configured_devices=True, return_response=True),
                timeout=DUMP_TIMEOUT_S)
        except Exception as e:
            self.log(f"BleScannerWatch: check skipped, bermuda.dump_devices failed: {e!a}", level="WARNING")
            return
        devices = devices_from(resp)
        if not devices:
            self.log(f"BleScannerWatch: check skipped, bermuda.dump_devices returned no devices: "
                     f"{str(resp)[:100]!a}", level="WARNING")
            return
        scanners, ages, mismatch = read_dump(devices, self._now())
        if mismatch:
            if not self._clock_warned:
                self.log("BleScannerWatch: check skipped, no scanner in the dump has a last_seen 0 to a day old "
                         "by Home Assistant's clock: the clocks differ or the dump changed", level="WARNING")
            self._clock_warned = True
            return
        self._clock_warned = False
        if not scanners:
            self.log("BleScannerWatch: check skipped, the dump has no scanner devices", level="WARNING")
            return

        listing = ", ".join(f"{scanners[address]} {_age_text(ages[address])}"
                            for address in sorted(scanners, key=lambda address: scanners[address]))
        self.log(f"BleScannerWatch: last heard: {listing}", level="DEBUG")

        for address, name in scanners.items():
            if address in self.ignore:
                continue
            if is_deaf(ages[address], self.fresh_s):
                await self._count_deaf(address, name)
            else:
                self._hears_again(address, name, ages[address])

    async def _count_deaf(self, address, name):
        streak = self._deaf.setdefault(address, {"checks": 0, "pushed": False})
        streak["checks"] += 1
        if streak["checks"] == 1:
            self.log(f"BleScannerWatch: {name} ({address}) is deaf: no device heard in the last "
                     f"{self.fresh_s:g} s", level="INFO")
        minutes = (streak["checks"] - 1) * self.interval_min
        if streak["pushed"] or minutes < self.deaf_min:
            return
        streak["pushed"] = True
        self.log(f"BleScannerWatch: {name} ({address}) deaf for {minutes:.0f} min, pushing the owner", level="INFO")
        await self._push(streak, name, minutes)

    def _hears_again(self, address, name, age):
        streak = self._deaf.pop(address, None)
        if streak is not None:
            self.log(f"BleScannerWatch: {name} ({address}) hears again after {streak['checks']} deaf check(s), "
                     f"last heard {age:.0f} s ago", level="INFO")

    async def _push(self, streak, name, minutes):
        message = f"{name} has heard no Bluetooth devices for {minutes:.0f} min. Rebooting it usually fixes it."
        try:
            sent = await self._notifier.notify(title=TITLE, message=message, target="user")
        except Exception as e:
            self.log(f"BleScannerWatch: {name} notify failed: {e!a}", level="WARNING")
            streak["pushed"] = False
            return
        if not sent:
            self.log(f"BleScannerWatch: {name} notify reached no recipients, will retry", level="WARNING")
            streak["pushed"] = False

    # ---------- misc ----------

    def _now(self):
        """HA's time.monotonic() clock, the one the dump's last_seen is on; the host is shared, so this
        process reads the same clock."""
        return time.monotonic()
