
import os
import json
import requests

from pathlib import Path
from datetime import datetime

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

EXCLUDED_EXPERIENCES = {
    "3D",
    "ScreenX",
}

STATE_FILE = Path("state.json")

API_KEY = os.getenv("CINEPLEX_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

if not API_KEY:
    raise RuntimeError("Missing CINEPLEX_API_KEY.")

if not DISCORD_WEBHOOK:
    raise RuntimeError("Missing DISCORD_WEBHOOK.")

HEADERS = {
    "Ocp-Apim-Subscription-Key": API_KEY,
    "Accept": "application/json",
}


# ============================================================
# DISCORD
# ============================================================

def send_discord(message):
    """Send a Discord message, splitting long messages."""

    chunks = []

    while len(message) > 1900:
        split_at = message.rfind("\n", 0, 1900)

        if split_at == -1:
            split_at = 1900

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
            print(f"Discord error: {error}")


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    """Load previously detected showtimes."""

    if not STATE_FILE.exists():
        return set()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict):
            if isinstance(data.get("seen"), list):
                return set(str(x) for x in data["seen"])

            return set(str(x) for x in data.keys())

        if isinstance(data, list):
            return set(str(x) for x in data)

    except (json.JSONDecodeError, OSError) as error:
        print(f"Could not load state: {error}")

    return set()


def save_state(seen_sessions):
    """Save tracked showtimes."""

    data = {
        "seen": sorted(seen_sessions)
    }

    temporary_file = STATE_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    temporary_file.replace(STATE_FILE)


# ============================================================
# CINEPLEX API
# ============================================================

def get_showtimes(theatre_id, date):

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
# EXPERIENCE FILTERING
# ============================================================

def normalize_tag(tag):
    """Normalize experience names for comparison."""

    return "".join(
        character.lower()
        for character in str(tag)
        if character.isalnum()
    )


def is_excluded(experience_types):
    """
    Exclude a session if ANY of its experience tags
    matches an excluded experience.
    """

    excluded = {
        normalize_tag(tag)
        for tag in EXCLUDED_EXPERIENCES
    }

    for tag in experience_types:

        normalized = normalize_tag(tag)

        for excluded_tag in excluded:

            if excluded_tag in normalized:
                return True

    return False


# ============================================================
# TIME FORMATTING
# ============================================================

def format_showtime(date_string):
    """
    Convert Cineplex local datetime into readable time.

    Example:
    2026-12-17T14:50:00 -> 2:50 PM
    """

    try:
        showtime = datetime.fromisoformat(date_string)
        return showtime.strftime("%I:%M %p").lstrip("0")

    except (ValueError, TypeError):
        return str(date_string)


# ============================================================
# SESSION EXTRACTION
# ============================================================

def extract_sessions(response_data):
    """
    Parse Cineplex's actual response structure:

    theatre
      -> dates
        -> movies
          -> experiences
            -> experienceTypes
            -> sessions

    Duplicate sessions are grouped by vistaSessionId.
    All experience tags are combined BEFORE filtering.
    """

    grouped_sessions = {}

    # Cineplex response is normally a list of theatres.
    theatres = response_data if isinstance(response_data, list) else [response_data]

    for theatre in theatres:

        for date_info in theatre.get("dates", []):

            for movie in date_info.get("movies", []):

                # Make sure we are processing the requested movie.
                if str(movie.get("id")) != str(FILM_ID):
                    continue

                for experience in movie.get("experiences", []):

                    # IMPORTANT:
                    # experienceTypes belongs to the parent experience,
                    # not to the individual session.
                    experience_types = experience.get(
                        "experienceTypes", []
                    )

                    for session in experience.get("sessions", []):

                        session_id = session.get("vistaSessionId")

                        if session_id is None:
                            continue

                        session_id = str(session_id)

                        if session_id not in grouped_sessions:

                            grouped_sessions[session_id] = {
                                "session_id": session_id,
                                "experience_types": set(),
                                "session": session,
                            }

                        grouped = grouped_sessions[session_id]

                        # Combine tags from every duplicate record.
                        grouped["experience_types"].update(
                            experience_types
                        )

                        # Keep a session record with useful links.
                        current = grouped["session"]

                        if not current.get("ticketingUrl") and session.get("ticketingUrl"):
                            current["ticketingUrl"] = session["ticketingUrl"]

                        if not current.get("seatMapUrl") and session.get("seatMapUrl"):
                            current["seatMapUrl"] = session["seatMapUrl"]

    results = []

    for session_id, grouped in grouped_sessions.items():

        session = grouped["session"]

        experience_types = sorted(
            grouped["experience_types"]
        )

        # Filter AFTER collecting all experience tags.
        if is_excluded(experience_types):

            print(
                f"EXCLUDED: {session_id} | "
                f"Experiences: {experience_types}"
            )

            continue

        raw_time = session.get("showStartDateTime")

        results.append({
            "session_id": session_id,

            "time": format_showtime(raw_time),

            "experience_types": experience_types,

            "seats_remaining": session.get("seatsRemaining"),

            "is_sold_out": session.get("isSoldOut", False),

            "auditorium": session.get("auditorium"),

            "seat_map_url": session.get("seatMapUrl"),

            "ticketing_url": session.get("ticketingUrl"),

        })

        print(
            f"ELIGIBLE: {session_id} | "
            f"{format_showtime(raw_time)} | "
            f"{experience_types}"
        )

    return results


