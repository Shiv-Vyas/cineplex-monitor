
import os
import json
import requests

from pathlib import Path
from datetime import datetime


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://apis.cineplex.com/prod/cpx/theatrical/api/v1"

THEATRES = {
    7411: "SilverCity Brampton Cinemas",
    7122: "Cineplex Courtney Park",
    7420: "Cineplex Square One",
    7123: "Cineplex Winston Churchill",
    7285: "Cineplex Milton"
}

DATES = [
    "12/17/2026",
    "12/18/2026",
    "12/19/2026",
    "12/20/2026",
]

FILM_ID = 34362
FILM_NAME = "Avengers: Doomsday"

EXCLUDED_EXPERIENCES = {"3D", "ScreenX"}

# Discord users to mention when new showtimes are found
DISCORD_USER_IDS = [
    "213852031578800139",
    "237689413080055808",
    "279023899931574273",
    "1013321941349830757",
    "104669251507355648"
]

CINEPLEX_API_KEY = os.getenv("CINEPLEX_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

STATE_FILE = Path("state.json")

MAX_DISCORD_LENGTH = 1900


# ============================================================
# DISCORD
# ============================================================

def send_discord(message, ping_users=False):

    payload = {
        "content": message,
        "allowed_mentions": {
            "parse": []
        }
    }

    if ping_users and DISCORD_USER_IDS:
        mentions = " ".join(
            f"<@{user_id}>" for user_id in DISCORD_USER_IDS
        )

        payload["content"] = f"{mentions}\n{message}"

        payload["allowed_mentions"] = {
            "users": DISCORD_USER_IDS
        }

    response = requests.post(
        DISCORD_WEBHOOK,
        json=payload,
        timeout=15
    )

    response.raise_for_status()


def send_discord_chunks(messages, ping_users=False):
    """Send multiple message blocks without exceeding Discord's limit."""

    if not messages:
        return

    chunks = []
    current_chunk = ""

    for message in messages:

        if len(current_chunk) + len(message) + 2 > MAX_DISCORD_LENGTH:

            if current_chunk:
                chunks.append(current_chunk)

            current_chunk = message

        else:

            if current_chunk:
                current_chunk += "\n\n"

            current_chunk += message

    if current_chunk:
        chunks.append(current_chunk)

    # Mention users only in the first message of the batch.
    for index, chunk in enumerate(chunks):
        send_discord(
            chunk,
            ping_users=ping_users and index == 0
        )


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():

    if not STATE_FILE.exists():
        return {"seen": []}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        if "seen" not in state:
            state["seen"] = []

        return state

    except (json.JSONDecodeError, OSError):
        print("Could not read state.json. Starting with empty state.")
        return {"seen": []}


def save_state(state):

    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=4)


# ============================================================
# EXPERIENCE FILTERING
# ============================================================

def normalize_experience(experience):

    return "".join(
        character.lower()
        for character in str(experience)
        if character.isalnum()
    )


def is_excluded(experience_types):

    excluded = {
        normalize_experience(item)
        for item in EXCLUDED_EXPERIENCES
    }

    for experience in experience_types:

        normalized = normalize_experience(experience)

        for excluded_type in excluded:

            if excluded_type in normalized:
                return True

    return False


def get_experience_types(experience):

    types = experience.get("experienceTypes", [])

    if isinstance(types, str):
        return [types]

    if isinstance(types, list):
        return types

    return []


# ============================================================
# CINEPLEX API
# ============================================================

