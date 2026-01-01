"""
MCP Server with AUTO-AUTH Google Calendar (Browser Auto-Open)

AUTOMATIC AUTHENTICATION:
When user asks about calendar, the server:
1. Opens browser automatically for Google login
2. Captures auth code via local callback server
3. Returns to agent automatically - NO manual copy/paste!

TOOLS:
1. get_weather - Get weather for a city
2. get_current_time - Get current time
3. list_calendar_events - List events (auto-auth)
4. add_calendar_event - Add event (auto-auth)
5. delete_calendar_event - Delete event (auto-auth)
6. google_auth_submit - Manual auth code submission (fallback)

Run: python servers/http_server.py
"""

import json
import logging
import os
import secrets
import socket
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse
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

# Callback server port for OAuth (local only)
OAUTH_CALLBACK_PORT = 8085
OAUTH_CALLBACK_HOST = "localhost"


# Check if running locally (can open browser) or in cloud
def is_local_environment() -> bool:
    """Check if running locally (can open browser) or in cloud."""
    cloud_indicators = [
        "RENDER",
        "RAILWAY",
        "HEROKU",
        "AWS_LAMBDA_FUNCTION_NAME",
        "GOOGLE_CLOUD_PROJECT",
    ]
    return not any(os.environ.get(var) for var in cloud_indicators)


IS_LOCAL = is_local_environment()

# Set redirect URI based on environment
if IS_LOCAL:
    REDIRECT_URI = f"http://{OAUTH_CALLBACK_HOST}:{OAUTH_CALLBACK_PORT}/callback"
else:
    REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"  # Manual copy-paste for cloud

# =============================================================================
# OAUTH CALLBACK SERVER (Local Auto-Auth)
# =============================================================================

# Store for pending OAuth flows
PENDING_AUTH: dict[str, dict] = {}


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler to capture OAuth callback."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_GET(self):
        """Handle OAuth callback GET request."""
        parsed = urlparse(self.path)

        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]
            error = params.get("error", [None])[0]

            if error:
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    f"""
                <html><body style="font-family: Arial; text-align: center; padding: 50px;">
                <h1>❌ Authorization Failed</h1>
                <p>Error: {error}</p>
                <p>You can close this window.</p>
                </body></html>
                """.encode()
                )
                return

            if code and state and state in PENDING_AUTH:
                # Store the code for the session
                PENDING_AUTH[state]["code"] = code
                PENDING_AUTH[state]["completed"] = True

                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    """
                <html><body style="font-family: Arial; text-align: center; padding: 50px;">
                <h1>DayaTech MCP Authorization Successful!</h1>
                <p>You can close this window and return to the app.</p>
                <script>setTimeout(() => window.close(), 2000);</script>
                </body></html>
                """.encode()
                )
                logger.info(f"OAuth callback received for state: {state[:8]}...")
            else:
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    """
                <html><body style="font-family: Arial; text-align: center; padding: 50px;">
                <h1>❌ Invalid Request</h1>
                <p>Missing or invalid authorization state.</p>
                </body></html>
                """.encode()
                )
        else:
            self.send_response(404)
            self.end_headers()


def find_free_port(start_port: int = 8085, max_attempts: int = 10) -> int:
    """Find a free port starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((OAUTH_CALLBACK_HOST, port))
                return port
        except OSError:
            continue
    return start_port  # Fallback


def start_oauth_callback_server(port: int, timeout: int = 120) -> Optional[HTTPServer]:
    """Start a temporary HTTP server to capture OAuth callback."""
    try:
        server = HTTPServer((OAUTH_CALLBACK_HOST, port), OAuthCallbackHandler)
        server.timeout = timeout
        return server
    except Exception as e:
        logger.error(f"Failed to start OAuth callback server: {e}")
        return None


def auto_authenticate_local(session_id: str) -> str:
    """Perform automatic OAuth flow with browser open (LOCAL ONLY).

    1. Start local callback server
    2. Open browser with auth URL
    3. Wait for callback with code
    4. Exchange code for tokens
    5. Return success message
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "❌ Google Calendar not available."

    creds_file = (
        CREDENTIALS_FILE if CREDENTIALS_FILE.exists() else Path("/tmp/credentials.json")
    )
    if not creds_file.exists():
        return "❌ Google credentials not configured."

    # Find free port and set redirect URI
    port = find_free_port(OAUTH_CALLBACK_PORT)
    redirect_uri = f"http://{OAUTH_CALLBACK_HOST}:{port}/callback"

    try:
        # Create OAuth flow with local redirect
        flow = Flow.from_client_secrets_file(
            str(creds_file), scopes=SCOPES, redirect_uri=redirect_uri
        )

        # Generate state for security
        state = secrets.token_urlsafe(16)
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent", state=state
        )

        # Store pending auth
        PENDING_AUTH[state] = {
            "flow": flow,
            "session_id": session_id,
            "code": None,
            "completed": False,
        }

        # Start callback server in background
        server = start_oauth_callback_server(port, timeout=120)
        if not server:
            return get_manual_auth_prompt(session_id)  # Fallback to manual

        logger.info(f"Opening browser for OAuth (port {port})...")

        # Open browser
        webbrowser.open(auth_url)

        # Wait for callback (with timeout)
        import time

        start_time = time.time()
        timeout_seconds = 120

        while time.time() - start_time < timeout_seconds:
            server.handle_request()  # Handle one request

            if PENDING_AUTH.get(state, {}).get("completed"):
                break

            time.sleep(0.1)

        server.server_close()

        # Check if we got the code
        auth_data = PENDING_AUTH.get(state, {})
        if not auth_data.get("code"):
            del PENDING_AUTH[state]
            return "❌ Authorization timed out. Please try again."

        code = auth_data["code"]

        # Exchange code for credentials
        try:
            flow.fetch_token(code=code)
            credentials = flow.credentials

            # Store in session
            session = get_session(session_id)
            session["credentials"] = credentials
            session["pending_auth"] = None

            # Get user email
            service = build("calendar", "v3", credentials=credentials)
            calendar = service.calendars().get(calendarId="primary").execute()
            email = calendar.get("summary", "Your Calendar")
            session["email"] = email

            # Cleanup
            del PENDING_AUTH[state]

            logger.info(f"Auto-auth successful: {email}")

            return f"""✅ **Successfully Connected!**

📧 Calendar: {email}
🔑 Session ID: `{session_id}`

Your Google Calendar is now connected. What would you like to do?
• List your upcoming events
• Add a new event
• Check today's schedule"""

        except Exception as e:
            logger.error(f"Token exchange failed: {e}")
            del PENDING_AUTH[state]
            return f"❌ Authorization failed: {e}"

    except Exception as e:
        logger.error(f"Auto-auth failed: {e}")
        return get_manual_auth_prompt(session_id)  # Fallback to manual


