"""
BedroomBlindOwner - the one app that moves cover.bedroom_blind in normal operation.

``blind_arbiter`` (bd0ee55) put the precedence for this blind in one place as data:

    manual wall press (40)  >  venting need (30)  >  wake target (20)  >  solar shade (10)

but every writer still issued its own ``cover/set_cover_position``, and each still
carried a private notion of "somebody moved it by hand" (bedroom_blind_control's
Z-Wave gestures, bedroom_solar_shade's baseline-diff pause, wakeup's force toggle).
This app closes the seam: the writers hand their *request* here (via ``get_app``),
this app resolves the full claim set through blind_arbiter, moves the blind only
when the winning position changes, and holds the single manual-pause.

WHAT "MANUAL" MEANS HERE - one definition, replacing three:

  * a wall-remote gesture (bedroom_blind_control ranks it ``manual`` when it asks), and
  * an *uncommanded* position change - a move this app did not order - detected on the
    cover itself and debounced with the same settle logic bedroom_solar_shade used
    (the motor reports ~every 5 s while travelling; silence for manual_settle_seconds
    means it stopped).

Either one sets a manual claim at the position the human chose, active for
``manual_pause_min``. While it is active nothing automatic moves the blind, and
askers are told who outranked them so they can react (the wake routine starts its
light ramp anyway instead of waiting for a blind that will never arrive).

OWN WRITE vs HUMAN WRITE - the family_room_lights discrimination (2026-08-12):
AppDaemon's service calls carry ``context.user_id == appdaemon_user_id`` (yaml knob,
never hardcoded); a dashboard/UI move carries a person's user id; a physical remote
carries none. A report with a person's id is manual immediately. A report with the
AppDaemon id that this app did not order is another app's *fallback* write (see
FAIL-SAFE below) - adopted as the new baseline, never manual. A context-less report
is judged by the expectation window: attributed to this app's own in-flight command
if one is pending, otherwise it is a hand on the physical remote -> manual.

FAIL-SAFE (this is the alarm): every caller keeps a byte-identical direct-write
fallback for when this app is missing, failed to load, or raises. That fallback
write lands here as an AppDaemon-context uncommanded move and is adopted quietly.
A restart never moves the blind: state is reloaded, nothing is re-commanded.

POSITIONS ARE IN THE DEVICE'S FRAME - 100 = risen = COVERING the window, 38 = the
parked/privacy position, and HA's open/closed wording is inverted for this cover.
Read the banner in bedroom_blind_control.yaml before touching any number. This app
never interprets a position; it only decides whose number is used.

Restart-safe: claims (including the manual hold), the position baseline and any
in-flight own-command expectation persist to bedroom_blind_owner_state.json
(atomic tmp+replace). On the deploy that introduces this app, an in-flight manual
pause recorded by bedroom_solar_shade's own state file is adopted once, as a
position-less manual veto - the shade's file itself is left untouched.

Publishes sensor.bedroom_blind_owner (same transparency style as
sensor.smart_cooling_status): winning source, position, the whole claim set, and
the manual-pause state.
"""

import json
import os
import threading
from datetime import datetime, timedelta

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

# Deliberately NOT a defensive import: without the arbiter this app cannot rank
# anybody, so it must fail to load - which flips every caller onto its direct-write
# fallback. Half an owner would be worse than none.
import blind_arbiter
import cover_util


