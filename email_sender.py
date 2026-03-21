"""
email_sender.py

Formats a WeekPlan + GroceryList + StackingNotes into a plain-text email
and sends it via the Gmail API. Also handles re-sending a revised plan
in response to user feedback.

--- GMAIL API SETUP ---

The Gmail API uses the same OAuth credentials as the Calendar API.
If you have already completed the calendar_reader.py setup, you only need
to add the Gmail scope to your OAuth consent screen:

  1. Go to Google Cloud Console → APIs & Services → OAuth consent screen
  2. Add scope: https://www.googleapis.com/auth/gmail.send
     and:       https://www.googleapis.com/auth/gmail.readonly
  3. Delete your existing token.json so it is re-generated with the new scopes
  4. Run the agent once — it will prompt you to re-authorise in the browser

--- EMAIL FORMAT ---

Subject: Meal Plan: Mar 20 — Mar 26  ·  4 cook nights

Body (plain text):

  MEAL PLAN: Mar 20 – Mar 26
  4 cook nights

  ── THIS WEEK ──────────────────────────────────────

  Friday, Mar 20        Takeout pizza 🍕
  Saturday, Mar 21      Spiced Rice with Crispy Chickpeas
  Sunday, Mar 22        Hamburger Steaks with Onion Gravy
  Monday, Mar 23        no cook  (qualifying event after 3pm)
  Tuesday, Mar 24       Creamy Tomato and Spinach Pasta  [pasta]
  Wednesday, Mar 25     Instant Pot Ground Beef and Pasta  ⚠ meatless day
  Thursday, Mar 26      leftovers

  ── LUNCH THIS WEEK ────────────────────────────────

  Quinoa Mediterranean grain bowl
  ⚑ Weekend prep: Cook quinoa Sunday

  ── STACKING NOTES ─────────────────────────────────

  • Tuesday and Thursday both use rice — consider cooking a double batch
    on Tuesday.
  • Wednesday and Saturday both use cream cheese. Combined usage fits in
    one package — buy one and plan accordingly.

  ── GROCERY LIST ───────────────────────────────────

  PRODUCE
    onion (2)  · garlic (3 cloves)  · shallots (2)  · ...

  PROTEIN
    ground beef — 1 1/2 lb  · ground lamb — 1 lb

  DAIRY
    cream cheese — 4 oz  · whipping cream — 3/4 cup

  PANTRY
    basmati rice — 1 cup  · penne pasta — 1 lb  · ...

  ── LIKELY ON HAND ─────────────────────────────────

  The following were on last week's plan. Confirm before buying:
    • garam masala
    • beef broth
    • tomato paste

  ───────────────────────────────────────────────────
  Reply to this email to make changes.
  "Swap Tuesday for a pasta dish" / "Skip the experiment" / "Okay thanks"
"""

import base64
import logging
import os
from datetime import date, timedelta
from email.mime.text import MIMEText
from typing import Optional

from grocery_builder import GroceryList, GroceryItem, CATEGORY_ORDER
from planner import WeekPlan, MealSlot
from stacker import StackingNote

log = logging.getLogger(__name__)

# Gmail OAuth scopes needed
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Combined scopes for a single token covering both calendar and gmail
ALL_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

DEFAULT_CONFIG = {
    "credentials_path": "credentials.json",
    "token_path":       "token.json",
    "to_address":       None,   # required — set in config.yaml
}

# Column width for the meal schedule table
MEAL_COL_WIDTH = 22


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _divider(label: str = "", width: int = 52) -> str:
    if label:
        pad = width - len(label) - 4
        return f"── {label} {'─' * max(pad, 2)}"
    return "─" * width


def _fmt_date_range(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.strftime('%b %-d')} – {sunday.strftime('%-d')}"
    return f"{monday.strftime('%b %-d')} – {sunday.strftime('%b %-d')}"


