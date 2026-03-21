"""
calendar_reader.py

Reads Google Calendar for the upcoming planning week (Fri–Thu) and returns
structured day constraints for the planner.

--- FIRST-TIME SETUP ---

1. Create a Google Cloud project and enable the Calendar API:
   https://console.cloud.google.com/

   a. Create a new project (or use an existing one)
   b. Go to "APIs & Services" > "Enable APIs and Services"
   c. Search for "Google Calendar API" and enable it

2. Create OAuth 2.0 credentials:
   a. Go to "APIs & Services" > "Credentials"
   b. Click "Create Credentials" > "OAuth client ID"
   c. Application type: "Desktop app"
   d. Name it anything (e.g. "meal-planner")
   e. Download the JSON file and save it as: credentials.json
      in the same directory as this file (or set GOOGLE_CREDENTIALS_PATH in config)

3. Configure OAuth consent screen:
   a. Go to "APIs & Services" > "OAuth consent screen"
   b. User type: "External" (fine for personal use)
   c. Fill in app name and your email
   d. Add scope: .../auth/calendar.readonly
   e. Add your Gmail address as a test user

4. Install dependencies:
   pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client

5. First run will open a browser window asking you to authorize the app.
   After authorization, a token.json file is saved locally — you won't be
   prompted again until the token expires (~1 year).

--- CONFIGURATION (config.yaml) ---

calendar:
  credentials_path: credentials.json   # path to downloaded OAuth JSON
  token_path: token.json               # where the auth token is cached
  calendar_id: primary                 # 'primary' for your main calendar
  cook_event_prefixes:                 # only these prefixes trigger no-cook rules
    - "KRM"
    - "Family"
  ignore_prefixes:                     # these are always ignored
    - "SGM"
  fasting_event_title: "fasting"       # exact title (case-insensitive) for fast days
  fasting_event_hour: 5                # hour of day for fasting marker events (0-23)
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class DayConstraints:
    """
    Planning constraints derived from the calendar for a single day.

    Attributes:
        date:           The calendar date.
        is_no_cook:     True if a qualifying event makes this a no-cook day.
        is_fasting:     True if there is a fasting marker on this day.
        is_open_saturday: True if it's a Saturday with no qualifying events >2hrs.
        no_cook_reason: Human-readable explanation for why no_cook is set.
        qualifying_events: List of event titles that triggered constraints.
    """
    date: date
    is_no_cook: bool = False
    is_fasting: bool = False
    is_open_saturday: bool = False
    no_cook_reason: Optional[str] = None
    qualifying_events: list[str] = field(default_factory=list)

    @property
    def weekday_name(self) -> str:
        return self.date.strftime("%A")

    def __repr__(self) -> str:
        flags = []
        if self.is_no_cook:
            flags.append(f"no_cook({self.no_cook_reason})")
        if self.is_fasting:
            flags.append("fasting")
        if self.is_open_saturday:
            flags.append("open_saturday")
        flag_str = ", ".join(flags) if flags else "normal"
        return f"DayConstraints({self.date} {self.weekday_name}: {flag_str})"


@dataclass
class WeekConstraints:
    """
    Full constraint set for a planning week (Fri–Thu).

    Attributes:
        week_start_friday:  The Friday that opens the planning week.
        days:               Ordered list of DayConstraints, Fri through Thu.
        sunday_needs_easy:  True if Saturday was a no-cook day.
    """
    week_start_monday: date
    days: list[DayConstraints] = field(default_factory=list)

    @property
    def sunday_needs_easy(self) -> bool:
        saturday = self._day(5)  # Saturday is index 1 in Fri-based week
        if saturday and saturday.is_no_cook:
            return True
        return False

    def _day(self, weekday: int) -> Optional[DayConstraints]:
        """Return DayConstraints for a given weekday (0=Mon … 6=Sun)."""
        for dc in self.days:
            if dc.date.weekday() == weekday:
                return dc
        return None

    def get(self, d: date) -> Optional[DayConstraints]:
        for dc in self.days:
            if dc.date == d:
                return dc
        return None

    def no_cook_days(self) -> list[DayConstraints]:
        return [dc for dc in self.days if dc.is_no_cook]

    def fasting_days(self) -> list[DayConstraints]:
        return [dc for dc in self.days if dc.is_fasting]

    def open_saturday(self) -> Optional[DayConstraints]:
        for dc in self.days:
            if dc.is_open_saturday:
                return dc
        return None

    def summary(self) -> str:
        lines = [f"Week of {self.week_start_monday} (Mon–Sun):"]
        for dc in self.days:
            lines.append(f"  {repr(dc)}")
        if self.sunday_needs_easy:
            lines.append("  → Sunday requires an easy recipe (no-cook Saturday)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "credentials_path": "credentials.json",
    "token_path":       "token.json",
    "calendar_id":      "primary",
    "cook_event_prefixes": ["KRM", "Family"],
    "ignore_prefixes":     ["SGM"],
    "fasting_event_title": "fasting",
    "fasting_event_hour":  5,
}


# ---------------------------------------------------------------------------
# Calendar reader
# ---------------------------------------------------------------------------

class CalendarReader:
    """
    Reads Google Calendar and produces WeekConstraints for the planner.

    Args:
        config: Dict of config values (see DEFAULT_CONFIG for keys).
                Any missing keys fall back to defaults.
        timezone: IANA timezone string for interpreting event times.
                  Defaults to 'America/New_York'. Set to your local zone.
    """

    def __init__(self, config: Optional[dict] = None, timezone: str = "America/New_York"):
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.tz = ZoneInfo(timezone)
        self._service = None
        self._resolved_calendar_id: Optional[str] = None

    # -----------------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------------

    def _get_service(self):
        """Build and cache the Google Calendar API service object."""
        if self._service:
            return self._service

        try:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
        except ImportError:
            raise RuntimeError(
                "Google API libraries not installed. Run:\n"
                "  pip install google-auth google-auth-oauthlib "
                "google-auth-httplib2 google-api-python-client"
            )

        SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
        creds = None
        token_path = self.cfg["token_path"]
        creds_path = self.cfg["credentials_path"]

        import os
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
                log.info("Google Calendar token refreshed.")
            else:
                if not os.path.exists(creds_path):
                    raise FileNotFoundError(
                        f"OAuth credentials file not found: {creds_path}\n"
                        "See the setup instructions at the top of calendar_reader.py"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
                creds = flow.run_local_server(port=0)
                log.info("Google Calendar authorization complete.")

            with open(token_path, "w") as f:
                f.write(creds.to_json())
            log.info("Token saved to %s", token_path)

        self._service = build("calendar", "v3", credentials=creds)
        return self._service

    # -----------------------------------------------------------------------
    # Date helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def next_planning_monday(from_date: Optional[date] = None) -> date:
        """Return the Monday of the next calendar week from from_date.
        e.g. any day in week of Mar 16 returns Mar 23."""
        d = from_date or date.today()
        # Monday of next week = Monday of current week + 7
        days_to_monday = (7 - d.weekday()) % 7  # days until next Monday
        if days_to_monday == 0:
            days_to_monday = 7  # always go to NEXT week's Monday
        return d + timedelta(days=days_to_monday)

    @staticmethod
    def planning_window(start_monday: date) -> tuple[date, date]:
        """Return (monday, following_sunday) for a planning week."""
        return start_monday, start_monday + timedelta(days=6)

    # -----------------------------------------------------------------------
    # Event fetching
    # -----------------------------------------------------------------------

    def _resolve_calendar_id(self) -> str:
        """
        Resolve the configured calendar_id to an actual Google Calendar ID.

        If the value looks like an ID already (contains '@' or equals 'primary'),
        return it as-is. Otherwise treat it as a calendar name and search the
        user's calendar list for a match.
        """
        if self._resolved_calendar_id:
            return self._resolved_calendar_id

        configured = self.cfg["calendar_id"]

        # Already looks like a real ID
        if configured == "primary" or "@" in configured:
            self._resolved_calendar_id = configured
            return configured

        # Look up by name in the user's calendar list
        service = self._get_service()
        response = service.calendarList().list().execute()
        calendars = response.get("items", [])

        for cal in calendars:
            if cal.get("summary", "").strip().lower() == configured.strip().lower():
                self._resolved_calendar_id = cal["id"]
                log.info("Resolved calendar '%s' → %s", configured, cal["id"])
                return cal["id"]

        # List available calendars to help debugging
        names = [c.get("summary", "?") for c in calendars]
        raise ValueError(
            f"Calendar '{configured}' not found in your Google Calendar account.\n"
            f"Available calendars: {names}\n"
            f"Check the calendar_id setting in config.yaml."
        )

    def _fetch_events(self, start: date, end: date) -> list[dict]:
        """
        Fetch all events from the calendar between start and end (inclusive).
        Returns raw event dicts from the Google API.
        """
        service = self._get_service()
        calendar_id = self._resolve_calendar_id()

        # API expects RFC3339 datetimes
        time_min = datetime.combine(start, time.min).replace(tzinfo=self.tz).isoformat()
        time_max = datetime.combine(end, time(23, 59, 59)).replace(tzinfo=self.tz).isoformat()

        events = []
        page_token = None

        while True:
            response = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,       # expand recurring events
                orderBy="startTime",
                pageToken=page_token,
            ).execute()

            events.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        log.debug("Fetched %d events from %s to %s", len(events), start, end)
        return events

    # -----------------------------------------------------------------------
    # Event classification
    # -----------------------------------------------------------------------

    def _should_ignore(self, title: str) -> bool:
        """True if this event should be completely ignored."""
        for prefix in self.cfg["ignore_prefixes"]:
            if title.upper().startswith(prefix.upper()):
                return True
        return False

    def _is_qualifying(self, title: str) -> bool:
        """True if this event can trigger cooking restrictions."""
        for prefix in self.cfg["cook_event_prefixes"]:
            if title.upper().startswith(prefix.upper()):
                return True
        return False

    def _is_fasting_event(self, event: dict) -> bool:
        """
        True if this event is a fasting marker.
        Pattern: title == 'fasting' (case-insensitive), starts at 5am.
        """
        title = event.get("summary", "").strip().lower()
        if title != self.cfg["fasting_event_title"].lower():
            return False

        start = event.get("start", {})
        # Must be a timed event (not all-day), starting at the configured hour
        dt_str = start.get("dateTime")
        if not dt_str:
            return False

        try:
            dt = datetime.fromisoformat(dt_str)
            return dt.hour == self.cfg["fasting_event_hour"]
        except ValueError:
            return False

    def _event_date(self, event: dict) -> Optional[date]:
        """Extract the calendar date of an event's start."""
        start = event.get("start", {})
        if "dateTime" in start:
            try:
                return datetime.fromisoformat(start["dateTime"]).date()
            except ValueError:
                return None
        if "date" in start:
            try:
                return date.fromisoformat(start["date"])
            except ValueError:
                return None
        return None

    def _event_start_time(self, event: dict) -> Optional[datetime]:
        """Return the start datetime of a timed event, or None for all-day."""
        dt_str = event.get("start", {}).get("dateTime")
        if not dt_str:
            return None
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self.tz)
            return dt
        except ValueError:
            return None

    def _event_duration_hours(self, event: dict) -> float:
        """Return event duration in hours. Returns 0 for all-day events."""
        start_str = event.get("start", {}).get("dateTime")
        end_str = event.get("end", {}).get("dateTime")
        if not start_str or not end_str:
            return 0.0
        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str)
            return (end_dt - start_dt).total_seconds() / 3600
        except ValueError:
            return 0.0

    def _is_after_3pm(self, event: dict) -> bool:
        """True if the event starts after 3:00 PM."""
        start_dt = self._event_start_time(event)
        if start_dt is None:
            return False
        return start_dt.hour >= 15

    def _is_longer_than_2hrs(self, event: dict) -> bool:
        """True if the event duration exceeds 2 hours."""
        return self._event_duration_hours(event) > 2.0

    # -----------------------------------------------------------------------
    # Constraint derivation
    # -----------------------------------------------------------------------

    def _apply_weekday_rules(self, dc: DayConstraints, event: dict):
        """
        Apply weekday no-cook rule:
        Any qualifying event after 3pm → no-cook day.
        """
        title = event.get("summary", "")
        if self._is_after_3pm(event):
            dc.is_no_cook = True
            dc.no_cook_reason = f"qualifying event after 3pm"
            dc.qualifying_events.append(title)
            log.debug("%s: no-cook (after 3pm: '%s')", dc.date, title)

    def _apply_saturday_rules(self, dc: DayConstraints, events: list[dict]):
        """
        Saturday rules:
        - If any qualifying event is >2hrs → no-cook Saturday
        - Otherwise → open Saturday (experiment slot)
        """
        qualifying = [
            e for e in events
            if not self._should_ignore(e.get("summary", ""))
            and self._is_qualifying(e.get("summary", ""))
        ]

        blocking = [e for e in qualifying if self._is_longer_than_2hrs(e)]

        if blocking:
            dc.is_no_cook = True
            dc.no_cook_reason = "qualifying event >2hrs"
            dc.qualifying_events = [e.get("summary", "") for e in blocking]
            log.debug("%s: no-cook Saturday (%d blocking events)", dc.date, len(blocking))
        else:
            dc.is_open_saturday = True
            log.debug("%s: open Saturday", dc.date)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def get_week_constraints(self, start_monday: Optional[date] = None) -> WeekConstraints:
        """
        Fetch and return constraints for the planning week starting on start_monday.
        If start_monday is None, uses the Monday of the next calendar week.

        Returns a WeekConstraints object covering all 7 days (Mon–Sun).
        """
        monday = start_monday or self.next_planning_monday()
        sunday = monday + timedelta(days=6)

        log.info("Fetching calendar constraints for %s – %s", monday, sunday)

        raw_events = self._fetch_events(monday, sunday)

        # Group events by date
        events_by_date: dict[date, list[dict]] = {}
        for d_offset in range(7):
            events_by_date[monday + timedelta(days=d_offset)] = []

        for event in raw_events:
            title = event.get("summary", "")
            if self._should_ignore(title):
                log.debug("Ignoring event: '%s'", title)
                continue
            event_date = self._event_date(event)
            if event_date and event_date in events_by_date:
                events_by_date[event_date].append(event)

        wc = WeekConstraints(week_start_monday=monday)

        for d_offset in range(7):
            current_date = monday + timedelta(days=d_offset)
            day_events = events_by_date[current_date]
            dc = DayConstraints(date=current_date)

            # Fasting check — runs for any day
            for event in day_events:
                if self._is_fasting_event(event):
                    dc.is_fasting = True
                    log.debug("%s: fasting day", current_date)

            weekday = current_date.weekday()  # 0=Mon, 5=Sat, 6=Sun

            if weekday == 5:  # Saturday
                self._apply_saturday_rules(dc, day_events)
            else:
                # Weekday and Sunday: check for qualifying events after 3pm
                for event in day_events:
                    if self._is_qualifying(event.get("summary", "")):
                        self._apply_weekday_rules(dc, event)
                        break  # one qualifying event is enough to mark no-cook

            wc.days.append(dc)

        log.info("Week constraints built. No-cook days: %d, Fasting days: %d",
                 len(wc.no_cook_days()), len(wc.fasting_days()))
        return wc


# ---------------------------------------------------------------------------
# CLI for manual testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Fetch and display week constraints from Google Calendar.")
    parser.add_argument("--config",  default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--monday",  help="Start Monday as YYYY-MM-DD (default: next calendar week Monday)")
    parser.add_argument("--timezone", default="America/New_York", help="IANA timezone")
    args = parser.parse_args()

    config = {}
    try:
        with open(args.config) as f:
            full_config = yaml.safe_load(f)
            config = full_config.get("calendar", {})
    except FileNotFoundError:
        log.warning("No config.yaml found, using defaults.")

    start_monday = date.fromisoformat(args.monday) if args.monday else None

    reader = CalendarReader(config=config, timezone=args.timezone)
    wc = reader.get_week_constraints(start_monday)
    print(wc.summary())
