
import os
import json
import requests
from pathlib import Path
from datetime import datetime

API_KEY = os.environ["CINEPLEX_API_KEY"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

THEATRE_ID = 7411
FILM_ID = 34362
DATE = "12/17/2026"

STATE_FILE = Path("state.json")

API_URL = (
    "https://apis.cineplex.com/prod/cpx/theatrical/api/v1/showtimes"
)

def send_discord(message):
    response = requests.post(
        DISCORD_WEBHOOK,
        json={"content": message},
        timeout=20
    )
    response.raise_for_status()

def get_showtimes():
    response = requests.get(
        API_URL,
        params={
            "language": "en",
            "locationId": THEATRE_ID,
            "date": DATE,
            "filmId": FILM_ID
        },
        headers={
            "Ocp-Apim-Subscription-Key": API_KEY,
            "Accept": "application/json"
        },
        timeout=30
    )

    if response.status_code == 204:
        return []

    response.raise_for_status()
    data = response.json()

    sessions = {}

    for theatre in data:
        for date in theatre.get("dates", []):
            for movie in date.get("movies", []):
                for experience in movie.get("experiences", []):
                    experience_types = experience.get(
                        "experienceTypes", []
                    )

                    for session in experience.get("sessions", []):
                        session_id = str(
                            session.get("vistaSessionId")
                        )

                        if not session_id or session_id == "None":
                            continue

                        if session_id not in sessions:
                            sessions[session_id] = {
                                "id": session_id,
                                "time": session.get(
                                    "showStartDateTime"
                                ),
                                "auditorium": session.get(
                                    "auditorium", "Unknown"
                                ),
                                "seats": session.get(
                                    "seatsRemaining"
                                ),
                                "sold_out": session.get(
                                    "isSoldOut", False
                                ),
                                "ticket_url": session.get(
                                    "ticketingUrl"
                                ),
                                "seat_map_url": session.get(
                                    "seatMapUrl"
                                ),
                                "experiences": set()
                            }

                        sessions[session_id]["experiences"].update(
                            experience_types
                        )

    return list(sessions.values())

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

def save_state(sessions):
    state = {
        session["id"]: True
        for session in sessions
    }

    STATE_FILE.write_text(
        json.dumps(state, indent=2)
    )

def format_session(session):
    time = datetime.fromisoformat(
        session["time"]
    ).strftime("%I:%M %p").lstrip("0")

    experiences = ", ".join(
        sorted(session["experiences"])
    )

    return (
        f"• **{time}** — {experiences}\n"
        f"  Auditorium: {session['auditorium']}\n"
        f"  Seats remaining: {session['seats']}\n"
        f"  [View seats]({session['seat_map_url']})"
    )

def main():
    try:
        sessions = get_showtimes()

        previous_state = load_state()

        new_sessions = [
            session for session in sessions
            if session["id"] not in previous_state
        ]

        if new_sessions:
            message = (
                "🎬 **New Cineplex showtimes detected!**\n"
                "**Avengers: Doomsday**\n"
                "**SilverCity Brampton Cinemas**\n\n"
            )

            message += "\n\n".join(
                format_session(session)
                for session in new_sessions
            )

            send_discord(message)
        else:
            send_discord(
                "✅ **Cineplex Monitor**\n"
                "No new showtimes detected."
            )

        save_state(sessions)

    except Exception as error:
        try:
            send_discord(
                "⚠️ **Cineplex Monitor Error**\n"
                f"```{str(error)[:1500]}```"
            )
        finally:
            raise

if __name__ == "__main__":
    main()
