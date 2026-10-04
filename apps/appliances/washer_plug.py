"""Direct reads of the Shelly Plug S Gen3 (local RPC, no auth) and the finish decisions made on them.

The recorder only holds change reports, so a missing report and a held value look the same. PlugPoller reads the plug
every POLL_S seconds from its own thread into a ring of (monotonic_seconds, watts), watts None for a failed read. Every
decision below goes through window(): it is made only on reads that all succeeded, that sit at most MAX_GAP_FACTOR
poll intervals apart, that reach back to the start of the evidence the decision needs, and whose newest member is
fresh. Anything else is "no decision yet", never a guess.

The reads run on a private thread rather than an AppDaemon schedule: AppDaemon's event loop stalls for seconds at a
time (its utility loop re-parses the app files on the loop), and a scheduled read landing that late breaks the window
it falls in. The thread only ever waits for the GIL, which one file parse holds far shorter than the gap bound.
"""
import json
import socket
import threading
import time
from collections import deque

POLL_S = 2.0
DEADLINE_S = 1.0             # one read never takes longer than this, connect to last byte
MAX_GAP_FACTOR = 1.5         # a nudge lasts ~4 s, so reads <= 3 s apart always land at least one sample inside it
RING_S = 45 * 60             # the longest final spin chain seen is ~25 min; plus its 3-min lead-in and the train

STANDBY_W = 3.0; STANDBY_S = 180
SPIN_W = 150.0; SPIN_STRONG_W = 250.0; SPIN_STRONG_S = 60.0; SPIN_GAP_S = 180; SPIN_MAX_AGE_S = 20 * 60
HEAT_W = 1000.0; HEAT_S = 30.0
DRAIN_S = 45; TRAIN_S = 120; TRAIN_PEAK_W = 60.0; IDLE_W = 4.5; IDLE_FRAC = 0.30
PULSE_ON_W = 15.0; PULSE_OFF_W = 7.5; MIN_PULSES = 4
# A pulse is judged by the reads that bracket it (last at or below PULSE_OFF_W before, first after): an upper bound on
# how long it really lasted. Nudges (<= 7 s) bracket at <= 10 s on the 2 s grid; the interim tumbles that must be
# rejected (>= 15 s) bracket at >= 16 s.
PULSE_MAX_S = 11.0
RESUME_W = 18.0; RESUME_S = 60
HARD_OFF_W = 0.5; HARD_OFF_S = 300
ACTIVITY_W = 150.0