def _fmt_day_header(d: date) -> str:
    return f"{d.strftime('%A, %b %-d'):<{MEAL_COL_WIDTH}}"


def _slot_label(slot: MealSlot) -> str:
    """Build the display label for a meal slot, with inline flags."""
    parts = [slot.label]

    tag_badges = []
    if "pasta" in slot.tags:
        tag_badges.append("[pasta]")
    if "taco" in slot.tags:
        tag_badges.append("[taco]")
    if "freezer" in slot.tags:
        tag_badges.append("[freezer-friendly]")
    if tag_badges:
        parts.append("  " + " ".join(tag_badges))

    flags = []
    if slot.is_fasting:
        flags.append("⚑ fasting day")
    if slot.is_meatless and not slot.is_no_cook and slot.date.weekday() not in (2, 4):
        # Only show meatless flag on non-standing meatless days (it's implied Wed/Fri)
        flags.append("⚑ meatless day")
    for note in slot.notes:
        if note not in ("Would have been meatless", "Fasting day"):
            flags.append(f"({note})")
    if flags:
        parts.append("  " + "  ".join(flags))

    return "".join(parts)


def _fmt_quantity(item: GroceryItem) -> str:
    parts = []
    if item.quantity:
        parts.append(item.quantity)
    if item.unit:
        parts.append(item.unit)
    qty = " ".join(parts)
    if item.quantity_note:
        qty = f"{qty}  [{item.quantity_note}]" if qty else f"[{item.quantity_note}]"
    return qty


def _fmt_grocery_category(category: str, items: list[GroceryItem]) -> list[str]:
    if not items:
        return []
    lines = [f"  {category.upper()}"]
    for item in sorted(items, key=lambda i: i.name.lower()):
        qty = _fmt_quantity(item)
        if qty:
            lines.append(f"    {item.name} — {qty}")
        else:
            lines.append(f"    {item.name}")
    return lines


# ---------------------------------------------------------------------------
# Email body formatter
# ---------------------------------------------------------------------------

def format_plan_email(
    plan: WeekPlan,
    grocery: GroceryList,
    stacking_notes: list[StackingNote],
    is_revision: bool = False,
) -> tuple[str, str]:
    """
    Format the weekly plan into (subject, body) strings.

    Args:
        plan:            The WeekPlan.
        grocery:         The GroceryList.
        stacking_notes:  List of StackingNote advisories.
        is_revision:     True if this is a re-send after user feedback.

    Returns:
        (subject, body) — both plain text strings.
    """
    monday   = plan.week_start_monday
    date_str = _fmt_date_range(monday)
    revision_tag = " [REVISED]" if is_revision else ""

    subject = (
        f"Meal Plan: {date_str}  ·  {plan.cook_nights} cook nights{revision_tag}"
    )

    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────────
    lines.append(f"MEAL PLAN: {date_str}")
    lines.append(f"{plan.cook_nights} cook nights")
    if plan.warnings:
        for w in plan.warnings:
            lines.append(f"⚠  {w}")
    lines.append("")

    # ── This week ───────────────────────────────────────────────────────────
    lines.append(_divider("THIS WEEK"))
    lines.append("")

    for slot in plan.dinners:
        day_str  = _fmt_day_header(slot.date)
        meal_str = _slot_label(slot)
        lines.append(f"  {day_str}{meal_str}")

    lines.append("")

    # ── Lunch ───────────────────────────────────────────────────────────────
    if plan.lunch:
        lines.append(_divider("LUNCH THIS WEEK"))
        lines.append("")
        lines.append(f"  {plan.lunch.label}")
        for note in plan.lunch.notes:
            lines.append(f"  ⚑ {note}")
        lines.append("")

    # ── Stacking notes ──────────────────────────────────────────────────────
    if stacking_notes:
        lines.append(_divider("STACKING NOTES"))
        lines.append("")
        for note in stacking_notes:
            # Word-wrap long notes at 68 chars
            wrapped = _wrap(f"• {note.message}", width=68, indent="  ")
            lines.extend(wrapped)
        lines.append("")

    # ── Grocery list ────────────────────────────────────────────────────────
    lines.append(_divider("GROCERY LIST"))
    lines.append("")

    has_items = False
    for cat in CATEGORY_ORDER:
        items = grocery.category_items(cat)
        if items:
            has_items = True
            lines.extend(_fmt_grocery_category(cat, items))
            lines.append("")

    if not has_items:
        lines.append("  (no grocery items this week)")
        lines.append("")

    # ── Likely on hand ──────────────────────────────────────────────────────
    if grocery.likely_on_hand:
        lines.append(_divider("LIKELY ON HAND"))
        lines.append("")
        lines.append("  The following were on last week's plan.")
        lines.append("  Confirm before buying — or tell me to remove them:")
        lines.append("")
        for item in sorted(grocery.likely_on_hand, key=lambda i: i.name.lower()):
            lines.append(f"    • {item.name}")
        lines.append("")

    # ── Footer ──────────────────────────────────────────────────────────────
    lines.append(_divider())
    lines.append("  Reply to make changes:")
    lines.append('  "Swap Tuesday for a pasta dish"')
    lines.append('  "Skip the experiment this week"')
    lines.append('  "The cream cheese is already gone"')
    lines.append('  "Okay thanks"  ← ends this week\'s revision window')
    lines.append("")

    body = "\n".join(lines)
    return subject, body


