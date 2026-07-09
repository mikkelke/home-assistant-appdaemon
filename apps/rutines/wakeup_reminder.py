"""
Wakeup Reminder - Sends notifications on Friday/Saturday when Mikkel is home
and in the bedroom to ask if he needs the alarm wakeup routine enabled.

With `in_bed_by_person`, notification is only sent when his Withings in-bed sensor is on.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
from datetime import datetime, timedelta, time


class WakeupReminder(hass.Hass):
    """Reminds Mikkel to enable wakeup routine on Friday/Saturday evenings."""
    
    def initialize(self):
        """Initialize the wakeup reminder app."""
        self.log("Wakeup Reminder initializing...")
        
        # Configuration
        self.person_mikkel = self.args.get("person_mikkel", "person.mikkel")
        self.bedroom_presence_entity = self.args.get(
            "bedroom_presence_entity", "binary_sensor.bedroom_presence_presence"
        )
        self.bedroom_door_entity = self.args.get("bedroom_door_entity", "binary_sensor.bedroom_door_contact")
        self.alarm_enabled_entity = self.args.get("alarm_enabled_entity", "input_boolean.wakeup_bedroom")
        # Per-person Withings: notify each only when *they* are in bed (see in_bed_by_person in yaml)
        raw_map = self.args.get("in_bed_by_person")
        if raw_map and isinstance(raw_map, dict):
            self._in_bed_by_person = {str(k).lower(): v for k, v in raw_map.items()}
            self.in_bed_entities = list(dict.fromkeys(self._in_bed_by_person.values()))
        else:
            self._in_bed_by_person = {}
            # Legacy: flat list - require at least one on for any notification
            self.in_bed_entities = list(self.args.get("in_bed_entities") or [])
        
        # Optional: URL to open when Enable is pressed (e.g., bedroom detail card)
        # Can be a relative URL like "/lovelace/bedroom" or entity URL like "entityId:input_boolean.wakeup_bedroom"
        self.enable_action_url = self.args.get("enable_action_url")
        
        # MobileNotifier app reference
        self.mobile_notifier = self.get_app("MobileNotifier")
        if not self.mobile_notifier:
            self.log("WARNING: MobileNotifier app not found. Notifications will not work.", level="WARNING")
        
        # Notification settings
        self.notification_title = self.args.get("notification_title", "Wakeup Routine Reminder")
        self.notification_message = self.args.get(
            "notification_message", 
            "Do you need the alarm wakeup routine enabled for tomorrow?"
        )
        
        # Track notifications per person per day
        # Format: {date: {"mikkel": notified_time}}
        self._notifications_sent = {}
        
        # Track who has decided (enabled/disabled alarm) - resets daily
        # Format: {date: {"mikkel": decided_time}}
        self._decisions_made = {}
        
        # Track last known home state to detect when someone leaves
        self._last_mikkel_state = None
        
        # Log level
        self.log_level = self.args.get("log_level", "INFO")
        self.set_log_level(self.log_level)
        
        # Schedule daily check at 21:00 (9 PM) for Friday/Saturday
        # This ensures notifications are sent even if presence was already on before 9 PM
        self.run_daily(self._scheduled_evening_check, time(21, 0, 0))
        
        # Listen for bedroom presence changes on Friday/Saturday to trigger check
        # This triggers notification when they arrive home and enter bedroom
        self.listen_state(self._on_bedroom_presence_change, self.bedroom_presence_entity)
        
        # Listen for bedroom door changes - if door closes and presence is on, check if we should notify
        # This handles the case where door closes after entering bedroom
        self.listen_state(self._on_bedroom_door_change, self.bedroom_door_entity)

        for ent in self.in_bed_entities:
            self.listen_state(self._on_in_bed_sensor_change, ent)
        
        # Listen to person state changes to detect when someone leaves home
        # This allows resetting notification status when they leave and come back
        self.listen_state(self._on_mikkel_state_change, self.person_mikkel)
        
        # Listen to alarm state changes to detect when someone has "decided"
        self.listen_state(self._on_alarm_state_change, self.alarm_enabled_entity)
        
        # Listen for test events from Home Assistant
        self.listen_event(self._on_test_event, "wakeup_reminder_test")
        self.listen_event(self._on_reset_event, "wakeup_reminder_reset")
        
        # Listen for actionable notification button presses
        self.listen_event(self._on_notification_action, "mobile_app_notification_action")
        
        self.log("Wakeup Reminder initialized successfully", level="INFO")

    def _anyone_in_bed(self) -> bool:
        for ent in self.in_bed_entities:
            try:
                if self.get_state(ent) == "on":
                    return True
            except Exception:
                pass
        return False

    def _person_in_bed(self, person_name: str) -> bool:
        """True if this person's mapped in-bed sensor is on."""
        if not self._in_bed_by_person:
            return True
        ent = self._in_bed_by_person.get(person_name.lower())
        if not ent:
            return True
        try:
            return self.get_state(ent) == "on"
        except Exception:
            return False

    def _someone_in_bed_for_notify(self) -> bool:
        """At least one in-bed sensor on - used before scheduling checks from presence/door/9PM."""
        if self._in_bed_by_person:
            return any(self._person_in_bed(p) for p in self._in_bed_by_person)
        if self.in_bed_entities:
            return self._anyone_in_bed()
        return True

    def _on_in_bed_sensor_change(self, entity, attribute, old, new, kwargs):
        """Someone lay down - re-check Fri/Sat evening notification eligibility."""
        if new != "on":
            return
        weekday = self.datetime().weekday()
        if weekday not in [4, 5]:
            return
        now = self.datetime()
        if now.hour < 21:
            return
        self.log(f"In-bed sensor on ({entity}) - scheduling notification check", level="INFO")
        self.run_in(self._check_and_notify, 5)
    
    def _scheduled_evening_check(self, kwargs):
        """Scheduled check at 9 PM daily - ensures notifications are sent even if presence was already on."""
        try:
            weekday = self.datetime().weekday()  # 0=Monday, 4=Friday, 5=Saturday
            if weekday not in [4, 5]:  # Not Friday or Saturday
                return

            if not self._someone_in_bed_for_notify():
                self.log(
                    "Scheduled 9 PM check: no one in bed yet - skipping (will notify when someone lies down)",
                    level="DEBUG",
                )
                return
            
            self.log("Scheduled evening check at 9 PM - checking if notification needed", level="INFO")
            # Small delay to allow any state updates to settle
            self.run_in(self._check_and_notify, 5)
        except Exception as e:
            self.log(f"Error in scheduled evening check: {e}", level="ERROR")
    
    def _on_bedroom_presence_change(self, entity, attribute, old, new, kwargs):
        """Handle bedroom presence changes - check if we should notify on Friday/Saturday."""
        # Log all presence changes for debugging
        self.log(f"Bedroom presence changed: {old} -> {new} (at {self.datetime().strftime('%Y-%m-%d %H:%M:%S')})", level="DEBUG")
        
        # Only check if presence just turned on (not off)
        if new != "on":
            return
        
        # If presence was already on, don't trigger (scheduled check handles this)
        if old == "on":
            self.log(f"Presence already on - no state change trigger (scheduled check will handle at 9 PM)", level="DEBUG")
            return
        
        # Presence just turned ON (was off, now on) - this could be:
        # 1. First entry today (will notify if eligible)
        # 2. Re-entry after leaving bedroom (will notify if they dismissed earlier and are still eligible)
        # Clear notification status when re-entering bedroom (allows re-notification after dismissal)
        today = self.datetime().date()
        if today in self._notifications_sent:
            # Clear notification status when bedroom presence turns on
            # This allows re-notification if they dismissed and re-entered
            cleared = []
            for person in ["mikkel"]:
                if person in self._notifications_sent[today]:
                    # Only clear if they haven't decided yet
                    if today not in self._decisions_made or person not in self._decisions_made[today]:
                        del self._notifications_sent[today][person]
                        cleared.append(person)
            if cleared:
                self.log(f"Re-entered bedroom - cleared notification status for {cleared} (can notify again if eligible)", level="INFO")
        
        # Check if it's Friday or Saturday
        weekday = self.datetime().weekday()  # 0=Monday, 4=Friday, 5=Saturday
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        if weekday not in [4, 5]:  # Not Friday or Saturday
            self.log(f"Not Friday or Saturday (today is {weekday_names[weekday]}), skipping", level="DEBUG")
            return
        
        # Check if it's after 21:00 (9 PM) - notifications should only be sent in the evening
        now = self.datetime()
        if now.hour < 21:
            self.log(f"Too early ({now.hour}:{now.minute:02d}) - notifications only sent after 21:00", level="DEBUG")
            return
        
        # Check if door is closed before triggering (door closed = "off")
        bedroom_door = self.get_state(self.bedroom_door_entity)
        if bedroom_door != "off":
            self.log(f"Presence turned on but door is {bedroom_door} (not closed) - will check when door closes", level="DEBUG")
            return

        if not self._someone_in_bed_for_notify():
            self.log(
                "Presence on and door closed but no one in bed yet - skipping (will notify when someone lies down or at 9 PM if in bed)",
                level="DEBUG",
            )
            return
        
        self.log(f"State change trigger: bedroom presence turned on at {now.strftime('%H:%M:%S')} on {weekday_names[weekday]} with door closed - scheduling notification check", level="INFO")
        # Small delay to allow presence to settle and person state to update
        self.run_in(self._check_and_notify, 5)
    
    def _on_bedroom_door_change(self, entity, attribute, old, new, kwargs):
        """Handle bedroom door changes - if door closes and presence is on, check if we should notify."""
        # Log all door changes for debugging
        self.log(f"Bedroom door changed: {old} -> {new} (at {self.datetime().strftime('%Y-%m-%d %H:%M:%S')})", level="DEBUG")
        
        # Only check if door just closed (was open "on", now closed "off")
        if new != "off" or old != "on":
            return
        
        # Check if it's Friday or Saturday
        weekday = self.datetime().weekday()  # 0=Monday, 4=Friday, 5=Saturday
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        if weekday not in [4, 5]:  # Not Friday or Saturday
            self.log(f"Not Friday or Saturday (today is {weekday_names[weekday]}), skipping", level="DEBUG")
            return
        
        # Check if it's after 21:00 (9 PM) - notifications should only be sent in the evening
        now = self.datetime()
        if now.hour < 21:
            self.log(f"Too early ({now.hour}:{now.minute:02d}) - notifications only sent after 21:00", level="DEBUG")
            return
        
        # Check if bedroom has presence
        bedroom_presence = self.get_state(self.bedroom_presence_entity)
        if bedroom_presence != "on":
            self.log(f"Door closed but no presence in bedroom ({bedroom_presence}), skipping", level="DEBUG")
            return

        if not self._someone_in_bed_for_notify():
            self.log(
                "Door closed with presence but no one in bed yet - skipping (in-bed sensor will trigger when ready)",
                level="DEBUG",
            )
            return
        
        self.log(f"Door closed trigger: bedroom door closed at {now.strftime('%H:%M:%S')} on {weekday_names[weekday]} with presence - scheduling notification check", level="INFO")
        # Small delay to allow state to settle
        self.run_in(self._check_and_notify, 5)
    
    def _on_mikkel_state_change(self, entity, attribute, old, new, kwargs):
        """Handle Mikkel's state changes - reset notification status when he leaves home."""
        self._handle_person_state_change("mikkel", old, new)
        self._last_mikkel_state = new
    
    def _handle_person_state_change(self, person_name, old, new):
        """Handle person state change - reset notification status if they leave home."""
        try:
            # Only process on Friday/Saturday
            weekday = self.datetime().weekday()
            if weekday not in [4, 5]:
                return
            
            # Check if person left home (was home, now not_home)
            was_home = old and old != "not_home" and old != "unavailable"
            is_not_home = new == "not_home" or new == "unavailable"
            
            if was_home and is_not_home:
                today = self.datetime().date()
                # Clear notification status for this person (so they can get notified again when they come back)
                if today in self._notifications_sent and person_name in self._notifications_sent[today]:
                    # Only clear if no decision has been made
                    if today not in self._decisions_made or person_name not in self._decisions_made[today]:
                        del self._notifications_sent[today][person_name]
                        self.log(f"{person_name} left home - cleared notification status (can notify again when they return)", level="INFO")
                    else:
                        self.log(f"{person_name} left home but has already decided - keeping notification status", level="DEBUG")
                        
        except Exception as e:
            self.log(f"Error handling {person_name} state change: {e}", level="WARNING")
    
    def _check_and_notify(self, kwargs=None):
        """Check conditions and send notification if needed."""
        try:
            # Check if it's Friday or Saturday
            weekday = self.datetime().weekday()  # 0=Monday, 4=Friday, 5=Saturday
            if weekday not in [4, 5]:
                self.log("Not Friday or Saturday, skipping check", level="DEBUG")
                return
            
            today = self.datetime().date()
            now = self.datetime()
            
            # Check if it's after 21:00 (9 PM) - notifications should only be sent in the evening
            if now.hour < 21:
                self.log(f"Too early ({now.hour}:{now.minute:02d}) - notifications only sent after 21:00", level="DEBUG")
                return
            
            # Clean up old tracking data (older than 2 days)
            self._cleanup_old_tracking_data(today)
            
            # Check if Mikkel is home
            mikkel_home = self._is_person_home(self.person_mikkel)
            
            if not mikkel_home:
                self.log("Mikkel is not home, skipping", level="DEBUG")
                return
            
            # Check if bedroom has presence
            bedroom_presence = self.get_state(self.bedroom_presence_entity)
            if bedroom_presence != "on":
                self.log(f"Bedroom presence: {bedroom_presence} - no presence in bedroom, skipping", level="DEBUG")
                return

            if self._in_bed_by_person:
                if not any(self._person_in_bed(p) for p in self._in_bed_by_person):
                    self.log(
                        "Per-person in-bed: no one is in their bed - skipping",
                        level="DEBUG",
                    )
                    return
            elif self.in_bed_entities and not self._anyone_in_bed():
                self.log("In-bed sensors configured but no one in bed - skipping", level="DEBUG")
                return
            
            # Check if bedroom door is closed (door closed = "off", door open = "on")
            bedroom_door = self.get_state(self.bedroom_door_entity)
            if bedroom_door != "off":
                self.log(f"Bedroom door: {bedroom_door} - door is not closed, skipping (door must be closed for notification)", level="DEBUG")
                return
            
            # Determine who should receive notification
            people_to_notify = []
            
            if self._person_in_bed("mikkel") and self._should_notify_person("mikkel", today, now):
                people_to_notify.append("mikkel")
            
            if not people_to_notify:
                self.log("No one eligible for notification", level="DEBUG")
                return
            
            # Send notification to eligible people
            self._send_notification(people_to_notify)
            
            # Track that we sent notifications
            if today not in self._notifications_sent:
                self._notifications_sent[today] = {}
            for person in people_to_notify:
                self._notifications_sent[today][person] = now
            
        except Exception as e:
            self.log(f"Error in _check_and_notify: {e}", level="ERROR")
    
    def _should_notify_person(self, person_name, today, now):
        """Determine if a person should be notified."""
        # Check if this person has already decided
        if today in self._decisions_made and person_name in self._decisions_made[today]:
            self.log(f"{person_name} has already decided today, skipping", level="DEBUG")
            return False
        
        # Check if already notified today (but allow if they left and came back - handled by state change)
        if today in self._notifications_sent and person_name in self._notifications_sent[today]:
            # Check if they've already decided - if not, they might have dismissed it
            # Allow re-notification if they left and came back (notification status was cleared)
            # But if they're still here and were notified, don't spam
            self.log(f"{person_name} already notified today (may have dismissed) - will check if eligible", level="DEBUG")
            # If no decision made, we'll allow notification (they might have dismissed it)
            # The state change handler clears notification status when they leave, so if
            # we get here and they're still notified, they're still home and already got it
            # Only allow if they left and came back (notification was cleared but we're checking again)
            # Actually, if notification status exists, they haven't left, so don't notify again
            return False
        
        return True
    
    def _cleanup_old_tracking_data(self, today):
        """Remove tracking data older than 2 days."""
        try:
            cutoff_date = today - timedelta(days=2)
            dates_to_remove = [date for date in self._notifications_sent.keys() if date < cutoff_date]
            for date in dates_to_remove:
                del self._notifications_sent[date]
            
            dates_to_remove = [date for date in self._decisions_made.keys() if date < cutoff_date]
            for date in dates_to_remove:
                del self._decisions_made[date]
        except Exception as e:
            self.log(f"Error cleaning up tracking data: {e}", level="WARNING")
    
    def _is_person_home(self, person_entity):
        """Check if a person is home.
        
        Returns True if person state is 'home' or in a zone (not 'not_home').
        """
        try:
            person_state = self.get_state(person_entity)
            if person_state == "home":
                return True
            # Also check if person is in a specific zone (not "not_home")
            if person_state and person_state != "not_home" and person_state != "unavailable":
                return True
            return False
        except Exception as e:
            self.log(f"Error checking person {person_entity}: {e}", level="WARNING")
            return False
    
    def _on_alarm_state_change(self, entity, attribute, old, new, kwargs):
        """Handle alarm state changes to detect when someone has decided."""
        try:
            # Only track changes on Friday/Saturday
            weekday = self.datetime().weekday()
            if weekday not in [4, 5]:
                return
            
            today = self.datetime().date()
            now = self.datetime()
            
            # If alarm state changed and we've sent notifications today, mark decision
            if today in self._notifications_sent:
                if today not in self._decisions_made:
                    self._decisions_made[today] = {}
                
                if self._is_person_home(self.person_mikkel):
                    if "mikkel" not in self._decisions_made[today]:
                        self._decisions_made[today]["mikkel"] = now
                        self.log(f"Mikkel decided (alarm changed to {new})", level="INFO")
                    
        except Exception as e:
            self.log(f"Error tracking alarm decision: {e}", level="WARNING")
    
    def _send_notification(self, target_people):
        """Send notification to specified people via MobileNotifier.
        
        Args:
            target_people: List of person names to notify (e.g., ["mikkel"])
        """
        try:
            if not target_people:
                self.log("No target people specified for notification", level="WARNING")
                return
            
            if not self.mobile_notifier:
                self.log("MobileNotifier not available - cannot send notification", level="ERROR")
                return
            
            if not hasattr(self.mobile_notifier, 'notify'):
                self.log("MobileNotifier.notify method not available - cannot send notification", level="ERROR")
                return
            
            # Use run_in to schedule async call
            self.run_in(self._send_via_mobile_notifier_async, 0, target=target_people)
                
        except Exception as e:
            self.log(f"Error sending notification: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
    
    async def _send_via_mobile_notifier_async(self, kwargs):
        """Send notification via MobileNotifier app (async wrapper for run_in)."""
        try:
            target = kwargs.get("target", [])
            if not target:
                self.log("No target specified for notification", level="WARNING")
                return
                
            if not self.mobile_notifier:
                self.log("MobileNotifier not available - cannot send notification", level="ERROR")
                return
            
            self.log(f"Calling MobileNotifier.notify with target={target}, title='{self.notification_title}', message='{self.notification_message}'", level="INFO")
            
            # Add actionable buttons to the notification
            # According to HA docs: https://companion.home-assistant.io/docs/notifications/actionable-notifications/
            # iOS: Buttons appear when you long-press (3D Touch) the notification
            # Android: Buttons appear directly on the notification
            actions = [
                {
                    "action": "WAKEUP_ENABLE",
                    "title": "Enable"
                },
                {
                    "action": "WAKEUP_DISABLE",
                    "title": "Disable"
                },
                {
                    "action": "WAKEUP_LATER",
                    "title": "Later"
                }
            ]
            
            # Add URL to Enable button - this is what the user wants
            # The uri field on action buttons should open the app when the button is pressed
            # We'll use relative path which should work better than full URLs
            # Note: iOS may require long-press on notification to see buttons
            if self.enable_action_url:
                # Use relative path - this should open in the HA app
                # Format: "/dashboard-rooftop/0#room=bedroom"
                actions[0]["uri"] = self.enable_action_url
                self.log(f"Added URI to Enable button: {self.enable_action_url}", level="INFO")
                self.log("Enable button should now open the dashboard when pressed", level="INFO")
                self.log("Note: On iOS, long-press the notification to see action buttons", level="INFO")
            
            # Pass the data structure correctly - MobileNotifier merges data into notification_data
            # Home Assistant expects: {"data": {"actions": [...]}}
            notification_data = {
                "data": {
                    "actions": actions
                }
            }
            
            # Add URL to notification data so tapping the notification opens the same link as the button
            # This provides a backup way to open the dashboard
            if self.enable_action_url:
                # Android uses clickAction, iOS uses url
                notification_data["data"]["clickAction"] = self.enable_action_url  # Android
                notification_data["data"]["url"] = self.enable_action_url  # iOS
                self.log(f"Added clickAction/url to notification: {self.enable_action_url}", level="INFO")
                self.log("Tapping the notification will also open the dashboard", level="INFO")
            
            await self.mobile_notifier.notify(
                title=self.notification_title,
                message=self.notification_message,
                target=target,
                data=notification_data  # Pass the full structure with "data" key
            )
            self.log(f"MobileNotifier.notify completed for target {target}", level="INFO")
            
        except Exception as e:
            self.log(f"Error sending via MobileNotifier: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
    
    def _on_test_event(self, event_name, data, kwargs):
        """Handle test event from Home Assistant."""
        self.log("Test event received from Home Assistant", level="INFO")
        self.test_notification()
    
    def _on_reset_event(self, event_name, data, kwargs):
        """Handle reset event from Home Assistant."""
        self.log("Reset event received from Home Assistant", level="INFO")
        self.test_reset_tracking()
    
    def _on_notification_action(self, event_name, data, kwargs):
        """Handle actionable notification button presses.
        
        When a user taps a button in the notification, Home Assistant fires
        mobile_app_notification_action event with the action identifier.
        """
        try:
            self.log("=" * 60, level="INFO")
            self.log("NOTIFICATION ACTION RECEIVED", level="INFO")
            self.log(f"Event name: {event_name}", level="INFO")
            self.log(f"Event data: {data}", level="INFO")
            self.log(f"Event kwargs: {kwargs}", level="INFO")
            self.log("=" * 60, level="INFO")
            
            # Extract action - the event data structure may vary
            # Try different possible locations for the action
            action = None
            if isinstance(data, dict):
                action = data.get("action") or data.get("action_id")
            elif isinstance(kwargs, dict):
                action = kwargs.get("action") or kwargs.get("action_id")
            
            # Also check if data is nested
            if not action and isinstance(data, dict) and "data" in data:
                action = data["data"].get("action") or data["data"].get("action_id")
            
            device_id = None
            if isinstance(data, dict):
                device_id = data.get("device_id") or data.get("device")
            elif isinstance(kwargs, dict):
                device_id = kwargs.get("device_id") or kwargs.get("device")
            
            self.log(f"Extracted action: {action}, device_id: {device_id}", level="INFO")
            
            if not action:
                self.log(f"ERROR: Could not extract action from event. Data: {data}, Kwargs: {kwargs}", level="ERROR")
                return
            
            if action == "WAKEUP_ENABLE":
                self.log("Processing WAKEUP_ENABLE action", level="INFO")
                self._handle_enable_action(device_id)
            elif action == "WAKEUP_DISABLE":
                self.log("Processing WAKEUP_DISABLE action", level="INFO")
                self._handle_disable_action(device_id)
            elif action == "WAKEUP_LATER":
                self.log("Processing WAKEUP_LATER action", level="INFO")
                self._handle_later_action(device_id)
            else:
                self.log(f"Unknown action: {action}. Expected: WAKEUP_ENABLE, WAKEUP_DISABLE, or WAKEUP_LATER", level="WARNING")
                
        except Exception as e:
            self.log(f"Error handling notification action: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
    
    def _handle_enable_action(self, device_id):
        """Handle enable action - turn on the alarm."""
        try:
            self.log("=" * 60, level="INFO")
            self.log("HANDLING ENABLE ACTION", level="INFO")
            self.log(f"Device ID: {device_id}", level="INFO")
            self.log(f"Alarm entity: {self.alarm_enabled_entity}", level="INFO")
            
            today = self.datetime().date()
            now = self.datetime()
            
            # Get current alarm state before change
            alarm_state_before = self.get_state(self.alarm_enabled_entity)
            self.log(f"Alarm state before: {alarm_state_before}", level="INFO")
            
            # Determine who pressed the button based on device_id
            person_name = self._get_person_from_device(device_id)
            
            if not person_name:
                if self._is_person_home(self.person_mikkel):
                    person_name = "mikkel"
                else:
                    person_name = "unknown"
                    self.log("Could not determine who pressed the button, using 'unknown'", level="WARNING")
            
            self.log(f"Determined person: {person_name}", level="INFO")
            
            # Enable the alarm
            # For input_boolean entities, use call_service
            self.log(f"Calling input_boolean.turn_on service for {self.alarm_enabled_entity}", level="INFO")
            try:
                self.call_service("input_boolean/turn_on", entity_id=self.alarm_enabled_entity)
                self.log(f"Service call successful", level="INFO")
            except Exception as e:
                self.log(f"Error calling input_boolean.turn_on: {e}, trying set_state instead", level="WARNING")
                import traceback
                self.log(traceback.format_exc(), level="WARNING")
                # Fallback to set_state if call_service fails
                try:
                    self.set_state(self.alarm_enabled_entity, state="on")
                    self.log(f"set_state successful", level="INFO")
                except Exception as e2:
                    self.log(f"Error with set_state: {e2}", level="ERROR")
                    import traceback
                    self.log(traceback.format_exc(), level="ERROR")
                    return
            
            # Wait a moment for state to update
            self.run_in(self._verify_enable_action, 1, person_name=person_name, device_id=device_id)
            
            # Try to open the dashboard using command_webview
            # This sends a command to the mobile app to navigate to the dashboard
            # Note: command_webview may not work reliably, but we'll try it
            if self.enable_action_url:
                self.run_in(self._open_dashboard_via_command, 0.5, device_id=device_id, person_name=person_name)
            
        except Exception as e:
            self.log(f"Error handling enable action: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
    
    def _open_dashboard_via_command(self, kwargs):
        """Open dashboard using command_webview notification command."""
        try:
            device_id = kwargs.get("device_id")
            person_name = kwargs.get("person_name", "unknown")
            
            # Determine which notification service to use based on device_id
            target = None
            if self.mobile_notifier and hasattr(self.mobile_notifier, 'device_mapping'):
                # Try to find the person from device_id
                person = self._get_person_from_device(device_id)
                if person:
                    target = [person]
            
            if not target and self._is_person_home(self.person_mikkel):
                target = ["mikkel"]
            
            if not target:
                self.log("Could not determine target for dashboard command", level="WARNING")
                return
            
            self.log(f"Sending command_webview to {target} to open: {self.enable_action_url}", level="INFO")
            
            # Use run_in to call async function
            self.run_in(
                self._open_dashboard_via_command_async,
                0,
                target=target,
                url=self.enable_action_url
            )
            
        except Exception as e:
            self.log(f"Error opening dashboard via command: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
    
    async def _open_dashboard_via_command_async(self, kwargs):
        """Async wrapper for sending command_webview."""
        try:
            target = kwargs.get("target")
            url = kwargs.get("url")
            
            if not self.mobile_notifier:
                self.log("MobileNotifier not available for dashboard command", level="WARNING")
                return
            
            # Send command_webview - this is a special notification command that opens the dashboard
            # According to HA docs: message="command_webview", data.command="/path/to/dashboard"
            # The message must be exactly "command_webview" and the path goes in data.command
            # Note: command_webview might not preserve hash fragments, so we'll try URL encoding
            # Format: "/dashboard-rooftop/0#room=bedroom"
            # If hash doesn't work, the React app might need to read it differently
            
            # Ensure the hash is properly included in the URL
            # The React app reads from window.location.hash, so the hash should be preserved
            import urllib.parse
            # URL encode the hash part to ensure it's preserved
            if '#' in url:
                path, hash_part = url.split('#', 1)
                # Don't encode the hash - it should be passed as-is
                # command_webview should preserve the full URL including hash
                full_url = url
            else:
                full_url = url
            
            notification_data = {
                "data": {
                    "command": full_url  # The full path with hash (e.g., "/dashboard-rooftop/0#room=bedroom")
                }
            }
            
            self.log(f"Sending command_webview with command: {full_url}", level="INFO")
            self.log(f"Full URL being sent: {full_url}", level="INFO")
            
            # Send as a notification with message="command_webview"
            # The mobile app will recognize this as a command, not a regular notification
            await self.mobile_notifier.notify(
                title="",  # Empty title for command
                message="command_webview",  # Must be exactly "command_webview"
                target=target,
                data=notification_data
            )
            
            self.log(f"command_webview sent successfully with path: {full_url}", level="INFO")
            
            self.log(f"Sent command_webview to open: {url}", level="INFO")
            
        except Exception as e:
            self.log(f"Error in async dashboard command: {e}", level="ERROR")
    
    def _verify_enable_action(self, kwargs):
        """Verify the enable action was successful."""
        try:
            person_name = kwargs.get("person_name", "unknown")
            device_id = kwargs.get("device_id")
            
            # Verify the alarm was actually enabled
            alarm_state = self.get_state(self.alarm_enabled_entity)
            self.log(f"Alarm state after enable: {alarm_state}", level="INFO")
            
            if alarm_state == "on":
                self.log(f"SUCCESS: Alarm enabled via notification action by {person_name} (device_id: {device_id})", level="INFO")
            else:
                self.log(f"WARNING: Alarm state is {alarm_state}, expected 'on'", level="WARNING")
            
            today = self.datetime().date()
            now = self.datetime()
            
            # Mark decision
            if today not in self._decisions_made:
                self._decisions_made[today] = {}
            
            if person_name in ("mikkel", "unknown"):
                self._decisions_made[today]["mikkel"] = now
                self.log(f"Marked {person_name} as decided (enabled alarm)", level="INFO")
            
            self.log("=" * 60, level="INFO")
            
        except Exception as e:
            self.log(f"Error verifying enable action: {e}", level="ERROR")
    
    def _handle_disable_action(self, device_id):
        """Handle disable action - turn off the alarm."""
        try:
            self.log("=" * 60, level="INFO")
            self.log("HANDLING DISABLE ACTION", level="INFO")
            self.log(f"Device ID: {device_id}", level="INFO")
            self.log(f"Alarm entity: {self.alarm_enabled_entity}", level="INFO")
            
            today = self.datetime().date()
            now = self.datetime()
            
            # Get current alarm state before change
            alarm_state_before = self.get_state(self.alarm_enabled_entity)
            self.log(f"Alarm state before: {alarm_state_before}", level="INFO")
            
            # Determine who pressed the button based on device_id
            person_name = self._get_person_from_device(device_id)
            
            if not person_name:
                if self._is_person_home(self.person_mikkel):
                    person_name = "mikkel"
                else:
                    person_name = "unknown"
                    self.log("Could not determine who pressed the button, using 'unknown'", level="WARNING")
            
            self.log(f"Determined person: {person_name}", level="INFO")
            
            # Disable the alarm
            # For input_boolean entities, use call_service
            self.log(f"Calling input_boolean.turn_off service for {self.alarm_enabled_entity}", level="INFO")
            try:
                self.call_service("input_boolean/turn_off", entity_id=self.alarm_enabled_entity)
                self.log(f"Service call successful", level="INFO")
            except Exception as e:
                self.log(f"Error calling input_boolean.turn_off: {e}, trying set_state instead", level="WARNING")
                import traceback
                self.log(traceback.format_exc(), level="WARNING")
                # Fallback to set_state if call_service fails
                try:
                    self.set_state(self.alarm_enabled_entity, state="off")
                    self.log(f"set_state successful", level="INFO")
                except Exception as e2:
                    self.log(f"Error with set_state: {e2}", level="ERROR")
                    import traceback
                    self.log(traceback.format_exc(), level="ERROR")
                    return
            
            # Wait a moment for state to update
            self.run_in(self._verify_disable_action, 1, person_name=person_name, device_id=device_id)
            
        except Exception as e:
            self.log(f"Error handling disable action: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
    
    def _verify_disable_action(self, kwargs):
        """Verify the disable action was successful."""
        try:
            person_name = kwargs.get("person_name", "unknown")
            device_id = kwargs.get("device_id")
            
            # Verify the alarm was actually disabled
            alarm_state = self.get_state(self.alarm_enabled_entity)
            self.log(f"Alarm state after disable: {alarm_state}", level="INFO")
            
            if alarm_state == "off":
                self.log(f"SUCCESS: Alarm disabled via notification action by {person_name} (device_id: {device_id})", level="INFO")
            else:
                self.log(f"WARNING: Alarm state is {alarm_state}, expected 'off'", level="WARNING")
            
            today = self.datetime().date()
            now = self.datetime()
            
            # Mark decision
            if today not in self._decisions_made:
                self._decisions_made[today] = {}
            
            if person_name in ("mikkel", "unknown"):
                self._decisions_made[today]["mikkel"] = now
                self.log(f"Marked {person_name} as decided (disabled alarm)", level="INFO")
            
            self.log("=" * 60, level="INFO")
            
        except Exception as e:
            self.log(f"Error verifying disable action: {e}", level="ERROR")
    
    def _get_person_from_device(self, device_id):
        """Try to determine which person owns a device based on device_id.
        
        Returns person name ("mikkel") or None if unknown.
        """
        try:
            if not self.mobile_notifier or not hasattr(self.mobile_notifier, 'device_mapping'):
                return None
            
            device_mapping = self.mobile_notifier.device_mapping
            
            # Check each person's devices
            for person_name, devices in device_mapping.items():
                if isinstance(devices, str):
                    devices = [devices]
                
                for device_service in devices:
                    # Extract device identifier from service name
                    # notify.mobile_app_<device_id> -> <device_id>
                    if device_service.startswith("notify.mobile_app_"):
                        service_device_id = device_service.replace("notify.mobile_app_", "")
                        # Compare device IDs (they might not match exactly, so we'll use a simpler approach)
                        # Actually, device_id in the event might be different format
                        # Let's check if the device_id appears in the service name
                        if device_id and device_id.lower() in service_device_id.lower():
                            return person_name
            
            return None
        except Exception as e:
            self.log(f"Error getting person from device: {e}", level="WARNING")
            return None
    
    def _handle_later_action(self, device_id):
        """Handle later action - user dismissed notification to decide later.
        
        This does NOT mark them as decided, so they can still receive
        notifications if they leave and come back.
        """
        try:
            person_name = self._get_person_from_device(device_id)
            
            if not person_name:
                person_name = "mikkel" if self._is_person_home(self.person_mikkel) else "unknown"
            
            self.log(f"Notification dismissed (Later) by {person_name} - no decision made, can notify again if they leave and return", level="INFO")
            
            # Note: We do NOT mark them as decided, so:
            # - They won't get spammed while still in bedroom (notification status already set)
            # - If they leave and come back, notification status will be cleared and they can get notified again
            
        except Exception as e:
            self.log(f"Error handling later action: {e}", level="ERROR")
    
    def test_notification(self, kwargs=None):
        """Test method to manually trigger notification check.
        
        This method automatically resets tracking data before running the test,
        so you can test the same scenario multiple times without manual reset.
        
        Usage from AppDaemon console or service call:
        - self.call_service("appdaemon/execute", entity_id="WakeupReminder", function="test_notification")
        """
        self.log("=" * 60, level="INFO")
        self.log("TEST: Manual notification test triggered", level="INFO")
        self.log("=" * 60, level="INFO")
        
        try:
            # Automatically reset tracking data for today before testing
            today = self.datetime().date()
            if today in self._notifications_sent:
                del self._notifications_sent[today]
                self.log(f"Auto-reset: Cleared notifications sent for {today}", level="INFO")
            
            if today in self._decisions_made:
                del self._decisions_made[today]
                self.log(f"Auto-reset: Cleared decisions made for {today}", level="INFO")
            
            now = self.datetime()
            weekday = now.weekday()
            weekday_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][weekday]
            
            self.log(f"Current date: {today} ({weekday_name})", level="INFO")
            self.log(f"Current time: {now.strftime('%H:%M:%S')}", level="INFO")
            
            # Check if it's Friday or Saturday
            if weekday not in [4, 5]:
                self.log(f"WARNING: Today is {weekday_name}, not Friday or Saturday. Logic normally only works on Fri/Sat.", level="WARNING")
                self.log("Continuing test anyway...", level="INFO")
            
            # Get current states
            mikkel_home = self._is_person_home(self.person_mikkel)
            bedroom_presence = self.get_state(self.bedroom_presence_entity)
            alarm_enabled = self.get_state(self.alarm_enabled_entity)
            
            self.log("", level="INFO")
            self.log("Current States:", level="INFO")
            self.log(f"  Mikkel home: {mikkel_home}", level="INFO")
            self.log(f"  Bedroom presence: {bedroom_presence}", level="INFO")
            self.log(f"  Alarm enabled: {alarm_enabled}", level="INFO")
            
            # Show tracking data
            self.log("", level="INFO")
            self.log("Tracking Data:", level="INFO")
            if today in self._notifications_sent:
                self.log(f"  Notifications sent today: {list(self._notifications_sent[today].keys())}", level="INFO")
            else:
                self.log("  Notifications sent today: None", level="INFO")
            
            if today in self._decisions_made:
                self.log(f"  Decisions made today: {list(self._decisions_made[today].keys())}", level="INFO")
            else:
                self.log("  Decisions made today: None", level="INFO")
            
            # Check eligibility
            self.log("", level="INFO")
            self.log("Eligibility Check:", level="INFO")
            
            people_to_notify = []
            if mikkel_home:
                eligible = self._should_notify_person("mikkel", today, now)
                self.log(f"  Mikkel eligible: {eligible}", level="INFO")
                if eligible:
                    people_to_notify.append("mikkel")
            
            # Send notification if eligible
            self.log("", level="INFO")
            if people_to_notify:
                self.log(f"Sending test notification to: {people_to_notify}", level="INFO")
                self._send_notification(people_to_notify)
                
                # Track that we sent notifications
                if today not in self._notifications_sent:
                    self._notifications_sent[today] = {}
                for person in people_to_notify:
                    self._notifications_sent[today][person] = now
                    self.log(f"  Marked {person} as notified at {now.strftime('%H:%M:%S')}", level="INFO")
            else:
                self.log("No one eligible for notification", level="INFO")
            
            self.log("", level="INFO")
            self.log("=" * 60, level="INFO")
            self.log("TEST: Complete", level="INFO")
            self.log("=" * 60, level="INFO")
            
        except Exception as e:
            self.log(f"Error in test_notification: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
    
    def test_reset_tracking(self, kwargs=None):
        """Test method to reset tracking data for testing.
        
        This clears all notification and decision tracking for today.
        Useful for testing the same scenario multiple times.
        """
        today = self.datetime().date()
        
        if today in self._notifications_sent:
            del self._notifications_sent[today]
            self.log(f"Cleared notifications sent for {today}", level="INFO")
        
        if today in self._decisions_made:
            del self._decisions_made[today]
            self.log(f"Cleared decisions made for {today}", level="INFO")
        
        self.log("Tracking data reset complete", level="INFO")

