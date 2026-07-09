import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import datetime

class Rober2Schedule(hass.Hass):
    def initialize(self):
        # Entities to track last time the schedule was applied
        # Prefer HA helper (restored on HA restart); fallback to synthetic sensor
        self.LAST_RUN_INPUT = "input_text.rober2_schedule_last_run"
        self.LAST_RUN_SENSOR = "sensor.rober2_schedule_last_run"

        # Set up daily schedule update at midnight
        self.run_daily(self.update_schedule, datetime.time(0, 0, 0))

        # On startup, only apply if not already applied today
        self.run_in(self.update_schedule_if_needed, 5)
        
    def update_schedule(self, kwargs):
        # Turn on daily rooms
        daily_rooms = [
            "hallway",
            "kitchen",
            "kitchen_2",
            "living_room",
            "dining_room",
        ]
        for room_name in daily_rooms:
            try:
                self.call_service("input_boolean/turn_on", entity_id=f"input_boolean.rober2_clean_{room_name}")
            except Exception as e:
                self.log(f"Failed to turn on daily room flag for {room_name}: {e}", level="WARNING")

        # Record last run date so we don't re-apply on restarts
        try:
            today = self.get_today_date()
            self.set_last_run_date(today)
        except Exception as e:
            self.log(f"Failed to set last run date: {e}", level="WARNING")

    def update_schedule_if_needed(self, kwargs):
        try:
            today = self.get_today_date()
            last_run = self.get_last_run_date()

            if last_run == today:
                self.log(
                    f"Schedule already applied today ({today}); skipping startup re-apply",
                    level="INFO",
                )
                return

            self.log(
                "Applying daily schedule on startup (not yet applied today)",
                level="INFO",
            )
            self.update_schedule(kwargs)
        except Exception as e:
            self.log(f"Error in update_schedule_if_needed: {e}", level="ERROR")

    def get_today_date(self):
        return datetime.datetime.now().strftime("%Y-%m-%d") 

    def get_last_run_date(self):
        """Return last run date from input_text (preferred) or fallback sensor."""
        try:
            # Prefer HA helper if present
            input_val = self.get_state(self.LAST_RUN_INPUT)
            if input_val not in [None, "unknown", "unavailable", ""]:
                return input_val
        except Exception:
            pass

        try:
            # Fallback synthetic sensor
            sensor_val = self.get_state(self.LAST_RUN_SENSOR)
            if sensor_val not in [None, "unknown", "unavailable", ""]:
                return sensor_val
        except Exception:
            pass

        return None

    def set_last_run_date(self, date_str: str):
        """Persist last run date, preferring input_text helper."""
        # Try input_text first
        try:
            current = self.get_state(self.LAST_RUN_INPUT)
            if current is not None:  # helper exists
                self.call_service(
                    "input_text/set_value",
                    entity_id=self.LAST_RUN_INPUT,
                    value=date_str,
                )
                return
        except Exception:
            pass

        # Fallback to synthetic sensor state
        self.set_state(self.LAST_RUN_SENSOR, state=date_str)