def _wrap(text: str, width: int = 68, indent: str = "  ") -> list[str]:
    """Wrap a single paragraph to width, re-indenting continuation lines."""
    if len(text) <= width:
        return [text]
    words = text.split()
    lines: list[str] = []
    current = ""
    is_first = True
    for word in words:
        sep = " " if current else ""
        candidate = current + sep + word
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            is_first = False
            current = indent + word
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Gmail sender
# ---------------------------------------------------------------------------

class EmailSender:
    """
    Sends meal plan emails via the Gmail API.

    Args:
        config:   Dict with keys: credentials_path, token_path, to_address.
                  Missing keys fall back to DEFAULT_CONFIG.
        dry_run:  If True, prints the email instead of sending it.
    """

    def __init__(self, config: Optional[dict] = None, dry_run: bool = False):
        self.cfg     = {**DEFAULT_CONFIG, **(config or {})}
        self.dry_run = dry_run
        self._service = None

    # -----------------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------------

    def _get_service(self):
        if self._service:
            return self._service

        try:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
        except ImportError:
            raise RuntimeError(
                "Google API libraries not installed.\n"
                "Run: pip install google-auth google-auth-oauthlib "
                "google-auth-httplib2 google-api-python-client"
            )

        creds = None
        token_path = self.cfg["token_path"]
        creds_path = self.cfg["credentials_path"]

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, ALL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
            else:
                if not os.path.exists(creds_path):
                    raise FileNotFoundError(
                        f"OAuth credentials not found: {creds_path}\n"
                        "See setup instructions in calendar_reader.py"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    creds_path, ALL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    # -----------------------------------------------------------------------
    # Core send
    # -----------------------------------------------------------------------

    def _build_message(self, to: str, subject: str, body: str) -> dict:
        """Build a raw Gmail API message dict."""
        msg = MIMEText(body, "plain", "utf-8")
        msg["To"]      = to
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        return {"raw": raw}

    def _send_raw(self, message: dict) -> dict:
        """Send a pre-built message dict via the Gmail API."""
        service = self._get_service()
        result = service.users().messages().send(
            userId="me", body=message
        ).execute()
        log.info("Email sent. Message ID: %s", result.get("id"))
        return result

    def send_plan(
        self,
        plan: WeekPlan,
        grocery: GroceryList,
        stacking_notes: list[StackingNote],
        is_revision: bool = False,
    ) -> Optional[str]:
        """
        Format and send the weekly meal plan email.

        Returns the Gmail message ID on success, or None in dry-run mode.
        """
        to = self.cfg.get("to_address")
        if not to:
            raise ValueError(
                "to_address not set in config. "
                "Add 'to_address: your@email.com' to config.yaml under 'email'."
            )

        subject, body = format_plan_email(plan, grocery, stacking_notes, is_revision)

        if self.dry_run:
            print("=" * 60)
            print(f"TO:      {to}")
            print(f"SUBJECT: {subject}")
            print("=" * 60)
            print(body)
            print("=" * 60)
            return None

        message = self._build_message(to, subject, body)
        result  = self._send_raw(message)
        return result.get("id")

    # -----------------------------------------------------------------------
    # Thread / reply management
    # -----------------------------------------------------------------------

    def get_latest_reply(self, week_label: str) -> Optional[dict]:
        """
        Search Gmail for a reply to the meal plan email for the given week.

        Args:
            week_label: The date range string used in the subject,
                        e.g. "Mar 20 – 26"

        Returns:
            A dict with keys {message_id, thread_id, body, received_at}
            or None if no reply found.
        """
        service = self._get_service()
        query = f"subject:\"Meal Plan: {week_label}\" in:inbox"

        results = service.users().messages().list(
            userId="me", q=query, maxResults=10
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            log.debug("No replies found for query: %s", query)
            return None

        # Get the most recent message in the thread
        for msg_meta in messages:
            msg = service.users().messages().get(
                userId="me",
                messageId=msg_meta["id"],
                format="full",
            ).execute()

            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }

            # Skip messages we sent (From: contains our address)
            from_addr = headers.get("from", "")
            to_addr = self.cfg.get("to_address", "")
            if to_addr and to_addr.lower() in from_addr.lower():
                # This is a message we sent, not a reply
                # Check if it's actually a reply (has Re: in subject)
                subject_hdr = headers.get("subject", "")
                if not subject_hdr.lower().startswith("re:"):
                    continue

            body = self._extract_body(msg)
            if not body:
                continue

            return {
                "message_id": msg["id"],
                "thread_id":  msg.get("threadId"),
                "body":       body,
                "received_at": headers.get("date", ""),
                "from":        from_addr,
            }

        return None

    def _extract_body(self, message: dict) -> Optional[str]:
        """Extract plain-text body from a Gmail message."""
        payload = message.get("payload", {})

        def _decode(data: str) -> str:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

        # Simple single-part message
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return _decode(data)

        # Multipart — find first text/plain part
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return _decode(data)
            # Nested multipart
            for subpart in part.get("parts", []):
                if subpart.get("mimeType") == "text/plain":
                    data = subpart.get("body", {}).get("data", "")
                    if data:
                        return _decode(data)

        return None

    def is_acknowledgment(self, body: str) -> bool:
        """
        Return True if the reply body is a terminal acknowledgment
        that should stop the polling loop.
        """
        lower = body.strip().lower()
        # Strip quoted reply text (lines starting with >)
        lines = [l for l in lower.splitlines() if not l.startswith(">")]
        cleaned = " ".join(lines).strip()

        ACK_PATTERNS = [
            "okay thanks", "ok thanks", "looks good", "perfect",
            "great thanks", "all good", "sounds good", "done",
            "got it", "thanks", "thank you", "good", "👍",
        ]
        return any(p in cleaned for p in ACK_PATTERNS)


# ---------------------------------------------------------------------------
# CLI dry-run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Dry-run mode: generate a sample email from synthetic data and print it.
    Useful for checking formatting without sending anything.

    Usage: python email_sender.py
    """
    from datetime import date
    from planner import WeekPlan, MealSlot
    from grocery_builder import GroceryList, GroceryItem
    from stacker import StackingNote

    monday = date(2026, 3, 23)

    # Minimal synthetic plan
    plan = WeekPlan(
        week_start_monday=monday,
        week_key="2026-03-23",
        cook_nights=4,
    )

    def _slot(d, rid, label, tags=None, is_no_cook=False, notes=None,
               is_meatless=False, is_fasting=False):
        return MealSlot(
            date=d, slot="dinner", recipe_id=rid, label=label,
            tags=tags or [], ingredients=[], notes=notes or [],
            is_no_cook=is_no_cook, is_meatless=is_meatless, is_fasting=is_fasting,
        )

    from datetime import timedelta
    plan.dinners = [
        _slot(monday,               "hamburger-steaks", "Hamburger Steaks with Onion Gravy", tags=["onRotation"]),
        _slot(monday+timedelta(1),  None,               "no cook",  is_no_cook=True, notes=["qualifying event after 3pm"]),
        _slot(monday+timedelta(2),  "creamy-pasta",     "Creamy Tomato and Spinach Pasta",   tags=["pasta", "onRotation"], is_meatless=True),
        _slot(monday+timedelta(3),  None,               "leftovers"),
        _slot(monday+timedelta(4),  None,               "Takeout pizza",                     tags=["pizza"], is_meatless=True),
        _slot(monday+timedelta(5),  "spiced-rice",      "Spiced Rice with Crispy Chickpeas", tags=["vegetarian", "onRotation"]),
        _slot(monday+timedelta(6),  "ip-pasta",         "Instant Pot Ground Beef and Pasta", tags=["pasta", "onRotation"]),
    ]
    plan.lunch = MealSlot(
        date=monday, slot="lunch", recipe_id="grain-bowl",
        label="Quinoa Mediterranean grain bowl", tags=[], ingredients=[],
        notes=["Weekend prep: Cook quinoa Sunday"],
    )

    grocery = GroceryList(week_start_monday=monday)
    grocery.items_by_category = {
        "produce": [
            GroceryItem("onion, halved and sliced thin", "onion", "produce", "1", None, recipes=["Hamburger Steaks"]),
            GroceryItem("large shallots, cut into wedges", "shallot", "produce", "2", None, recipes=["Spiced Rice"]),
        ],
        "protein": [
            GroceryItem("85 percent lean ground beef", "ground beef", "protein", "1 1/2", "pounds", recipes=["Hamburger Steaks"]),
        ],
        "dairy": [
            GroceryItem("cream cheese", "cream cheese", "dairy", "4", "oz", recipes=["Creamy Pasta"]),
            GroceryItem("whipping cream", "heavy cream", "dairy", "3/4", "cup", recipes=["Cheesecake"]),
        ],
        "pantry": [
            GroceryItem("basmati rice", "rice", "pantry", "1", "cup", recipes=["Spiced Rice"]),
            GroceryItem("penne pasta", "pasta", "pantry", "1", "lb", recipes=["Creamy Pasta"]),
            GroceryItem("tomato paste", "tomato paste", "pantry", "4", "tbsp", recipes=["Creamy Pasta"]),
        ],
        "frozen": [],
        "other": [],
    }
    grocery.likely_on_hand = [
        GroceryItem("garam masala", "garam masala", "pantry", None, None, recipes=["Spiced Rice"]),
        GroceryItem("beef broth", "beef broth", "pantry", None, None, recipes=["Hamburger Steaks"]),
    ]

    stacking_notes = [
        StackingNote(kind="BATCH",   canonical="rice",         meals=["Spiced Rice", "Rice Bowl"],   message="Tuesday and Saturday both use rice — consider cooking a double batch on Tuesday."),
        StackingNote(kind="REMNANT", canonical="cream cheese", meals=["Creamy Pasta"],               message="Creamy Tomato Pasta calls for 4 oz cream cheese — you'll have some of a package left. Consider another recipe this week that uses cream cheese."),
    ]

    sender = EmailSender(config={"to_address": "you@example.com"}, dry_run=True)
    sender.send_plan(plan, grocery, stacking_notes)
