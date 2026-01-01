"""
MCP Server with AUTO-AUTH for Google Calendar

AUTOMATIC AUTHENTICATION:
When user asks about calendar (list events, add event, etc.),
the server automatically checks auth status and prompts for login if needed.

NO session_id required in user prompts!

TOOLS:
1. get_weather - Get weather for a city
2. get_current_time - Get current time in a timezone
3. list_calendar_events - List upcoming events (auto-auth)
4. add_calendar_event - Add event (auto-auth)
5. google_auth_submit - Submit auth code (only when prompted)

FLOW:
1. User: "What's on my calendar?"
2. Server: "🔐 Please authorize first: [URL]. Then call google_auth_submit with your code"
3. User provides code
4. Server: ✅ Connected! Here are your events...
5. Future requests work automatically

Run: python servers/http_server.py
"""

import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

# Google Calendar imports
try:
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    GOOGLE_CALENDAR_AVAILABLE = True
except ImportError:
    GOOGLE_CALENDAR_AVAILABLE = False

# Logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mcp-http-server")

# Server config
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8000))

mcp = FastMCP(name="mcp-calendar-server", host=HOST, port=PORT)

# Google OAuth config
SCOPES = ["https://www.googleapis.com/auth/calendar"]
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

# =============================================================================
# MULTI-USER SESSION STORAGE (In-Memory, keyed by connection)
# =============================================================================

# Store credentials per user session in memory
# Key: session_id (from MCP context or generated), Value: session data
USER_SESSIONS: dict[str, dict] = {}

# Session timeout (hours) - clean up old sessions
SESSION_TIMEOUT_HOURS = 24


def get_session_id_from_context() -> str:
    """Get or create a session ID.

    In a real MCP deployment, this could come from:
    - HTTP headers (X-Session-ID)
    - MCP client session
    - Cookie

    For now, we use a simple approach with a default session
    that can be overridden by the user.
    """
    # You can enhance this to extract from MCP request context
    # For HTTP transport, you might use request headers
    return "default"


def get_session(session_id: str = None) -> dict:  # type: ignore
    """Get or create a user session."""
    if session_id is None:
        session_id = get_session_id_from_context()

    if session_id not in USER_SESSIONS:
        USER_SESSIONS[session_id] = {
            "credentials": None,
            "email": None,
            "pending_auth": None,
            "created_at": datetime.now(),
            "last_access": datetime.now(),
        }
    else:
        USER_SESSIONS[session_id]["last_access"] = datetime.now()

    return USER_SESSIONS[session_id]


def cleanup_old_sessions():
    """Remove expired sessions."""
    now = datetime.now()
    expired = [
        sid
        for sid, session in USER_SESSIONS.items()
        if (now - session.get("last_access", now)).total_seconds()
        > SESSION_TIMEOUT_HOURS * 3600
    ]
    for sid in expired:
        del USER_SESSIONS[sid]
        logger.info(f"Cleaned up expired session: {sid[:8]}...")


def get_user_count() -> int:
    """Get number of active user sessions."""
    cleanup_old_sessions()
    return len([s for s in USER_SESSIONS.values() if s.get("credentials")])