class BedroomBlindOwner(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.cover = a("cover_entity", "cover.bedroom_blind")
        # This install's AppDaemon user id - service calls from any AppDaemon app
        # carry it in context.user_id. Same knob and same warning as
        # family_room_lights.yaml; never hardcoded.
        self.appdaemon_user_id = str(a("appdaemon_user_id") or "").strip() or None
        if not self.appdaemon_user_id:
            self.log(
                "appdaemon_user_id not configured - fallback writes from other apps "
                "may be mistaken for manual moves",
                level="WARNING",
            )
        self.manual_pause_min = float(a("manual_pause_min", 120))
        # Claim lifetimes. wake/vent match bedroom_solar_shade's wake_grace_min (20):
        # the shade starts asking exactly as the wake claim lapses, so the owner's
        # ranking and the shade's own gates cannot disagree about the morning.
        # shade outlives one 10-min tick so a standing claim never expires mid-cycle.
        self.claim_ttl_min = {
            blind_arbiter.SOURCE_MANUAL: float(a("manual_pause_min", 120)),
            blind_arbiter.SOURCE_VENT: float(a("vent_ttl_min", 20)),
            blind_arbiter.SOURCE_WAKE: float(a("wake_ttl_min", 20)),
            blind_arbiter.SOURCE_SHADE: float(a("shade_ttl_min", 15)),
        }
        # Same tolerance and settle numbers as bedroom_solar_shade, so moving the
        # detection here cannot change what counts as "a move" or "stopped".
        self.pos_tol = int(a("position_tolerance", 6))
        self.manual_settle_s = float(a("manual_settle_seconds", 12))
        # How long a commanded move may keep producing position reports before an
        # off-target settle stops being "our travel". Full range is ~60 s at the
        # measured ~0.6 s/percent (2026-07-27), so 90 s covers it with margin.
        self.own_move_window_s = float(a("own_move_window_sec", 90))
        # Post-write arrival check, moved from bedroom_blind_control (the writer is
        # the one who knows what it commanded). 70 s > full-range travel; 2 s used
        # to warn on every close - 99 false "motor issue" lines.
        self.verification_delay = float(a("verification_delay", 70.0))
        self.verification_tolerance = int(a("verification_tolerance", 5))
        self.status_entity = a("status_entity", "sensor.bedroom_blind_owner")
        self.state_file = a("state_file", "/conf/apps/blinds/bedroom_blind_owner_state.json")
        # One-time adoption of an in-flight manual pause across the deploy that
        # introduces this app - the shade's file schema stays untouched.
        self.shade_state_file = a(
            "shade_state_file", "/conf/apps/blinds/bedroom_solar_shade_state.json"
        )
        self.dry_run = bool(a("dry_run", False))

        # Cross-app calls (request/withdraw/manual_hold_*) run on the CALLER's
        # worker thread; the cover listener runs on this app's. One lock keeps the
        # claim table and the episode machine coherent.
        self._lock = threading.RLock()
        #: {source: {"position": int|None, "reason": str, "until": datetime|None}}
        self._claims = {}
        #: Last settled position this app has accounted for (manual baseline).
        self._baseline = None
        #: In-flight own command: {"target": int, "source": str, "until": datetime}.
        self._pending = None
        #: Last write issued: {"position": int, "source": str, "at": iso str}.
        self._last_cmd = None
        self._cmd_seq = 0
        # Motor-travel episode (see _ingest_position / _motor_settled).
        self._episode = None
        self._settle_handle = None
        self._republish_handle = None

        self._load_state()
        if self._baseline is None:
            seed = cover_util.position(self, self.cover)
            if seed is not None:
                self._baseline = int(seed)
                self.log(f"No persisted baseline - seeded from current position {self._baseline}%")
        self._adopt_shade_manual_pause()

        self.listen_state(self._on_cover_change, self.cover, attribute="current_position")
        # Republish + reload only - an AppDaemon restart must never move the blind.
        self._publish("restart")
        self._arm_republish_at_next_expiry()
        self.log(
            f"BedroomBlindOwner started - owns {self.cover}; "
            f"claims: {self._describe_claims()}; dry_run={self.dry_run}"
        )

    # ---------- public API (called by the writers via get_app) ----------
    def request(self, source, position, *, reason="", ttl_min=None):
        """One source's current claim on the blind. Records it, re-arbitrates the
        whole set, moves the blind only when the asking source wins AND the winning
        position differs from where the blind sits.

        Returns {"granted", "winner", "position", "manual_until"} so the caller can
        react (the wake routine starts its ramp anyway on a manual refusal).
        Raises on an unregistered source - the caller's try/except then falls back
        to its direct write, which is safer than silently storing a rank-0 claim.
        """
        src = str(source or "").strip().lower()
        if src not in blind_arbiter.PRIORITIES:
            raise ValueError(f"unknown blind source {source!r}")
        pos = max(0, min(100, int(position)))
        with self._lock:
            now = self.get_now()
            ttl = self.claim_ttl_min.get(src, 15.0) if ttl_min is None else float(ttl_min)
            self._claims[src] = {
                "position": pos,
                "reason": str(reason or ""),
                "until": now + timedelta(minutes=ttl),
            }
            self._prune(now)
            requests = self._live_requests(now)
            top = blind_arbiter.top(requests)
            winner = blind_arbiter.arbitrate(requests)
            granted = winner is not None and winner.source == src
            moved = False
            if granted:
                moved = self._actuate(winner, trigger=f"{src} request")
            else:
                blocker = blind_arbiter.blocked_by(src, requests)
                who = blocker.source if blocker is not None else (top.source if top else "?")
                self.log(
                    f"{src} asked for {pos}% ({reason}) - outranked by {who}; "
                    f"claims: {blind_arbiter.describe(requests)}"
                )
            self._save_state()
            self._publish(f"{src} request")
            self._arm_republish_at_next_expiry()
            until = self._manual_until_locked()
            return {
                "granted": granted,
                "moved": moved,
                "winner": top.source if top is not None else None,
                "position": winner.position if winner is not None else None,
                "manual_until": until.isoformat() if until else None,
            }

    def withdraw(self, source, reason=""):
        """Remove a source's claim. Never moves the blind - a withdrawal is not a
        command, and the next mover is whoever asks next (keeps the callers' own
        cadence in charge, exactly as before the owner existed)."""
        src = str(source or "").strip().lower()
        with self._lock:
            if self._claims.pop(src, None) is not None:
                self.log(f"{src} withdrew its claim" + (f" ({reason})" if reason else ""))
                self._save_state()
                self._publish(f"{src} withdrew")

    def manual_hold_active(self):
        """True while a human's move is holding automation off this blind."""
        with self._lock:
            return self._manual_until_locked() is not None

    def manual_hold_until(self):
        """When the current manual hold lapses (datetime), or None."""
        with self._lock:
            return self._manual_until_locked()

    def _manual_until_locked(self):
        c = self._claims.get(blind_arbiter.SOURCE_MANUAL)
        if not c:
            return None
        until = c.get("until")
        if until is None:
            return None
        try:
            if self.get_now() >= until:
                return None
        except TypeError:
            return None
        return until

    # ---------- arbitration plumbing ----------
    def _live_requests(self, now):
        reqs = []
        for src, c in self._claims.items():
            until = c.get("until")
            try:
                active = until is None or now < until
            except TypeError:
                active = False
            if active:
                reqs.append(
                    blind_arbiter.BlindRequest(src, c.get("position"), c.get("reason", ""))
                )
        return reqs

    def _prune(self, now):
        for src in list(self._claims):
            until = self._claims[src].get("until")
            try:
                expired = until is not None and now >= until
            except TypeError:
                expired = True
            if expired:
                del self._claims[src]

    def _actuate(self, winner, trigger=""):
        """Issue the write when it is needed; returns True when the motor was told
        to move. "Already there" is judged against the SETTLED reality: while one
        of our own commands is still travelling toward a DIFFERENT target, the
        instantaneous position is transient and must not swallow the new command
        (a wall press seconds after the wake open reads ~95% - close to the close
        target - while the motor is heading for 38)."""
        now = self.get_now()
        in_flight_elsewhere = False
        if self._pending is not None:
            try:
                in_flight_elsewhere = (
                    now <= self._pending["until"]
                    and abs(int(self._pending["target"]) - int(winner.position)) > self.pos_tol
                )
            except (TypeError, ValueError):
                in_flight_elsewhere = False
        cur = cover_util.position(self, self.cover)
        if (not in_flight_elsewhere and cur is not None
                and abs(int(cur) - int(winner.position)) <= self.pos_tol):
            return False  # already there - idempotent, no motor churn
        if self.dry_run:
            self.log(
                f"DRY-RUN would set {self.cover} -> {winner.position}% "
                f"[{winner.source}] ({winner.reason}) <- {trigger}"
            )
            return False
        pos = blind_arbiter.command(
            self, self.cover, winner.position,
            source=winner.source, reason=winner.reason, log_fn=self.log,
        )
        self._cmd_seq += 1
        self._last_cmd = {"position": pos, "source": winner.source, "at": now.isoformat()}
        self._pending = {
            "target": pos,
            "source": winner.source,
            "until": now + timedelta(seconds=self.own_move_window_s),
        }
        try:
            self.run_in(
                self._verify_position, self.verification_delay,
                expected_position=pos, cmd_seq=self._cmd_seq,
                reason=f"{winner.source}: {winner.reason}",
            )
        except Exception:
            pass
        return True

    def _verify_position(self, kwargs):
        """Did the motor arrive? Moved from bedroom_blind_control with the write."""
        if kwargs.get("cmd_seq") != self._cmd_seq:
            return  # superseded by a newer command - that one has its own check
        expected = kwargs.get("expected_position")
        reason = kwargs.get("reason", "")
        actual = cover_util.position(self, self.cover)
        if expected is None:
            return
        if actual is None:
            self.log(
                f"Position verification: {self.cover} has no current_position ({reason})",
                level="WARNING",
            )
            return
        if abs(int(actual) - int(expected)) <= self.verification_tolerance:
            self.log(
                f"Position verified: {self.cover} at {actual}% (expected {expected}%, {reason})",
                level="DEBUG",
            )
            return
        if self.manual_hold_active():
            return  # a human intervened after the command - mismatch is expected
        self.log(
            f"Position mismatch: {self.cover} at {actual}% (expected {expected}%, "
            f"diff={abs(int(actual) - int(expected))}%, {reason}). "
            "This may indicate low battery or a motor issue.",
            level="WARNING",
        )

    # ---------- uncommanded-move detection (the shade's settle machine, generalized) ----------
    def _cover_context_user_id(self):
        try:
            full = self.get_state(self.cover, attribute="all")
            if isinstance(full, dict):
                ctx = full.get("context")
                if isinstance(ctx, dict):
                    return ctx.get("user_id")
        except Exception:
            pass
        return None

    def _on_cover_change(self, entity, attribute, old, new, kwargs):
        try:
            pos = int(float(new))
        except (TypeError, ValueError):
            return
        with self._lock:
            self._ingest_position(pos)

    def _ingest_position(self, pos):
        """Classify one position report. One human move is ONE episode however many
        reports it emits (motor reports ~every 5 s while travelling; the 2026-07-27
        close produced seven) - the settle timer decides when it is over."""
        now = self.get_now()
        ctx = self._cover_context_user_id()
        own_ctx = bool(self.appdaemon_user_id) and ctx == self.appdaemon_user_id
        human_ctx = ctx not in (None, "") and not own_ctx
        pending_live = False
        if self._pending is not None:
            try:
                pending_live = now <= self._pending["until"]
            except TypeError:
                pending_live = False
            if not pending_live:
                self._pending = None  # travel window lapsed; judge what follows on its own

        if self._episode is None:
            if human_ctx:
                kind = "manual"  # a person's user id on the state - dashboard/UI move
            elif pending_live:
                kind = "own"     # our command is travelling
            elif own_ctx:
                kind = "adopt"   # another app's fallback write - ecosystem, not a hand
            else:
                if self._baseline is not None and abs(pos - self._baseline) <= self.pos_tol:
                    return       # jitter around the settled position
                kind = "manual"  # a move nobody here ordered: physical remote / unknown
            self._episode = {"kind": kind, "from": self._baseline, "last": pos}
            if kind == "manual":
                # Hold NOW and persist NOW, so an AppDaemon restart mid-travel still
                # knows a manual move happened (solar shade pattern, 2026-07-27).
                self._set_manual_claim(pos, now, reason="manual move in progress")
                self._save_state()
        else:
            self._episode["last"] = pos
            if human_ctx and self._episode["kind"] != "manual":
                self._episode["kind"] = "manual"
                self._set_manual_claim(pos, now, reason="manual move in progress")
                self._save_state()
            elif self._episode["kind"] == "manual":
                # Push the hold out - it must run from the END of the travel.
                self._set_manual_claim(pos, now, reason="manual move in progress")

        self._safe_cancel_timer(self._settle_handle)
        try:
            self._settle_handle = self.run_in(self._motor_settled, self.manual_settle_s)
        except Exception as e:
            # Never lose the record: finalize immediately rather than dropping it.
            self._settle_handle = None
            self.log(f"settle timer failed ({e}) - finalizing the move now", level="WARNING")
            self._finish_episode(now)

    def _motor_settled(self, kwargs):
        with self._lock:
            self._settle_handle = None
            self._finish_episode(self.get_now())

    def _finish_episode(self, now):
        ep, self._episode = self._episode, None
        if ep is None:
            return
        final = ep["last"]
        if ep["kind"] == "own":
            target = self._pending.get("target") if self._pending else None
            self._pending = None
            if target is not None and abs(final - target) <= self.pos_tol:
                self._baseline = final
                self._save_state()
                self._publish("own move arrived")
                return
            # Our command, but the motor stopped somewhere else: a hand on the
            # physical remote mid-travel (or a stall). The human interpretation is
            # the safe one - automation holds off either way.
            self._finalize_manual(ep, final, now, note=" (own command interrupted)")
            return
        if ep["kind"] == "adopt":
            self._baseline = final
            self._save_state()
            # Worth a line: an AppDaemon-context move this app did not order means
            # some caller's request path failed and its fail-safe direct write ran.
            self.log(f"Adopted an AppDaemon-context move to {final}% (a caller's fallback write)")
            self._publish("adopted an AppDaemon write")
            return
        self._finalize_manual(ep, final, now)

    def _finalize_manual(self, ep, final, now, note=""):
        frm = ep.get("from")
        span = f"{frm}% -> {final}%" if frm is not None else f"to {final}%"
        self._baseline = final
        until = self._set_manual_claim(final, now, reason=f"manual move {span}")
        self._pending = None
        self._save_state()
        self.log(
            f"Manual blind move {span}{note} -> automation holds off "
            f"{self.manual_pause_min:.0f} min (until {until.strftime('%H:%M')})"
        )
        self._publish("manual move")
        self._arm_republish_at_next_expiry()

    def _set_manual_claim(self, pos, now, reason=""):
        until = now + timedelta(minutes=self.manual_pause_min)
        self._claims[blind_arbiter.SOURCE_MANUAL] = {
            "position": int(pos) if pos is not None else None,
            "reason": reason or "manual move",
            "until": until,
        }
        return until

    # ---------- state persistence ----------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                d = json.load(f)
        except Exception:
            return
        claims = d.get("claims")
        if isinstance(claims, dict):
            for src, c in claims.items():
                if str(src) not in blind_arbiter.PRIORITIES or not isinstance(c, dict):
                    continue
                until = self._parse_dt(c.get("until"))
                pos = c.get("position")
                try:
                    pos = int(pos) if pos is not None else None
                except (TypeError, ValueError):
                    pos = None
                self._claims[str(src)] = {
                    "position": pos,
                    "reason": str(c.get("reason") or ""),
                    "until": until,
                }
        base = d.get("baseline")
        if base is not None:
            try:
                self._baseline = int(base)
            except (TypeError, ValueError):
                pass
        lc = d.get("last_cmd")
        if isinstance(lc, dict):
            self._last_cmd = lc
        pending = d.get("pending")
        if isinstance(pending, dict):
            until = self._parse_dt(pending.get("until"))
            try:
                target = int(pending.get("target"))
            except (TypeError, ValueError):
                target = None
            if until is not None and target is not None:
                self._pending = {
                    "target": target,
                    "source": str(pending.get("source") or ""),
                    "until": until,
                }

    def _save_state(self):
        try:
            data = {
                "claims": {
                    src: {
                        "position": c.get("position"),
                        "reason": c.get("reason"),
                        "until": c["until"].isoformat() if c.get("until") else None,
                    }
                    for src, c in self._claims.items()
                },
                "baseline": self._baseline,
                "last_cmd": self._last_cmd,
                "pending": {
                    "target": self._pending["target"],
                    "source": self._pending.get("source"),
                    "until": self._pending["until"].isoformat(),
                } if self._pending else None,
            }
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed ({e}) - continuing in-memory", level="WARNING")

    @staticmethod
    def _parse_dt(raw):
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None

    def _adopt_shade_manual_pause(self):
        """One-time migration: an in-flight manual pause written by
        bedroom_solar_shade before this app existed must survive the deploy that
        introduces it. Adopted as a POSITION-LESS manual veto (we don't know where
        the hand left the blind, only that automation must hold off), which the
        arbiter treats as a block, not a proposal. The shade's file is read, never
        written - its schema stays exactly what its own tests pin."""
        if blind_arbiter.SOURCE_MANUAL in self._claims or not self.shade_state_file:
            return
        try:
            with open(self.shade_state_file) as f:
                d = json.load(f)
            until = self._parse_dt(d.get("override_until"))
        except Exception:
            return
        if until is None:
            return
        try:
            if until <= self.get_now():
                return
        except TypeError:
            return  # naive/aware mismatch - do not guess with a manual hold
        self._claims[blind_arbiter.SOURCE_MANUAL] = {
            "position": None,
            "reason": "manual pause adopted from bedroom_solar_shade",
            "until": until,
        }
        self._save_state()
        self.log(
            f"Adopted bedroom_solar_shade's in-flight manual pause (until "
            f"{until.strftime('%H:%M')})"
        )

    # ---------- status ----------
    def _describe_claims(self):
        try:
            return blind_arbiter.describe(self._live_requests(self.get_now()))
        except Exception:
            return "?"

    def _arm_republish_at_next_expiry(self):
        """The status entity must not keep naming a winner whose claim has lapsed -
        republish when the earliest claim expires (transparency only, never a move)."""
        self._safe_cancel_timer(self._republish_handle)
        self._republish_handle = None
        try:
            now = self.get_now()
            untils = [c["until"] for c in self._claims.values() if c.get("until")]
            if not untils:
                return
            secs = min((u - now).total_seconds() for u in untils)
            self._republish_handle = self.run_in(self._republish, max(1, int(secs) + 1))
        except Exception:
            pass

    def _republish(self, kwargs):
        with self._lock:
            self._republish_handle = None
            self._prune(self.get_now())
            self._save_state()
            self._publish("claim expired")
            self._arm_republish_at_next_expiry()

    def _publish(self, trigger=""):
        """sensor.bedroom_blind_owner - FIXED CONTRACT with the dashboard:

        state: "manual" | "vent" | "wake" | "shade" | "idle"  (idle = no active
        request; the dashboard hides the row on idle/unknown/unavailable).
        attributes:
          position          - number the owner is enforcing (null/omitted at idle;
                              a position-less manual veto reports where the blind
                              sits, since a hold keeps it exactly there)
          manual_pause_until- ISO-8601 timestamp string while a manual pause is
                              active, otherwise "" (EMPTY STRING, not null: AD
                              4.5.13 drops some falsy attrs from set_state - see
                              smart_cooling._publish - and "" survives)
          claims            - list of {"source", "position"} for every ACTIVE
                              NON-winning request, highest rank first, may be []
          reason            - one short human sentence
          friendly_name     - "Bedroom blind owner"
        replace=True so stale attributes never linger. Extra attributes beyond the
        contract (icon, current_position, last_command, trigger) are additive and
        ignored by the dashboard."""
        if not self.status_entity:
            return
        try:
            now = self.get_now()
            requests = self._live_requests(now)
            top = blind_arbiter.top(requests)
            winner = blind_arbiter.arbitrate(requests)
            manual_until = self._manual_until_locked()
            state = top.source if top is not None else "idle"
            if winner is not None:
                position = winner.position
            elif top is not None:
                # Position-less manual veto: the hold keeps the blind where it is.
                position = cover_util.position(self, self.cover)
            else:
                position = None
            claims = [
                {"source": r.source, "position": r.position}
                for r in sorted(requests, key=lambda r: -r.priority)
                if top is not None and r.source != top.source
            ]
            if top is None:
                reason = "No active claims"
            elif top.source == blind_arbiter.SOURCE_MANUAL and manual_until is not None:
                what = top.reason or "manual move"
                reason = f"{what}, holding until {manual_until.strftime('%H:%M')}"
            else:
                reason = top.reason or f"{top.source} holds the blind"
            attrs = {
                "friendly_name": "Bedroom blind owner",
                "icon": "mdi:blinds-horizontal",
                "position": position,
                "manual_pause_until": manual_until.isoformat() if manual_until else "",
                "claims": claims,
                "reason": reason,
                "current_position": cover_util.position(self, self.cover),
                "last_command": self._last_cmd,
                "trigger": trigger,
            }
            if self.dry_run:
                attrs["dry_run"] = True
            self.set_state(self.status_entity, state=state, attributes=attrs, replace=True)
        except Exception as e:
            self.log(f"publish failed: {e}", level="WARNING")

    # ---------- misc ----------
    def _safe_cancel_timer(self, handle):
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
        except Exception:
            pass