def get_manual_auth_prompt(session_id: str) -> str:
    """Generate manual auth prompt (for cloud or fallback)."""
    creds_file = (
        CREDENTIALS_FILE if CREDENTIALS_FILE.exists() else Path("/tmp/credentials.json")
    )

    try:
        flow = Flow.from_client_secrets_file(
            str(creds_file), scopes=SCOPES, redirect_uri="urn:ietf:wg:oauth:2.0:oob"
        )
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

        session = get_session(session_id)
        session["pending_auth"] = flow

        return f"""🔐 **Google Calendar Authorization Required**

**Step 1:** Open this URL in your browser:
{auth_url}

**Step 2:** Log into your Google account and grant permission

**Step 3:** Copy the authorization code shown

**Step 4:** Submit the code:
```
google_auth_submit(session_id="{session_id}", code="YOUR_CODE_HERE")
```

📌 **Your Session ID:** `{session_id}`"""

    except Exception as e:
        return f"❌ Failed to create auth flow: {e}"


def get_auth_prompt(session_id: Optional[str] = None) -> str:
    """Get auth prompt - auto-opens browser locally, manual prompt for cloud."""
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "❌ Google Calendar not available. Install required packages."

    # Ensure credentials file exists
    if not CREDENTIALS_FILE.exists():
        env_var = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if env_var:
            try:
                Path("/tmp/credentials.json").write_text(env_var)
            except:  # noqa: E722
                pass

        if not CREDENTIALS_FILE.exists() and not Path("/tmp/credentials.json").exists():
            return "❌ Google credentials not configured on server."

    # Generate session ID if not provided
    if not session_id:
        session_id = secrets.token_urlsafe(16)

    # Local: Auto-open browser and capture code
    if IS_LOCAL:
        return auto_authenticate_local(session_id)

    # Cloud: Return manual prompt
    return get_manual_auth_prompt(session_id)


# =============================================================================
# MULTI-USER SESSION STORAGE
# =============================================================================

# Store credentials per user session in memory
USER_SESSIONS: dict[str, dict] = {}
SESSION_TIMEOUT_HOURS = 24


def get_session(session_id: Optional[str] = None) -> dict:
    """Get or create a user session."""
    if session_id is None:
        session_id = "default"

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


def get_calendar_service(session_id: Optional[str] = None):
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


def is_authenticated(session_id: Optional[str] = None) -> bool:
    """Check if user is authenticated."""
    session = get_session(session_id)
    return session.get("credentials") is not None


# def get_auth_prompt(session_id: str = None) -> str:
#     """Generate auth URL and return prompt for user."""
#     if not GOOGLE_CALENDAR_AVAILABLE:
#         return "❌ Google Calendar not available. Install required packages."

#     if not CREDENTIALS_FILE.exists():
#         env_var = os.environ.get('GOOGLE_CREDENTIALS_JSON')
#         if env_var:
#             try:
#                 Path('/tmp/credentials.json').write_text(env_var)
#             except:
#                 pass

#         if not CREDENTIALS_FILE.exists() and not Path('/tmp/credentials.json').exists():
#             return "❌ Google credentials not configured on server."

#     creds_file = CREDENTIALS_FILE if CREDENTIALS_FILE.exists() else Path('/tmp/credentials.json')

#     try:
#         flow = Flow.from_client_secrets_file(str(creds_file), scopes=SCOPES, redirect_uri=REDIRECT_URI)
#         auth_url, _ = flow.authorization_url(access_type='offline', prompt='consent')

#         # Generate unique session ID for this auth flow
#         new_session_id = secrets.token_urlsafe(16)
#         session = get_session(new_session_id)
#         session['pending_auth'] = flow

#         return f"""🔐 **Google Calendar Authorization Required**

# To access your calendar, please:

# **Step 1:** Open this URL in your browser:
# {auth_url}

# **Step 2:** Log into your Google account and grant permission

# **Step 3:** Copy the authorization code shown

# **Step 4:** Submit the code with your session ID:
# ```
# google_auth_submit(session_id="{new_session_id}", code="YOUR_CODE_HERE")
# ```

# 📌 **Your Session ID:** `{new_session_id}`
# (Save this - you'll need it to complete authentication!)"""

#     except Exception as e:
#         logger.error(f"Failed to create auth flow: {e}")
#         return f"❌ Failed to start authentication: {e}"


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