# ============================================================
# DISCORD MESSAGE FORMATTING
# ============================================================

def format_session_message(theatre_name, date, session):

    experiences = ", ".join(
        session["experience_types"]
    ) or "Not specified"

    message = (
        f"🎬 **NEW {FILM_NAME} SHOWTIME FOUND!**\n"
        f"📍 **Theatre:** {theatre_name}\n"
        f"📅 **Date:** {date}\n"
        f"🕒 **Time:** {session['time']}\n"
        f"🎥 **Experience:** {experiences}\n"
    )

    if session["auditorium"]:
        message += f"🏢 **Auditorium:** {session['auditorium']}\n"

    if session["seats_remaining"] is not None:
        message += (
            f"💺 **Seats remaining:** "
            f"{session['seats_remaining']}\n"
        )

    if session["is_sold_out"]:
        message += "⚠️ **Marked as sold out**\n"

    if session["seat_map_url"]:
        message += (
            f"🗺️ [View Seat Map]"
            f"({session['seat_map_url']})\n"
        )

    if session["ticketing_url"]:
        message += (
            f"🎟️ [Book Tickets]"
            f"({session['ticketing_url']})\n"
        )

    message += (
        f"🆔 Session ID: `{session['session_id']}`"
    )

    return message


# ============================================================
# MAIN MONITOR
# ============================================================

def main():

    print("=" * 55)
    print(f"CINEPLEX MONITOR: {FILM_NAME}")
    print(f"Theatres: {len(THEATRES)}")
    print(f"Dates: {len(DATES)}")
    print(f"API requests: {len(THEATRES) * len(DATES)}")
    print(f"Excluded: {EXCLUDED_EXPERIENCES}")
    print("=" * 55)

    seen_sessions = load_state()
    updated_state = set(seen_sessions)

    new_sessions = []
    errors = []

    successful_checks = 0
    total_checks = len(THEATRES) * len(DATES)

    # --------------------------------------------------------
    # Check all theatres and dates
    # --------------------------------------------------------

    for theatre_id, theatre_name in THEATRES.items():

        for date in DATES:

            print(f"\nChecking {theatre_name} | {date}")

            try:

                response_data = get_showtimes(
                    theatre_id,
                    date,
                )

                sessions = extract_sessions(response_data)

                successful_checks += 1

                print(
                    f"Eligible sessions found: {len(sessions)}"
                )

                for session in sessions:

                    state_key = (
                        f"{theatre_id}|"
                        f"{date}|"
                        f"{session['session_id']}"
                    )

                    if state_key in seen_sessions:
                        continue

                    print(
                        f"NEW SESSION: "
                        f"{session['session_id']} "
                        f"at {session['time']}"
                    )

                    updated_state.add(state_key)

                    new_sessions.append({
                        "theatre": theatre_name,
                        "date": date,
                        "session": session,
                    })

            except Exception as error:

                error_message = (
                    f"{theatre_name} | {date}: {error}"
                )

                print(f"ERROR: {error_message}")
                errors.append(error_message)

    # --------------------------------------------------------
    # Send new showtime notifications
    # --------------------------------------------------------

    if new_sessions:

        print(f"\nNew sessions: {len(new_sessions)}")

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
    # Send error notification
    # --------------------------------------------------------

    if errors:

        message = (
            "⚠️ **Cineplex Monitor Errors**\n"
            f"Successful checks: {successful_checks}/{total_checks}\n\n"
        )

        for error in errors:
            message += f"• {error}\n"

        send_discord(message)

    # --------------------------------------------------------
    # Heartbeat
    # --------------------------------------------------------

    if successful_checks == total_checks and not new_sessions:

        timestamp = datetime.utcnow().strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )

        send_discord(
            "✅ **Cineplex Monitor Running**\n"
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
