"""
MCP Server using Streamable HTTP Transport - Multi-User OAuth Support

MULTI-USER SUPPORT:
Each user authorizes their own Google Calendar. Tokens are stored per-session.

TOOLS:
1. get_weather - Get weather information for a city
2. get_current_time - Get current time in a specified timezone
3. google_auth_start - Start Google OAuth flow (returns auth URL)
4. google_auth_callback - Complete OAuth with authorization code
5. google_auth_status - Check authentication status
6. google_auth_logout - Clear session and logout
7. add_calendar_event - Add event to YOUR Google Calendar
8. list_calendar_events - List YOUR upcoming events

Multi-User OAuth Flow:
1. User calls google_auth_start → Gets auth URL + session_id
2. User visits URL → Logs into THEIR Google account → Grants permission
3. User copies authorization code from Google
4. User calls google_auth_callback with session_id + code → Token stored
5. User uses session_id for all calendar operations

Run: python servers/http_server.py
Server: http://localhost:8000/mcp
"""

import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

# Google Calendar imports
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
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

mcp = FastMCP(name="mcp-multiuser-calendar", host=HOST, port=PORT)

# Google OAuth config
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = Path(__file__).parent.parent / "credentials.json"
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"  # Manual code copy-paste

# Session storage
USER_SESSIONS: dict[str, dict] = {}
SESSION_EXPIRY_HOURS = 24

# Weather API
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"


# ============================================================================
# SESSION MANAGEMENT
# ============================================================================


def generate_session_id() -> str:
    return secrets.token_urlsafe(32)


def store_user_credentials(session_id: str, credentials: Credentials) -> None:
    USER_SESSIONS[session_id] = {
        "credentials": credentials,
        "created_at": datetime.now(),
    }
    logger.info(f"Stored credentials for session {session_id[:8]}...")


def get_user_credentials(session_id: str) -> Optional[Credentials]:
    if session_id not in USER_SESSIONS:
        return None

    session = USER_SESSIONS[session_id]
    created_at = session.get("created_at", datetime.now())

    if datetime.now() - created_at > timedelta(hours=SESSION_EXPIRY_HOURS):
        del USER_SESSIONS[session_id]
        return None

    credentials = session.get("credentials")

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            session["credentials"] = credentials
        except Exception as e:
            logger.error(f"Failed to refresh: {e}")
            del USER_SESSIONS[session_id]
            return None

    return credentials


def get_calendar_service(session_id: str):
    if not GOOGLE_CALENDAR_AVAILABLE:
        return None
    credentials = get_user_credentials(session_id)
    if not credentials:
        return None
    try:
        return build("calendar", "v3", credentials=credentials)
    except Exception as e:
        logger.error(f"Error building service: {e}")
        return None


# ============================================================================
# WEATHER HELPERS
# ============================================================================


async def get_coordinates(city: str, country: str | None = None) -> dict | None:
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


# ============================================================================
# AUTH TOOLS
# ============================================================================


@mcp.tool()
def google_auth_start() -> str:
    """Start Google OAuth - Get authorization URL to connect YOUR Google Calendar.

    Returns authorization URL and session_id. Visit the URL, login with YOUR
    Google account, copy the code, then call google_auth_callback.
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "❌ Google Calendar not available. Install: pip install google-auth google-auth-oauthlib google-api-python-client"

    if not CREDENTIALS_FILE.exists():
        return f"❌ credentials.json not found at {CREDENTIALS_FILE}"

    try:
        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_FILE), scopes=SCOPES, redirect_uri=REDIRECT_URI
        )
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        session_id = generate_session_id()

        return f"""🔐 **Connect Your Google Calendar**

**Step 1:** Open this URL in your browser:
{auth_url}

**Step 2:** Log into YOUR Google account and grant permission

**Step 3:** Copy the authorization code shown

**Step 4:** Call google_auth_callback with:
```
session_id: {session_id}
code: <paste your code here>
```

