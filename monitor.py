
import os
import json
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin

# ============================================================
# CONFIGURATION
# ============================================================

API_BASE = "https://apis.cineplex.com/prod/cpx/theatrical/api/v1"

THEATRES = {
    7411: "SilverCity Brampton Cinemas",
    7122: "Cineplex Courtney Park",
    7420: "Cineplex Square One",
}

DATES = [
    "12/17/2026",
    "12/18/2026",
    "12/19/2026",
    "12/20/2026",
]

FILM_ID = 34362
FILM_NAME = "Avengers: Doomsday"

# Any session containing one of these experiences will be ignored.
EXCLUDED_EXPERIENCES = {
    "3D",
    "ScreenX",
}

STATE_FILE = Path("state.json")

API_KEY = os.getenv("CINEPLEX_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

# ============================================================
# VALIDATION
# ============================================================

if not API_KEY:
    raise RuntimeError("Missing CINEPLEX_API_KEY environment variable.")

if not DISCORD_WEBHOOK:
    raise RuntimeError("Missing DISCORD_WEBHOOK environment variable.")

HEADERS = {
    "Ocp-Apim-Subscription-Key": API_KEY,
    "Accept": "application/json",
}

# ============================================================
# DISCORD
# ============================================================

def send_discord(message):
    """Send a message to Discord, splitting long messages if needed."""

    max_length = 1900

    chunks = []
    while len(message) > max_length:
        split_at = message.rfind("\n", 0, max_length)

        if split_at == -1:
            split_at = max_length

        chunks.append(message[:split_at])
        message = message[split_at:].strip()

    if message:
        chunks.append(message)

    for chunk in chunks:
        try:
            response = requests.post(
                DISCORD_WEBHOOK,
                json={"content": chunk},
                timeout=15,
            )
            response.raise_for_status()

        except requests.RequestException as error:
            print(f"Discord notification failed: {error}")


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    """
    Load previously detected sessions.

    Supports both the newer {"seen": [...]} format and
    older dictionary-based state files.
    """

    if not STATE_FILE.exists():
        return set()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        # Current format
        if isinstance(data, dict) and isinstance(data.get("seen"), list):
            return set(str(item) for item in data["seen"])

        # Legacy dictionary format
        if isinstance(data, dict):
            return set(str(key) for key in data.keys())

        # Legacy list format
        if isinstance(data, list):
            return set(str(item) for item in data)

    except (json.JSONDecodeError, OSError) as error:
        print(f"Could not load state file: {error}")

    return set()


def save_state(seen_sessions):
    """Save detected sessions to state.json."""

    data = {
        "seen": sorted(seen_sessions)
    }

    temporary_file = STATE_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    temporary_file.replace(STATE_FILE)


# ============================================================
# API
# ============================================================

def get_showtimes(theatre_id, date):
    """Retrieve showtimes for one theatre and one date."""

    url = f"{API_BASE}/showtimes"

    params = {
        "language": "en",
        "locationId": theatre_id,
        "date": date,
        "filmId": FILM_ID,
    }

    response = requests.get(
        url,
        headers=HEADERS,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# SESSION PARSING
# ============================================================

def walk_dicts(value):
    """Recursively yield every dictionary in a JSON response."""

    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_dicts(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def get_experience_tags(session):
    """
    Extract experience tags from a session.

    Supports string, list, and dictionary representations.
    """

    tags = []

    experience_keys = {
        "experience",
        "experiences",
        "experiencetag",
        "experiencetags",
        "experiencename",
        "format",
        "formats",
        "formatname",
    }

    def extract(value):
        if isinstance(value, str):
            if value.strip():
                tags.append(value.strip())

        elif isinstance(value, list):
            for item in value:
                extract(item)

        elif isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in experience_keys:
                    extract(child)

    for key, value in session.items():
        if str(key).lower() in experience_keys:
            extract(value)

    return sorted(set(tags))


def get_session_time(session):
    """Find a readable showtime from common API field names."""

    possible_fields = [
        "showTime",
        "startTime",
        "sessionTime",
        "dateTime",
        "startDateTime",
        "showDateTime",
        "start",
    ]

    for field in possible_fields:
        value = session.get(field)

        if value:
            return str(value)

    return "Time unavailable"


def get_session_value(session, possible_fields, default=None):
    """Return the first available value from a list of field names."""

    for field in possible_fields:
        value = session.get(field)

        if value is not None:
            return value

    return default


def is_excluded(tags):
    """Return True if any experience tag matches an exclusion."""

    excluded = {
        "".join(character.lower() for character in item if character.isalnum())
        for item in EXCLUDED_EXPERIENCES
    }

    for tag in tags:
        normalized_tag = "".join(
            character.lower()
            for character in tag
            if character.isalnum()
        )

        for excluded_tag in excluded:
            if excluded_tag and excluded_tag in normalized_tag:
                return True

    return False


def extract_sessions(response_data):
    """
    Find all session records in the API response.

    The API can return duplicate records for the same session
    under different experience configurations. We combine
    those records by vistaSessionId before filtering.
    """

    grouped_sessions = {}

    for item in walk_dicts(response_data):

        session_id = item.get("vistaSessionId")

        if session_id is None:
            continue

        session_id = str(session_id)

        if session_id not in grouped_sessions:
            grouped_sessions[session_id] = {
                "session_id": session_id,
                "records": [],
                "experience_tags": set(),
            }

        grouped_sessions[session_id]["records"].append(item)

        tags = get_experience_tags(item)

        grouped_sessions[session_id]["experience_tags"].update(tags)

    results = []

    for session_id, grouped in grouped_sessions.items():

        records = grouped["records"]
        tags = sorted(grouped["experience_tags"])

        # Filter only AFTER combining all duplicate records.
        if is_excluded(tags):
            print(
                f"Skipping session {session_id}: "
                f"excluded experience found: {tags}"
            )
            continue

        # Use the first record as the main session record.
        session = records[0]

        results.append({
            "session_id": session_id,
            "time": get_session_time(session),
            "experience_tags": tags,
            "seats_remaining": get_session_value(
                session,
                ["seatsRemaining", "SeatsRemaining"],
            ),
            "is_sold_out": get_session_value(
                session,
                ["isSoldOut", "IsSoldOut"],
                False,
            ),
            "seat_map_url": get_session_value(
                session,
                ["seatMapUrl", "SeatMapUrl"],
            ),
            "ticket_url": get_session_value(
                session,
                ["ticketUrl", "bookingUrl", "url"],
            ),
        })

    return results


# ============================================================
# DISCORD MESSAGE FORMATTING
# ============================================================

def format_session_message(theatre_name, date, session):
    """Format a newly detected session for Discord."""

    tags = session["experience_tags"]

    experience_text = (
        ", ".join(tags) if tags else "Not specified"
    )

    message = (
        f"🎬 **NEW {FILM_NAME} SHOWTIME FOUND!**\n"
        f"📍 **Theatre:** {theatre_name}\n"
        f"📅 **Date:** {date}\n"
        f"🕒 **Time:** {session['time']}\n"
        f"🎥 **Experience:** {experience_text}\n"
        f"🆔 **Session ID:** `{session['session_id']}`\n"
    )

    seats = session["seats_remaining"]

    if seats is not None:
        message += f"💺 **Seats remaining:** {seats}\n"

    if session["is_sold_out"]:
        message += "⚠️ **This session is marked as sold out.**\n"

    if session["seat_map_url"]:
        message += f"🗺️ [View Seat Map]({session['seat_map_url']})\n"

    if session["ticket_url"]:
        message += f"🎟️ [Book Tickets]({session['ticket_url']})\n"

    return message


# ============================================================
# MAIN MONITOR
# ============================================================

def main():

    print("=" * 55)
    print(f"Starting Cineplex monitor: {FILM_NAME}")
    print(f"Film ID: {FILM_ID}")
    print(f"Theatres: {len(THEATRES)}")
    print(f"Dates: {len(DATES)}")
    print(f"Total API requests expected: {len(THEATRES) * len(DATES)}")
    print(f"Excluded experiences: {EXCLUDED_EXPERIENCES}")
    print("=" * 55)

    seen_sessions = load_state()

    updated_state = set(seen_sessions)

    new_sessions = []
    errors = []

    successful_checks = 0
    total_checks = len(THEATRES) * len(DATES)

    # --------------------------------------------------------
    # Check every theatre and date
    # --------------------------------------------------------

    for theatre_id, theatre_name in THEATRES.items():

        for date in DATES:

            print(f"\nChecking {theatre_name} | {date}")

            try:
                response_data = get_showtimes(theatre_id, date)

                sessions = extract_sessions(response_data)

                successful_checks += 1

                print(f"Found {len(sessions)} eligible sessions.")

                for session in sessions:

                    # Unique key for theatre + date + session.
                    state_key = (
                        f"{theatre_id}|{date}|{session['session_id']}"
                    )

                    if state_key in seen_sessions:
                        continue

                    print(
                        f"NEW SESSION: {session['session_id']} "
                        f"at {session['time']}"
                    )

                    updated_state.add(state_key)

                    new_sessions.append({
                        "theatre": theatre_name,
                        "date": date,
                        "session": session,
                    })

            except requests.RequestException as error:

                error_message = (
                    f"{theatre_name} on {date}: {error}"
                )

                print(f"API ERROR: {error_message}")
                errors.append(error_message)

            except Exception as error:

                error_message = (
                    f"{theatre_name} on {date}: {error}"
                )

                print(f"UNEXPECTED ERROR: {error_message}")
                errors.append(error_message)

    # --------------------------------------------------------
    # Notify Discord about new sessions
    # --------------------------------------------------------

    if new_sessions:

        print(f"\nFound {len(new_sessions)} new sessions!")

        for item in new_sessions:

            message = format_session_message(
                item["theatre"],
                item["date"],
                item["session"],
            )

            send_discord(message)

    else:
        print("\nNo new sessions found.")

    # --------------------------------------------------------
    # Report API errors
    # --------------------------------------------------------

    if errors:

        error_message = (
            f"⚠️ **Cineplex Monitor Errors**\n"
            f"Successful checks: {successful_checks}/{total_checks}\n\n"
        )

        for error in errors:
            error_message += f"• {error}\n"

        send_discord(error_message)

    # --------------------------------------------------------
    # Heartbeat
    # --------------------------------------------------------

    if successful_checks == total_checks and not new_sessions:

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        send_discord(
            f"✅ Cineplex monitor is running.\n"
            f"Checked {total_checks} theatre/date combinations.\n"
            f"No new {FILM_NAME} sessions found.\n"
            f"Last check: {timestamp}"
        )

    # --------------------------------------------------------
    # Save state
    # --------------------------------------------------------

    save_state(updated_state)

    print("\n" + "=" * 55)
    print("MONITOR COMPLETE")
    print(f"Successful checks: {successful_checks}/{total_checks}")
    print(f"New sessions: {len(new_sessions)}")
    print(f"Errors: {len(errors)}")
    print(f"Tracked sessions: {len(updated_state)}")
    print("=" * 55)


if __name__ == "__main__":
    main()