def get_credentials_path() -> Path:
    """Get credentials from environment variable or local file."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        temp_path = Path("/tmp/credentials.json")
        try:
            temp_path.write_text(creds_json)
            logger.info("Using GOOGLE_CREDENTIALS_JSON from environment")
            return temp_path
        except Exception as e:
            logger.error(f"Failed to write credentials: {e}")

    local_path = Path(__file__).parent.parent / "credentials.json"
    return local_path


CREDENTIALS_FILE = get_credentials_path()


def get_calendar_service(session_id: str = None):  # type: ignore
    """Get Google Calendar service if authenticated."""
    if not GOOGLE_CALENDAR_AVAILABLE:
        return None

    session = get_session(session_id)
    creds = session.get("credentials")
    if not creds:
        return None

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            session["credentials"] = creds
            logger.info("Refreshed expired token for session")
        except Exception as e:
            logger.error(f"Failed to refresh token: {e}")
            session["credentials"] = None
            return None

    try:
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Failed to build calendar service: {e}")
        return None


def is_authenticated(session_id: str = None) -> bool:  # type: ignore
    """Check if user is authenticated."""
    session = get_session(session_id)
    return session.get("credentials") is not None


def get_auth_prompt(session_id: str = None) -> str:  # type: ignore
    """Generate auth URL and return prompt for user."""
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "❌ Google Calendar not available. Install required packages."

    if not CREDENTIALS_FILE.exists():
        env_var = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if env_var:
            try:
                Path("/tmp/credentials.json").write_text(env_var)
            except:  # noqa: E722
                pass

        if not CREDENTIALS_FILE.exists() and not Path("/tmp/credentials.json").exists():
            return "❌ Google credentials not configured on server."

    creds_file = (
        CREDENTIALS_FILE if CREDENTIALS_FILE.exists() else Path("/tmp/credentials.json")
    )

    try:
        flow = Flow.from_client_secrets_file(
            str(creds_file), scopes=SCOPES, redirect_uri=REDIRECT_URI
        )
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

        # Generate unique session ID for this auth flow
        new_session_id = secrets.token_urlsafe(16)
        session = get_session(new_session_id)
        session["pending_auth"] = flow

        return f"""🔐 **Google Calendar Authorization Required**

To access your calendar, please:

**Step 1:** Open this URL in your browser:
{auth_url}

**Step 2:** Log into your Google account and grant permission

**Step 3:** Copy the authorization code shown

**Step 4:** Submit the code with your session ID:
```
google_auth_submit(session_id="{new_session_id}", code="YOUR_CODE_HERE")
```