def get_showtimes(theatre_id, date):

    url = f"{BASE_URL}/showtimes"

    headers = {
        "Ocp-Apim-Subscription-Key": CINEPLEX_API_KEY
    }

    params = {
        "language": "en",
        "locationId": theatre_id,
        "date": date,
        "filmId": FILM_ID,
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# EXTRACT AND GROUP SESSIONS
# ============================================================

def extract_sessions(data):

    grouped_sessions = {}

    if isinstance(data, dict):
        data = [data]

    if not isinstance(data, list):
        return []

    for theatre in data:

        for date_info in theatre.get("dates", []):

            for movie in date_info.get("movies", []):

                if movie.get("id") != FILM_ID:
                    continue

                for experience in movie.get("experiences", []):

                    experience_types = get_experience_types(
                        experience
                    )

                    for session in experience.get("sessions", []):

                        session_id = session.get("vistaSessionId")

                        if session_id is None:
                            continue

                        session_id = str(session_id)

                        if session_id not in grouped_sessions:

                            grouped_sessions[session_id] = {
                                "session": session.copy(),
                                "experience_types": []
                            }

                        grouped = grouped_sessions[session_id]

                        # Combine experience types from duplicate
                        # occurrences of the same session.
                        for experience_type in experience_types:

                            if experience_type not in grouped["experience_types"]:
                                grouped["experience_types"].append(
                                    experience_type
                                )

                        # Fill in missing links from duplicate entries.
                        for field in [
                            "seatMapUrl",
                            "deeplinkUrl",
                            "ticketingUrl"
                        ]:

                            if (
                                not grouped["session"].get(field)
                                and session.get(field)
                            ):
                                grouped["session"][field] = session[field]

    results = []

    for session_id, grouped in grouped_sessions.items():

        session = grouped["session"]
        experience_types = grouped["experience_types"]

        # Exclude the entire session if any associated experience
        # contains an excluded type.
        if is_excluded(experience_types):
            continue

        session["experienceTypes"] = experience_types

        results.append(session)

    return results


# ============================================================
# FORMATTING
# ============================================================

def format_showtime(session):

    start_time = session.get("showStartDateTime")

    if not start_time:
        return None, None

    try:
        show_datetime = datetime.fromisoformat(start_time)

    except ValueError:
        return None, None

    formatted_date = show_datetime.strftime(
        "%A, %B %d, %Y"
    )

    formatted_time = show_datetime.strftime(
        "%I:%M %p"
    ).lstrip("0")

    return formatted_date, formatted_time


def create_notification(theatre_name, session):

    date, time = format_showtime(session)

    if not date or not time:
        return None

    experience_types = session.get("experienceTypes", [])

    experience_text = ", ".join(experience_types)

    message = (
        f"🎬 **NEW {FILM_NAME} SHOWTIME FOUND!**\n"
        f"📍 Theatre: {theatre_name}\n"
        f"📅 Date: {date}\n"
        f"🕒 Time: {time}\n"
        f"🎥 Experience: {experience_text}"
    )

    seat_map_url = session.get("seatMapUrl")
    booking_url = session.get("deeplinkUrl")

    if seat_map_url:
        message += f"\n🗺️ [View Seat Map]({seat_map_url})"

    if booking_url:
        message += f"\n🎟️ [Book Tickets in Cineplex App]({booking_url})"

    return message


# ============================================================
# MAIN MONITOR
# ============================================================

def main():

    if not CINEPLEX_API_KEY:
        raise RuntimeError("Missing CINEPLEX_API_KEY environment variable.")

    if not DISCORD_WEBHOOK:
        raise RuntimeError("Missing DISCORD_WEBHOOK environment variable.")

    state = load_state()

    seen_sessions = set(
        str(session_id)
        for session_id in state.get("seen", [])
    )

    new_sessions = []
    errors = []

    total_checks = 0
    successful_checks = 0

    print(f"Starting {FILM_NAME} monitor...")

    for theatre_id, theatre_name in THEATRES.items():

        for date in DATES:

            total_checks += 1

            print(
                f"Checking {theatre_name} - {date}"
            )

            try:

                data = get_showtimes(theatre_id, date)

                successful_checks += 1

                sessions = extract_sessions(data)

                for session in sessions:

                    session_id = str(
                        session.get("vistaSessionId")
                    )

                    if session_id in seen_sessions:
                        continue

                    notification = create_notification(
                        theatre_name,
                        session
                    )

                    # Mark the session as seen even if formatting
                    # fails, preventing repeated processing.
                    seen_sessions.add(session_id)

                    if notification:

                        new_sessions.append(notification)

                        print(
                            f"NEW SHOWTIME: {theatre_name} - "
                            f"{session_id}"
                        )

            except Exception as error:

                error_message = (
                    f"Error checking {theatre_name} "
                    f"on {date}: {error}"
                )

                print(error_message)
                errors.append(error_message)

    # ========================================================
    # SEND NEW SHOWTIME NOTIFICATIONS
    # ========================================================

    if new_sessions:

        print(
            f"Found {len(new_sessions)} new showtime(s)."
        )

        send_discord_chunks(
            new_sessions,
            ping_users=True
        )

    # ========================================================
    # SEND ERRORS
    # ========================================================

    if errors:

        error_message = (
            "⚠️ **Cineplex Monitor Errors**\n\n"
            + "\n".join(errors)
        )

        send_discord(error_message)

    # ========================================================
    # HEARTBEAT
    # ========================================================

    if (
        successful_checks == total_checks
        and not new_sessions
    ):

        heartbeat_message = (
            "💚 **Cineplex Monitor Heartbeat**\n"
            "All theatre and date checks completed successfully.\n"
            "No new showtimes found."
        )

        send_discord(heartbeat_message)

        print("Heartbeat sent.")

    # ========================================================
    # SAVE STATE
    # ========================================================

    state["seen"] = sorted(seen_sessions)

    save_state(state)

    print(
        f"Monitoring complete. "
        f"{successful_checks}/{total_checks} checks successful. "
        f"{len(new_sessions)} new showtime(s)."
    )


if __name__ == "__main__":
    main()