def read_switch(host, deadline_s=DEADLINE_S, clock=time.monotonic):
    """apower (W) from Switch.GetStatus. Raises on any failure, including the overall deadline. `host` is an address:
    a name lookup would run outside the deadline."""
    end = clock() + deadline_s

    def left():
        remaining = end - clock()
        if remaining <= 0:
            raise TimeoutError("plug read deadline")
        return remaining

    with socket.create_connection((host, 80), timeout=left()) as s:
        s.settimeout(left())
        s.sendall(b"GET /rpc/Switch.GetStatus?id=0 HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
        buf = b""
        while True:
            s.settimeout(left())
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n", 1)[0]:
        raise OSError(f"plug answered {head[:40]!r}")
    return float(json.loads(body)["apower"])


class PlugPoller:
    """Reads the plug every poll_s from one daemon thread into a bounded ring; the app only takes snapshots and never
    blocks on the plug, and the thread never touches AppDaemon. start() retires any earlier thread (it exits after at
    most one more read, which it then discards), stop() retires the current one (the app's terminate() calls it), and
    clear() starts a fresh evidence period: a read that was in flight across it is dropped."""

    def __init__(self, read, poll_s=POLL_S, clock=time.monotonic):
        self._read = read
        self.poll_s = poll_s
        self.clock = clock
        self._lock = threading.Lock()
        self._ring = deque(maxlen=int(RING_S / poll_s) + 1)
        self._gen = 0
        self._epoch = 0
        self._peak = 0.0
        self._last_ok = clock()

    def start(self):
        with self._lock:
            self._gen += 1
            gen = self._gen
        threading.Thread(target=self._run, args=(gen,), name="washer-plug", daemon=True).start()

    def stop(self):
        with self._lock:
            self._gen += 1

    def clear(self):
        with self._lock:
            self._ring.clear()
            self._epoch += 1
            self._peak = 0.0

    def _run(self, gen):
        due = self.clock()
        while True:
            with self._lock:
                if gen != self._gen:
                    return
            self.poll_once(gen)
            due += self.poll_s
            wait = due - self.clock()
            if wait < 0:
                due, wait = self.clock(), 0.0
            time.sleep(wait)

    def poll_once(self, gen=None):
        """One read, timestamped when it was issued. Dropped if the poller was retired or cleared meanwhile."""
        with self._lock:
            epoch = self._epoch
        ts = self.clock()
        try:
            watts = self._read()
        except Exception:
            watts = None
        with self._lock:
            if epoch != self._epoch or (gen is not None and gen != self._gen):
                return
            self._ring.append((ts, watts))
            if watts is not None:
                self._last_ok = ts
                self._peak = max(self._peak, watts)

    def snapshot(self):
        with self._lock:
            return list(self._ring)

    def last_ok(self):
        """Monotonic time of the newest successful read (the poller's creation time before any)."""
        with self._lock:
            return self._last_ok

    def peak(self):
        """Highest watts read since the last clear()."""
        with self._lock:
            return self._peak


def window(samples, now, seconds, poll_s=POLL_S):
    """The reads covering [now - seconds, now], oldest first, when that span is fully observed; None otherwise."""
    start = now - seconds
    max_gap = MAX_GAP_FACTOR * poll_s
    out = []
    for ts, w in reversed(samples):
        if w is None:
            return None
        if out and out[-1][0] - ts > max_gap:
            return None
        out.append((ts, w))
        if ts <= start:
            break
    else:
        return None
    if now - out[0][0] > max_gap:
        return None
    out.reverse()
    return out


def standby(samples, now, poll_s=POLL_S):
    win = window(samples, now, STANDBY_S, poll_s)
    return win is not None and all(w <= STANDBY_W for _, w in win)


def resumed(samples, now, poll_s=POLL_S):
    """A published Unemptied was wrong only if the machine is demonstrably washing again (a nudge lasts ~4 s)."""
    win = window(samples, now, RESUME_S, poll_s)
    return win is not None and all(w >= RESUME_W for _, w in win)


def hard_off(samples, now, poll_s=POLL_S):
    win = window(samples, now, HARD_OFF_S, poll_s)
    return win is not None and all(w <= HARD_OFF_W for _, w in win)


def _seconds_at_or_above(seg, lo, hi_ts, threshold):
    """Step-hold time the reads in seg[lo..] up to hi_ts spent at or above threshold."""
    return sum(seg[i + 1][0] - seg[i][0] for i in range(lo, len(seg) - 1) if seg[i][0] <= hi_ts and seg[i][1] >= threshold)


def spin_end(samples, now, poll_s=POLL_S):
    """Monotonic time the final spin ended, or None. One observed stretch must hold all of the evidence: SPIN_GAP_S of
    reads before the spin chain (so nothing hidden could have linked it to a heating phase), the chain itself (a real
    spin, not heating), the drain, and a TRAIN_S anti-crease nudge train that is still going on at `now`."""
    hi = [i for i, (_, w) in enumerate(samples) if w is not None and w >= SPIN_W]
    if not hi or hi[-1] + 1 >= len(samples):
        return None
    k = hi[-1]
    j = k
    for i in reversed(hi):
        if samples[j][0] - samples[i][0] > SPIN_GAP_S:
            break
        j = i
    t_first, t_last = samples[j][0], samples[k][0]
    seg = window(samples, now, now - t_first + SPIN_GAP_S, poll_s)
    if seg is None:
        return None
    end_ts = next(t for t, w in seg if t > t_last)
    if now - end_ts > SPIN_MAX_AGE_S:
        return None
    lo = next(i for i, (t, _) in enumerate(seg) if t >= t_first)
    if _seconds_at_or_above(seg, lo, t_last, HEAT_W) >= HEAT_S:
        return None
    if _seconds_at_or_above(seg, lo, t_last, SPIN_STRONG_W) < SPIN_STRONG_S:
        return None
    return end_ts if _is_train(seg, now - TRAIN_S, end_ts + DRAIN_S) else None


def spin_end_since(samples, since, now, poll_s=POLL_S):
    """spin_end decided at each read in (since, now], oldest first, then at `now` itself: the first end found, provided
    every read after the deciding one is at or below TRAIN_PEAK_W and the reads from it to `now` are all observed and
    fresh (window), so the train is still going at `now`. spin_end holds only on the idle reads between nudges and
    fails for TRAIN_S after a nudge that runs long, so deciding at one instant per tick can step over every instant it
    holds; the reads since the previous tick cannot be stepped over."""
    for i, (t, _) in enumerate(samples):
        if t <= since or t > now:
            continue
        end = spin_end(samples[:i + 1], t, poll_s)
        if end is None:
            continue
        tail = window(samples, now, now - t, poll_s)
        if tail is not None and all(w <= TRAIN_PEAK_W for _, w in tail):
            return end
    return spin_end(samples, now, poll_s)


def _is_train(seg, win_start, since):
    """seg from win_start on is an anti-crease nudge train starting after `since`: peak <= TRAIN_PEAK_W, at least
    IDLE_FRAC at the idle level, >= MIN_PULSES nudges starting inside it, and every pulse touching it shorter than
    PULSE_MAX_S. The newest read must be at the off level: a pulse still in progress has no bound yet."""
    first = next((i for i, (t, _) in enumerate(seg) if t >= win_start), None)
    if first is None or seg[first][0] < since or seg[-1][1] > PULSE_OFF_W:
        return False
    ws = [w for _, w in seg[first:]]
    if max(ws) > TRAIN_PEAK_W or sum(1 for w in ws if w <= IDLE_W) < IDLE_FRAC * len(ws):
        return False
    pulses = _pulses(seg, first)
    return (pulses is not None and all(length < PULSE_MAX_S for length, _ in pulses)
            and sum(1 for _, inside in pulses if inside) >= MIN_PULSES)


def _pulses(seg, first):
    """(length_s, starts_inside) for every pulse touching seg[first:] - a run of reads above PULSE_OFF_W reaching
    above PULSE_ON_W - measured between the reads that bracket it (the last at or below PULSE_OFF_W before, the first
    after), so never shorter than the pulse really was. None when a pulse is not bracketed inside seg."""
    out = []
    i = first
    while i > 0 and seg[i][1] > PULSE_OFF_W:
        i -= 1
    while i < len(seg):
        if seg[i][1] <= PULSE_OFF_W:
            i += 1
            continue
        a = i
        while i < len(seg) and seg[i][1] > PULSE_OFF_W:
            i += 1
        if max(w for _, w in seg[a:i]) <= PULSE_ON_W:
            continue
        if a == 0 or i == len(seg):
            return None
        out.append((seg[i][0] - seg[a - 1][0], a - 1 >= first))
    return out