📌 **Save this Session ID:** `{session_id}`
You'll need it for all calendar operations!"""
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def google_auth_callback(session_id: str, code: str) -> str:
    """Complete OAuth by providing the authorization code from Google.

    Args:
        session_id: Session ID from google_auth_start
        code: Authorization code copied from Google
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "❌ Google Calendar not available"

    try:
        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_FILE), scopes=SCOPES, redirect_uri=REDIRECT_URI
        )
        flow.fetch_token(code=code)
        credentials = flow.credentials

        store_user_credentials(session_id, credentials)

        # Get user email
        service = build("calendar", "v3", credentials=credentials)
        calendar = service.calendars().get(calendarId="primary").execute()
        email = calendar.get("summary", "Your Calendar")

        return f"""✅ **Successfully Connected!**

📧 Calendar: {email}
🔑 Session ID: `{session_id}`

You can now use:
• `list_calendar_events(session_id="{session_id}")`
• `add_calendar_event(session_id="{session_id}", ...)`

⚠️ Keep your session_id - you need it for all calendar operations!"""
    except Exception as e:
        return f"❌ Authorization failed: {e}\n\nTry google_auth_start again."


@mcp.tool()
def google_auth_status(session_id: str) -> str:
    """Check if your session is authenticated.

    Args:
        session_id: Your session ID
    """
    credentials = get_user_credentials(session_id)
    if not credentials:
        return f"❌ Session `{session_id[:8]}...` not found or expired.\n\nCall google_auth_start to reconnect."

    try:
        service = build("calendar", "v3", credentials=credentials)
        calendar = service.calendars().get(calendarId="primary").execute()
        return f"✅ Connected to: {calendar.get('summary', 'Your Calendar')}\nSession: `{session_id[:8]}...`"
    except Exception as e:
        return f"⚠️ Session exists but error: {e}"


@mcp.tool()
def google_auth_logout(session_id: str) -> str:
    """Logout and clear your session.

    Args:
        session_id: Your session ID
    """
    if session_id in USER_SESSIONS:
        del USER_SESSIONS[session_id]
        return f"✅ Logged out. Session `{session_id[:8]}...` cleared."
    return f"⚠️ Session not found: `{session_id[:8]}...`"


# ============================================================================
# WEATHER & TIME TOOLS
# ============================================================================


@mcp.tool()
async def get_weather(city: str, country: str | None = None) -> str:
    """Get current weather for a city.

    Args:
        city: City name (e.g., "Tokyo", "London")
        country: Optional country code (e.g., "JP", "UK")
    """
    location = await get_coordinates(city, country)
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
        timezone: Timezone (e.g., "UTC", "US/Eastern", "Asia/Tokyo")
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


# ============================================================================
# CALENDAR TOOLS (Multi-User)
# ============================================================================


@mcp.tool()
def add_calendar_event(
    session_id: str,
    title: str,
    start_time: str,
    end_time: str,
    description: str | None = None,
    location: str | None = None,
    timezone: str = "UTC",
    attendees: str | None = None,
) -> str:
    """Add event to YOUR Google Calendar.

    Args:
        session_id: Your session ID from google_auth_callback
        title: Event title
        start_time: Start (ISO format: "2025-01-15T10:00:00" or "2025-01-15" for all-day)
        end_time: End (ISO format)
        description: Optional description
        location: Optional location
        timezone: Timezone (default: UTC)
        attendees: Optional comma-separated emails
    """
    service = get_calendar_service(session_id)
    if not service:
        return f"❌ Not authenticated. Call google_auth_start first.\nSession: `{session_id[:8] if session_id else 'None'}...`"

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
            event["attendees"] = [ # type: ignore
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

        return f"""✅ Event Created!

📅 {title}
⏰ {start_time} - {end_time} ({timezone})
{f"📍 {location}" if location else ""}
{f"👥 {attendees}" if attendees else ""}