📌 **Your Session ID:** `{new_session_id}`
(Save this - you'll need it to complete authentication!)"""

    except Exception as e:
        logger.error(f"Failed to create auth flow: {e}")
        return f"❌ Failed to start authentication: {e}"


# =============================================================================
# WEATHER HELPERS
# =============================================================================

GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"


async def get_coordinates(city: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        try:
            params = {"name": city, "count": 1, "language": "en", "format": "json"}
            response = await client.get(GEOCODING_API, params=params, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            if "results" in data and len(data["results"]) > 0:
                r = data["results"][0]
                return {
                    "latitude": r["latitude"],
                    "longitude": r["longitude"],
                    "name": r["name"],
                    "country": r.get("country", "Unknown"),
                }
            return None
        except Exception as e:
            logger.error(f"Geocoding error: {e}")
            return None


async def fetch_weather(lat: float, lon: float) -> dict | None:
    async with httpx.AsyncClient() as client:
        try:
            params = {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                "temperature_unit": "celsius",
            }
            response = await client.get(WEATHER_API, params=params, timeout=10.0)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Weather error: {e}")
            return None


def get_weather_description(code: int) -> str:
    codes = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Foggy",
        51: "Light drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        71: "Slight snow",
        73: "Moderate snow",
        75: "Heavy snow",
        95: "Thunderstorm",
    }
    return codes.get(code, "Unknown")


# =============================================================================
# WEATHER & TIME TOOLS (No auth needed)
# =============================================================================


@mcp.tool()
async def get_weather(city: str, country: str | None = None) -> str:
    """Get current weather for a city.

    Args:
        city: City name (e.g., "Tokyo", "London", "Jakarta")
        country: Optional country code (e.g., "JP", "UK", "ID")
    """
    location = await get_coordinates(city)
    if not location:
        return f"❌ Could not find: {city}"

    weather = await fetch_weather(location["latitude"], location["longitude"])
    if not weather:
        return f"❌ Could not fetch weather for {location['name']}"

    c = weather.get("current", {})
    return f"""🌤️ Weather for {location["name"]}, {location["country"]}:
• Temperature: {c.get("temperature_2m", "N/A")}°C
• Humidity: {c.get("relative_humidity_2m", "N/A")}%
• Conditions: {get_weather_description(c.get("weather_code", -1))}
• Wind: {c.get("wind_speed_10m", "N/A")} km/h"""


@mcp.tool()
def get_current_time(timezone: str = "UTC") -> str:
    """Get current date and time.

    Args:
        timezone: Timezone (e.g., "UTC", "Asia/Jakarta", "US/Eastern")
    """
    try:
        tz = ZoneInfo(timezone)
        now = datetime.now(tz)
        return f"""🕐 Current Time ({timezone}):
• Date: {now.strftime("%A, %B %d, %Y")}
• Time: {now.strftime("%I:%M:%S %p")}
• ISO: {now.isoformat()}"""
    except Exception:
        now = datetime.now(ZoneInfo("UTC"))
        return (
            f"⚠️ Invalid timezone '{timezone}'. UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )


# =============================================================================
# AUTH TOOLS
# =============================================================================


@mcp.tool()
def google_auth_submit(session_id: str, code: str) -> str:
    """Complete Google Calendar authentication by submitting the authorization code.

    Call this after user has:
    1. Received auth URL from a calendar tool (list/add/delete)
    2. Visited the authorization URL
    3. Logged into their Google account
    4. Copied the authorization code

    Args:
        session_id: The session ID provided in the auth prompt
        code: The authorization code the user copied from Google
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "❌ Google Calendar not available"

    session = get_session(session_id)
    flow = session.get("pending_auth")

    # If no pending flow, create one
    if not flow:
        creds_file = (
            CREDENTIALS_FILE
            if CREDENTIALS_FILE.exists()
            else Path("/tmp/credentials.json")
        )
        if not creds_file.exists():
            return "❌ No pending authorization. Please call connect_google_calendar first."

        try:
            flow = Flow.from_client_secrets_file(
                str(creds_file), scopes=SCOPES, redirect_uri=REDIRECT_URI
            )
        except Exception as e:
            return f"❌ Failed to create auth flow: {e}"

    try:
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Store credentials in session
        session["credentials"] = credentials
        session["pending_auth"] = None

        # Get user email
        service = build("calendar", "v3", credentials=credentials)
        calendar = service.calendars().get(calendarId="primary").execute()
        email = calendar.get("summary", "Your Calendar")
        session["email"] = email

        logger.info(f"User authenticated: {email} (session: {session_id[:8]}...)")

        return f"""✅ **Successfully Connected!**

📧 Calendar: {email}
🔑 Session ID: `{session_id}`

**Save your session ID!** You'll need it for calendar operations.

Now you can use:
• `list_calendar_events(session_id="{session_id}")`
• `add_calendar_event(session_id="{session_id}", title="...", ...)`

Or just tell me what you want to do with your calendar!"""

    except Exception as e:
        logger.error(f"Auth callback error: {e}")
        return f"""❌ **Authorization failed**

Error: {str(e)}

Please call connect_google_calendar to get a new authorization link."""


@mcp.tool()
def google_auth_status(session_id: str) -> str:
    """Check Google Calendar authentication status for a session.

    Args:
        session_id: Your session ID
    """
    session = get_session(session_id)

    if session.get("credentials"):
        email = session.get("email", "Connected")
        return f"""✅ **Authenticated**

📧 Calendar: {email}
🔑 Session: `{session_id}`

Your Google Calendar is connected and ready!"""
    else:
        return f"""❌ **Not authenticated**

Session `{session_id}` is not connected to Google Calendar.
Request calendar access to get started!"""


@mcp.tool()
def google_auth_logout(session_id: str) -> str:
    """Disconnect Google Calendar for a session.

    Args:
        session_id: Your session ID
    """
    if session_id in USER_SESSIONS:
        email = USER_SESSIONS[session_id].get("email", "Unknown")
        del USER_SESSIONS[session_id]
        logger.info(f"User logged out: {email}")
        return f"""✅ **Logged out**

Session `{session_id}` has been disconnected.
Request calendar access anytime to reconnect!"""
    else:
        return f"⚠️ Session `{session_id}` not found."


# =============================================================================
# CALENDAR TOOLS (Auto-auth - no session_id needed!)
# =============================================================================


@mcp.tool()
def list_calendar_events(
    max_results: int = 10,
    time_min: str | None = None,
    time_max: str | None = None,
    session_id: str = "",
) -> str:
    """List upcoming Google Calendar events.

    Just call this tool to see calendar events. Authentication is handled automatically.
    If not yet authenticated, will return instructions to connect Google Calendar.

    Args:
        max_results: Max events to return (1-50, default 10)
        time_min: Optional start date/time filter (ISO format, e.g., "2025-01-15")
        time_max: Optional end date/time filter (ISO format)
        session_id: (Auto-managed) Leave empty for new auth, or provide existing session
    """
    # Auto-trigger auth if no session or not authenticated
    if not session_id or not is_authenticated(session_id):
        return get_auth_prompt()

    service = get_calendar_service(session_id)
    if not service:
        return get_auth_prompt()

    try:
        if not time_min:
            time_min = datetime.now(ZoneInfo("UTC")).isoformat()
        if not time_min.endswith("Z") and "+" not in time_min:
            time_min += "Z"

        params = {
            "calendarId": "primary",
            "timeMin": time_min,
            "maxResults": min(max(1, max_results), 50),
            "singleEvents": True,
            "orderBy": "startTime",
        }

        if time_max:
            if not time_max.endswith("Z") and "+" not in time_max:
                time_max += "Z"
            params["timeMax"] = time_max

        events = service.events().list(**params).execute().get("items", [])

        if not events:
            return "📅 No upcoming events found."

        session = get_session(session_id)
        email = session.get("email", "Your Calendar")

        result = (
            f"📅 **{email}** - Upcoming Events ({len(events)}):\n" + "=" * 40 + "\n"
        )

        for i, e in enumerate(events, 1):
            start = e["start"].get("dateTime", e["start"].get("date"))
            if "T" in start:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                start_str = dt.strftime("%b %d, %Y %I:%M %p")
            else:
                start_str = f"{start} (All day)"

            result += f"\n{i}. **{e.get('summary', '(No title)')}**\n"
            result += f"   📆 {start_str}\n"
            if e.get("location"):
                result += f"   📍 {e['location']}\n"
            result += f"   🆔 `{e.get('id', 'N/A')}`\n"

        return result

    except HttpError as e:
        logger.error(f"Calendar API error: {e}")
        return f"❌ Calendar error: {e.reason}"
    except Exception as e:
        logger.error(f"Error listing events: {e}")
        return f"❌ Error: {e}"


@mcp.tool()
def add_calendar_event(
    title: str,
    start_time: str,
    end_time: str,
    description: str | None = None,
    location: str | None = None,
    timezone: str = "Asia/Jakarta",
    attendees: str | None = None,
    session_id: str = "",
) -> str:
    """Add a new event to Google Calendar.

    Just call this tool to create an event. Authentication is handled automatically.
    If not yet authenticated, will return instructions to connect Google Calendar.

    Args:
        title: Event title (e.g., "Team Meeting", "Lunch with Bob")
        start_time: Start time - ISO format "2025-01-15T14:00:00" or date "2025-01-15" for all-day
        end_time: End time - ISO format "2025-01-15T15:00:00" or date "2025-01-16" for all-day
        description: Optional event description/notes
        location: Optional location (e.g., "Conference Room A", "Zoom")
        timezone: Timezone for the event (default: Asia/Jakarta)
        attendees: Optional comma-separated email addresses to invite
        session_id: (Auto-managed) Leave empty for new auth, or provide existing session
    """
    # Auto-trigger auth if no session or not authenticated
    if not session_id or not is_authenticated(session_id):
        return get_auth_prompt()

    service = get_calendar_service(session_id)
    if not service:
        return get_auth_prompt()

    try:
        is_all_day = "T" not in start_time
        event = {"summary": title}

        if is_all_day:
            event["start"] = {"date": start_time}  # type: ignore
            event["end"] = {"date": end_time}  # type: ignore
        else:
            event["start"] = {"dateTime": start_time, "timeZone": timezone}  # type: ignore
            event["end"] = {"dateTime": end_time, "timeZone": timezone}  # type: ignore

        if description:
            event["description"] = description
        if location:
            event["location"] = location
        if attendees:
            event["attendees"] = [  # type: ignore
                {"email": e.strip()} for e in attendees.split(",") if e.strip()
            ]

        created = (
            service.events()
            .insert(
                calendarId="primary",
                body=event,
                sendUpdates="all" if attendees else "none",
            )
            .execute()
        )

        return f"""✅ **Event Created!**

📅 **{title}**
⏰ {start_time} → {end_time}
🌍 Timezone: {timezone}
{f"📍 Location: {location}" if location else ""}
{f"📝 Description: {description}" if description else ""}
{f"👥 Attendees: {attendees}" if attendees else ""}

🔗 {created.get("htmlLink", "")}
🆔 Event ID: `{created.get("id", "")}`"""

    except HttpError as e:
        logger.error(f"Calendar API error: {e}")
        return f"❌ Calendar error: {e.reason}"
    except Exception as e:
        logger.error(f"Error creating event: {e}")
        return f"❌ Error: {e}"


@mcp.tool()
def delete_calendar_event(event_id: str, session_id: str = "") -> str:
    """Delete an event from Google Calendar.

    Just call this tool to delete an event. Authentication is handled automatically.
    If not yet authenticated, will return instructions to connect Google Calendar.

    Args:
        event_id: The event ID to delete (get this from list_calendar_events)
        session_id: (Auto-managed) Leave empty for new auth, or provide existing session
    """
    # Auto-trigger auth if no session or not authenticated
    if not session_id or not is_authenticated(session_id):
        return get_auth_prompt()

    service = get_calendar_service(session_id)
    if not service:
        return get_auth_prompt()

    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return f"✅ Event `{event_id}` deleted successfully!"
    except HttpError as e:
        return f"❌ Failed to delete event: {e.reason}"
    except Exception as e:
        return f"❌ Error: {e}"


# =============================================================================
# RESOURCES
# =============================================================================


@mcp.resource("config://settings")
def get_config() -> str:
    cleanup_old_sessions()
    return json.dumps(
        {
            "version": "3.0.0",
            "multi_user": True,
            "google_calendar": GOOGLE_CALENDAR_AVAILABLE,
            "active_users": get_user_count(),
            "total_sessions": len(USER_SESSIONS),
            "session_timeout_hours": SESSION_TIMEOUT_HOURS,
        },
        indent=2,
    )


@mcp.resource("calendar://help")
def get_calendar_help() -> str:
    return """# Multi-User Google Calendar

## How It Works:

1. **Request Calendar Access**
   Ask: "Show my calendar" or "What events do I have?"
   
2. **Get Your Session ID**
   You'll receive a unique session ID and authorization URL.
   
3. **Authorize**
   - Open the URL in your browser
   - Login with YOUR Google account
   - Copy the authorization code
   
4. **Submit Code**
   Call: google_auth_submit(session_id="YOUR_ID", code="YOUR_CODE")
   
5. **Use Calendar**
   - list_calendar_events(session_id="YOUR_ID")
   - add_calendar_event(session_id="YOUR_ID", title="...", ...)

## Each User Gets Their Own Session!
Multiple users can connect their own calendars simultaneously.
"""


# =============================================================================
# PROMPTS
# =============================================================================


@mcp.prompt()
def daily_schedule(session_id: str = "") -> str:
    """Get today's schedule."""
    return f"""Please show me my calendar events for today.

Session ID: {session_id if session_id else "(not provided - will need to authenticate)"}

1. Use get_current_time to find today's date
2. Use list_calendar_events with the session_id to get today's events
3. Summarize the schedule"""


@mcp.prompt()
def schedule_meeting(session_id: str, title: str, duration_minutes: int = 60) -> str:
    """Help schedule a new meeting."""
    return f"""Help me schedule: "{title}" ({duration_minutes} minutes)

Session ID: {session_id}

1. First check my calendar for available times using list_calendar_events
2. Suggest good time slots
3. When I confirm, create the event using add_calendar_event"""


# =============================================================================
# MAIN
# =============================================================================


def main():
    logger.info(f"Starting MCP Server on http://{HOST}:{PORT}/mcp")
    logger.info("Auto-auth enabled - users just ask about calendar!")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
