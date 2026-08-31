"""
Mobile Notifier - Centralized mobile app notification handler
Supports sending notifications to specific devices or to people who are home.

Recommended: Target person(s) by name (e.g. "mikkel" or ["mikkel", "kristine"]).
Keep device mapping in this app's device_mapping; avoid putting raw notify.*
service names in other apps' configs.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

class MobileNotifier(hass.Hass):
    """Centralized mobile app notification handler with presence detection."""
    
    def initialize(self):
        """Initialize the MobileNotifier app."""
        self.log("Mobile Notifier initializing...")
        
        # Person entities to track for presence detection
        self.person_entities = self.args.get("person_entities", [
            "person.mikkel",
            "person.kristine",
        ])
        
        # Device mapping: person name -> notification service(s)
        # Format: {"mikkel": ["notify.mobile_app_iphone"], "kristine": ["notify.mobile_app_android"]}
        self.device_mapping = self.args.get("device_mapping", {})

        # Category-scoped home-broadcast audiences: {"category": ["person", ...]}.
        # See _filter_people_for_category for semantics (no entry for a category -> everyone).
        self.category_audience = self.args.get("category_audience", {}) or {}

        # Default notification service for user (for vacuum errors, etc.)
        self.user_notification_service = self.args.get("user_notification_service")
        if not self.user_notification_service:
            self.log("WARNING: user_notification_service not configured. Vacuum error notifications will not work.", level="WARNING")
        
        self.log(f"Mobile Notifier initialized. Tracking {len(self.person_entities)} person(s)", level="INFO")
    
    async def get_people_home(self):
        """Return list of person names who are currently home.
        
        Returns:
            List of person names (e.g., ["mikkel", "kristine"])
        """
        people_home = []
        try:
            for person_entity in self.person_entities:
                try:
                    person_state = await self.get_state(person_entity)
                    # Extract person name from entity (e.g., "person.mikkel" -> "mikkel")
                    person_name = person_entity.replace("person.", "").lower()
                    
                    # Person is home if state is "home" or their zone
                    if person_state == "home":
                        people_home.append(person_name)
                    # Also check if person is in a specific zone (not "not_home")
                    elif person_state and person_state != "not_home" and person_state != "unavailable":
                        # Could be a zone name, treat as home
                        people_home.append(person_name)
                except Exception as e:
                    self.log(f"Error checking person {person_entity}: {e}", level="WARNING")
                    continue
            
            return people_home
        except Exception as e:
            self.log(f"Error getting people home: {e}", level="ERROR")
            return []
    
    async def get_notification_services_for_people(self, people_names):
        """Get notification services for given people.
        
        Args:
            people_names: List of person names (e.g., ["mikkel", "kristine"])
            
        Returns:
            List of notification service names
        """
        services = []
        for person_name in people_names:
            person_devices = self.device_mapping.get(person_name, [])
            if isinstance(person_devices, str):
                person_devices = [person_devices]
            services.extend(person_devices)
        
        # Remove duplicates while preserving order
        seen = set()
        unique_services = []
        for service in services:
            if service not in seen:
                seen.add(service)
                unique_services.append(service)
        
        return unique_services

    @staticmethod
    def _filter_people_for_category(people, category, category_audience):
        """Category-scoped home-broadcasts: when a category has an audience list,
        only those people receive it. No category / unlisted category -> everyone."""
        if not category:
            return people
        audience = (category_audience or {}).get(category)
        if audience is None:
            return people
        return [p for p in people if p in audience]

    async def _resolve_services(self, target, category: str = None):
        """Resolve a `target` (see notify()'s docstring) to a list of notify service
        names. Shared by notify() and clear_notification() so both use identical
        targeting semantics. Returns [] (and logs) when nothing resolves."""
        # Normalize: single person name (in device_mapping) -> list, so all callers work
        if isinstance(target, str) and target not in ("home", "user", "all"):
            if not target.startswith("notify.") and target in self.device_mapping:
                target = [target]

        if target == "user":
            if self.user_notification_service:
                if isinstance(self.user_notification_service, str):
                    return [self.user_notification_service]
                return list(self.user_notification_service)
            self.log("WARNING: user_notification_service not configured, cannot resolve target", level="WARNING")
            return []

        if target == "home":
            people_home = await self.get_people_home()
            people_home_before_category = people_home
            people_home = self._filter_people_for_category(people_home, category, self.category_audience)
            if category and people_home != people_home_before_category:
                filtered_out = [p for p in people_home_before_category if p not in people_home]
                self.log(
                    f"Category '{category}' filtered out {', '.join(filtered_out)} from this home-broadcast",
                    level="DEBUG",
                )
            if not people_home:
                self.log("No one is home, skipping", level="DEBUG")
                return []
            return await self.get_notification_services_for_people(people_home)

        if target == "all":
            all_services = []
            for person_devices in self.device_mapping.values():
                if isinstance(person_devices, str):
                    all_services.append(person_devices)
                else:
                    all_services.extend(person_devices)
            if self.user_notification_service:
                if isinstance(self.user_notification_service, str):
                    all_services.append(self.user_notification_service)
                else:
                    all_services.extend(self.user_notification_service)
            return list(set(all_services))

        if isinstance(target, list):
            return await self.get_notification_services_for_people(target)

        if isinstance(target, str):
            return [target]

        return []

    async def clear_notification(self, tag: str, target: str = "home", category: str = None):
        """Dismiss a previously sent notification on-device by tag.

        Uses the same target resolution as notify(), so pass the identical `target`
        (and `category`, if any) that the original notify() call used, and every
        device that received it will have it dismissed - including devices where
        the recipient never interacted with it.
        """
        try:
            services = await self._resolve_services(target, category)
            if not services:
                return
            for service in services:
                try:
                    service_path = service.replace("notify.", "notify/", 1) if service.startswith("notify.") else service
                    await self.call_service(service_path, message="clear_notification", data={"tag": tag})
                    self.log(f"Cleared notification tag={tag!r} on {service_path}", level="DEBUG")
                except Exception as e:
                    self.log(f"Failed to clear notification tag={tag!r} on {service}: {e}", level="WARNING")
        except Exception as e:
            self.log(f"Error clearing notification tag={tag!r}: {e}", level="ERROR")

    async def notify(self, title: str, message: str, target: str = "home", data: dict = None, category: str = None):
        """Send notification to mobile app(s).

        Target semantics:
            - "home": Send to all people who are home
            - "user": Send to user (default notification service)
            - "all": Send to all configured devices
            - List of person names: Send to specific people (e.g. ["mikkel", "kristine"]).
              For a single person, pass a one-element list (e.g. ["mikkel"]), not a string.
            - String that is a person name in device_mapping: Treated as that person
              (normalized to a one-element list internally).
            - Any other string: Treated as a raw notification service name
              (e.g. "notify.mobile_app_iphone").

        Category scoping (category=...) only restricts target="home" broadcasts (via
        category_audience); every other target is caller-directed and always reaches
        who it names, regardless of category.

        Args:
            title: Notification title
            message: Notification message
            target: Who to send to (see semantics above).
            data: Optional additional data (e.g., {"data": {"importance": "high"}})
            category: Optional category name; scopes target="home" to category_audience[category]
                when that category is listed (see initialize()). Ignored for other targets.
        """
        try:
            notification_data = {
                "title": title,
                "message": message,
            }

            # Add optional data
            # If data contains a "data" key, merge it properly for Home Assistant
            # Home Assistant expects: {"data": {"actions": [...]}}
            if data:
                if "data" in data and isinstance(data["data"], dict):
                    # If data is {"data": {...}}, merge the inner dict into notification_data["data"]
                    if "data" not in notification_data:
                        notification_data["data"] = {}
                    notification_data["data"].update(data["data"])
                else:
                    # Otherwise, merge normally
                    notification_data.update(data)

            # Determine target services
            services = await self._resolve_services(target, category)

            # Check if no services found
            if not services:
                self.log(f"No notification services found for target '{target}'", level="WARNING")
                return

            # Send to all target services
            success_count = 0
            for service in services:
                try:
                    # Convert service name from notify.mobile_app_xxx to notify/mobile_app_xxx format
                    # AppDaemon expects domain/service format, not domain.service
                    # According to HA docs: https://companion.home-assistant.io/docs/notifications/notifications-basic/
                    if service.startswith("notify."):
                        service_path = service.replace("notify.", "notify/", 1)
                    else:
                        service_path = service
                    
                    await self.call_service(service_path, **notification_data)
                    self.log(f"Sent notification to {service_path} (original: {service}): {title}", level="INFO")
                    success_count += 1
                except Exception as e:
                    self.log(f"Failed to send notification to {service}: {e}", level="WARNING")
            
            if success_count == 0:
                self.log(f"Failed to send notification to any service", level="ERROR")
            elif success_count < len(services):
                self.log(f"Sent notification to {success_count}/{len(services)} services", level="WARNING")
                
        except Exception as e:
            self.log(f"Error sending notification: {e}", level="ERROR")