🔗 {created.get("htmlLink", "No link")}"""
    except HttpError as e:
        return f"❌ Calendar error: {e.reason}"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def list_calendar_events(
    session_id: str,
    max_results: int = 10,
    time_min: str | None = None,
    time_max: str | None = None,
) -> str:
    """List YOUR upcoming Google Calendar events.

    Args:
        session_id: Your session ID from google_auth_callback
        max_results: Max events (1-50, default 10)
        time_min: Optional start (ISO format)
        time_max: Optional end (ISO format)
    """
    service = get_calendar_service(session_id)
    if not service:
        return f"❌ Not authenticated. Call google_auth_start first.\nSession: `{session_id[:8] if session_id else 'None'}...`"

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
            return "📅 No upcoming events."

        result = f"📅 **Your Upcoming Events** ({len(events)}):\n" + "=" * 40 + "\n"
        for i, e in enumerate(events, 1):
            start = e["start"].get("dateTime", e["start"].get("date"))
            if "T" in start:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                start_str = dt.strftime("%b %d %I:%M %p")
            else:
                start_str = f"{start} (All day)"

            result += (
                f"\n{i}. **{e.get('summary', '(No title)')}**\n   📆 {start_str}\n"
            )
            if e.get("location"):
                result += f"   📍 {e['location']}\n"

        return result
    except HttpError as e:
        return f"❌ Calendar error: {e.reason}"
    except Exception as e:
        return f"❌ Error: {e}"


# ============================================================================
# RESOURCES
# ============================================================================


@mcp.resource("config://settings")
def get_config() -> str:
    return json.dumps(
        {
            "version": "2.0.0",
            "multi_user": True,
            "google_calendar": GOOGLE_CALENDAR_AVAILABLE,
            "active_sessions": len(USER_SESSIONS),
            "session_expiry_hours": SESSION_EXPIRY_HOURS,
        },
        indent=2,
    )


@mcp.resource("auth://status")
def get_auth_info() -> str:
    return json.dumps(
        {
            "google_calendar_available": GOOGLE_CALENDAR_AVAILABLE,
            "credentials_file_exists": CREDENTIALS_FILE.exists(),
            "active_sessions": len(USER_SESSIONS),
            "how_to_connect": "Call google_auth_start tool to connect your Google Calendar",
        },
        indent=2,
    )


@mcp.resource("calendar://help")
def get_calendar_help() -> str:
    return """# Multi-User Google Calendar

## How to Connect YOUR Calendar:

1. Call `google_auth_start` tool
2. Open the URL in your browser  
3. Login with YOUR Google account
4. Grant calendar permission
5. Copy the authorization code
6. Call `google_auth_callback(session_id="...", code="...")`
7. Save your session_id!

## Using Calendar Tools:

All calendar tools require YOUR session_id:

```
list_calendar_events(session_id="your-session-id")
add_calendar_event(session_id="your-session-id", title="Meeting", ...)
```

## Commands:
- google_auth_start - Get auth URL
- google_auth_callback - Complete auth with code  
- google_auth_status - Check if connected
- google_auth_logout - Disconnect
- add_calendar_event - Create event
- list_calendar_events - View events
"""


# ============================================================================
# PROMPTS
# ============================================================================


@mcp.prompt()
def connect_calendar() -> str:
    """Help connect Google Calendar."""
    return """Let's connect your Google Calendar!

I'll call google_auth_start to get you an authorization URL.
Then you'll:
1. Open the URL in your browser
2. Login with YOUR Google account
3. Copy the code
4. Give me the code to complete setup

Ready? I'll start the process now."""


@mcp.prompt()
def schedule_meeting(session_id: str, title: str, duration_minutes: int = 60) -> str:
    """Schedule a meeting."""
    return f"""Schedule meeting: "{title}" ({duration_minutes} min)

Using session: {session_id}

1. First, check current schedule with list_calendar_events
2. Get current time to find good slots
3. Suggest available times
4. Create event when confirmed

Let me check your calendar..."""


def main():
    logger.info(f"Starting Multi-User MCP Server on http://{HOST}:{PORT}/mcp")
    logger.info("Each user connects their OWN Google Calendar")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
