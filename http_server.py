"""
MCP Server with Multi-User OAuth - Automatic Callback

AUTOMATIC AUTHENTICATION:
- OAuth callback automatically completes authentication
- No manual session ID copy-paste required
- Works on both local and cloud environments

SUPPORTED ENVIRONMENTS:
1. Local Development (VSCode, Claude Code):
   - Callback: http://localhost:8085/callback
   - Browser opens automatically

2. Cloud Deployment (Render, Railway, Fly.io, etc):
   - Callback: https://your-domain.com/callback
   - Set CALLBACK_URL environment variable

SETUP:
1. Create OAuth 2.0 Client ID (Web Application) in Google Cloud Console
2. Add authorized redirect URIs:
   - http://localhost:8085/callback (for local)
   - https://your-cloud-domain.com/callback (for cloud)
3. Download credentials.json OR set GOOGLE_CREDENTIALS_JSON env var

Run: python http_server.py
"""

import json
import logging
import os
import secrets
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass, field
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
logger = logging.getLogger("mcp-server")

# -------------------
# CONFIGURATION
# -------------------

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8000))
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALLBACK_PORT = int(os.environ.get("CALLBACK_PORT", 8085))
DATA_STORAGE_PATH = Path(os.environ.get("DATA_STORAGE_PATH", "./data"))

# Environment detection
IS_CLOUD = bool(
    os.environ.get("RENDER")
    or os.environ.get("RAILWAY_STATIC_URL")
    or os.environ.get("FLY_APP_NAME")
    or os.environ.get("CLOUD_RUN_SERVICE")
    or os.environ.get("CALLBACK_URL")
    or os.environ.get("IS_CLOUD")
)


def get_callback_url() -> str:
    """Get the callback URL based on environment."""
    if os.environ.get("CALLBACK_URL"):
        return os.environ["CALLBACK_URL"]
    if os.environ.get("RENDER_EXTERNAL_URL"):
        return f"{os.environ['RENDER_EXTERNAL_URL']}/callback"
    if os.environ.get("RAILWAY_STATIC_URL"):
        return f"https://{os.environ['RAILWAY_STATIC_URL']}/callback"
    if os.environ.get("FLY_APP_NAME"):
        return f"https://{os.environ['FLY_APP_NAME']}.fly.dev/callback"
    return f"http://localhost:{CALLBACK_PORT}/callback"


CALLBACK_URL = get_callback_url()
logger.info(f"Callback URL: {CALLBACK_URL}")

mcp = FastMCP(name="mcp-calendar-server", host=HOST, port=PORT)


def get_credentials_file() -> Path:
    """Get credentials file path."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        temp_path = Path("/tmp/credentials.json")
        temp_path.write_text(creds_json)
        return temp_path

    locations = [
        Path(__file__).parent / "credentials.json",
        Path(__file__).parent.parent / "credentials.json",
        Path.cwd() / "credentials.json",
    ]
    for loc in locations:
        if loc.exists():
            return loc
    return Path(__file__).parent / "credentials.json"


CREDENTIALS_FILE = get_credentials_file()


# -------------------
# USER SESSION WITH AUTO-CALLBACK
# -------------------


@dataclass
class UserSession:
    """User authentication session with callback support."""

    session_id: str
    user_id: str  # Identifier from client (e.g., conversation ID, user ID)
    created_at: datetime = field(default_factory=datetime.now)
    credentials: Optional[Credentials] = None
    email: Optional[str] = None
    pending_auth: Optional[dict] = None
    auth_completed: bool = False
    auth_error: Optional[str] = None

    def is_authenticated(self) -> bool:
        if not self.credentials:
            return False
        if self.credentials.expired and self.credentials.refresh_token:
            try:
                self.credentials.refresh(Request())
                self.save_token()
            except Exception:
                return False
        return True

    def save_token(self):
        if not self.credentials:
            return

        DATA_STORAGE_PATH.mkdir(parents=True, exist_ok=True)
        # Save by user_id for persistence across sessions
        token_file = DATA_STORAGE_PATH / f"{self.user_id}.json"

        try:
            token_file.write_text(
                json.dumps(
                    {
                        "token": self.credentials.token,
                        "refresh_token": self.credentials.refresh_token,
                        "token_uri": self.credentials.token_uri,
                        "client_id": self.credentials.client_id,
                        "client_secret": self.credentials.client_secret,
                        "scopes": list(self.credentials.scopes or SCOPES),
                        "email": self.email,
                        "user_id": self.user_id,
                    },
                    indent=2,
                )
            )
            logger.info(f"Token saved for user {self.user_id[:8]}...")
        except Exception as e:
            logger.error(f"Failed to save token: {e}")

    def load_token(self) -> bool:
        token_file = DATA_STORAGE_PATH / f"{self.user_id}.json"
        if not token_file.exists():
            return False

        try:
            data = json.loads(token_file.read_text())
            self.credentials = Credentials(
                token=data.get("token"),
                refresh_token=data.get("refresh_token"),
                token_uri=data.get("token_uri"),
                client_id=data.get("client_id"),
                client_secret=data.get("client_secret"),
                scopes=data.get("scopes", SCOPES),
            )
            self.email = data.get("email")

            if self.credentials.expired and self.credentials.refresh_token:
                self.credentials.refresh(Request())
                self.save_token()

            logger.info(f"Loaded token for {self.email}")
            return True
        except Exception as e:
            logger.error(f"Failed to load token: {e}")
            return False

    def wait_for_auth(self, timeout: int = 120) -> bool:
        """Wait for OAuth callback to complete."""
        start = time.time()
        while time.time() - start < timeout:
            if self.auth_completed:
                return self.is_authenticated()
            if self.auth_error:
                return False
            time.sleep(0.5)
        return False


class SessionManager:
    """Manages user sessions with automatic callback handling."""

    def __init__(self):
        self.sessions: dict[str, UserSession] = {}
        self.user_sessions: dict[str, str] = {}  # user_id -> session_id
        self.auth_states: dict[str, str] = {}  # OAuth state -> session_id
        self._lock = threading.Lock()

    def get_or_create_session(self, user_id: str) -> UserSession:
        """Get existing session for user or create new one."""
        with self._lock:
            # Check if user already has a session
            if user_id in self.user_sessions:
                session_id = self.user_sessions[user_id]
                if session_id in self.sessions:
                    return self.sessions[session_id]

            # Create new session
            session_id = secrets.token_urlsafe(16)
            session = UserSession(session_id=session_id, user_id=user_id)

            # Try to load existing token for this user
            session.load_token()

            self.sessions[session_id] = session
            self.user_sessions[user_id] = session_id

            logger.info(f"Created session for user {user_id[:8]}...")
            return session

    def get_session(self, session_id: str) -> Optional[UserSession]:
        return self.sessions.get(session_id)

    def get_session_by_user(self, user_id: str) -> Optional[UserSession]:
        session_id = self.user_sessions.get(user_id)
        if session_id:
            return self.sessions.get(session_id)
        return None

    def register_auth_state(self, state: str, session_id: str):
        with self._lock:
            self.auth_states[state] = session_id

    def get_session_by_state(self, state: str) -> Optional[UserSession]:
        session_id = self.auth_states.get(state)
        if session_id:
            return self.sessions.get(session_id)
        return None

    def cleanup_auth_state(self, state: str):
        with self._lock:
            self.auth_states.pop(state, None)


session_manager = SessionManager()


# -------------------
# OAUTH CALLBACK HANDLER
# -------------------


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for OAuth callbacks - auto-completes authentication."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/callback":
            self._handle_oauth_callback(parsed)
        elif parsed.path == "/health":
            self._send_text(200, "OK")
        elif parsed.path == "/auth/check":
            self._handle_auth_check(parsed)
        else:
            self._send_text(404, "Not Found")

    def _handle_oauth_callback(self, parsed):
        """Handle OAuth callback - automatically complete auth."""
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]

        if error:
            session = session_manager.get_session_by_state(state) if state else None
            if session:
                session.auth_error = error
                session.auth_completed = True

            self._send_html(
                400,
                f"""
                <h1>Authorization Failed</h1>
                <p>Error: {error}</p>
                <p>{params.get("error_description", [""])[0]}</p>
                <p>You can close this window.</p>
            """,
            )
            return

        if not code or not state:
            self._send_html(400, "<h1>Invalid callback</h1>")
            return

        session = session_manager.get_session_by_state(state)
        if not session or not session.pending_auth:
            self._send_html(400, "<h1>Session expired</h1><p>Please try again.</p>")
            return

        try:
            flow = session.pending_auth.get("flow")
            if not flow:
                raise ValueError("No OAuth flow")

            # Exchange code for tokens
            flow.fetch_token(code=code)
            session.credentials = flow.credentials

            # Get user email
            service = build("calendar", "v3", credentials=session.credentials)
            calendar = service.calendars().get(calendarId="primary").execute()
            session.email = calendar.get("summary", "Unknown")

            # Save and mark complete
            session.save_token()
            session.pending_auth = None
            session.auth_completed = True
            session.auth_error = None

            session_manager.cleanup_auth_state(state)

            logger.info(f"✅ OAuth complete for {session.email}")

            self._send_html(
                200,
                f"""
                <h1>DayaTech MCP Authorization Successful!</h1>
                <p>Welcome, <strong>{session.email}</strong>!</p>
                <p>You can close this window and return to your application.</p>
                <p>Your calendar is now connected.</p>
                <script>
                    setTimeout(() => {{
                        window.close();
                    }}, 2000);
                </script>
            """,
            )

        except Exception as e:
            logger.error(f"Callback error: {e}")
            session.auth_error = str(e)
            session.auth_completed = True
            self._send_html(500, f"<h1>Error</h1><p>{e}</p>")

    def _handle_auth_check(self, parsed):
        """Check auth status for polling."""
        params = parse_qs(parsed.query)
        user_id = params.get("user_id", [None])[0]

        if not user_id:
            self._send_json(400, {"error": "Missing user_id"})
            return

        session = session_manager.get_session_by_user(user_id)
        if not session:
            self._send_json(404, {"authenticated": False, "pending": False})
            return

        self._send_json(
            200,
            {
                "authenticated": session.is_authenticated(),
                "pending": session.pending_auth is not None,
                "completed": session.auth_completed,
                "email": session.email,
                "error": session.auth_error,
            },
        )

    def _send_text(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def _send_html(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        html = f"""<!DOCTYPE html>
<html><head><title>MCP OAuth</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; text-align: center; padding: 50px; background: #f5f5f5; }}
h1 {{ color: #333; }}
</style></head>
<body>{body}</body></html>"""
        self.wfile.write(html.encode())

    def _send_json(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


def find_free_port(start: int = 8085) -> int:
    for port in range(start, start + 10):
        try:
            with socket.socket() as s:
                s.bind(("localhost", port))
                return port
        except OSError:
            continue
    return start


# Global callback server
_callback_server: Optional[HTTPServer] = None
_callback_thread: Optional[threading.Thread] = None


def start_callback_server():
    """Start callback server for OAuth."""
    global _callback_server, _callback_thread, CALLBACK_PORT

    if _callback_server:
        return

    if IS_CLOUD:
        logger.info("Cloud mode - callback handled by main server or external URL")
        return

    port = find_free_port(CALLBACK_PORT)
    CALLBACK_PORT = port

    _callback_server = HTTPServer(("0.0.0.0", port), OAuthCallbackHandler)

    def serve():
        logger.info(f"Callback server on port {port}")
        _callback_server.serve_forever()  # type: ignore

    _callback_thread = threading.Thread(target=serve, daemon=True)
    _callback_thread.start()


# -------------------
# AUTHENTICATION FLOW
# -------------------


def start_oauth_flow(user_id: str, open_browser: bool = True) -> dict:
    """Start OAuth flow for a user - returns immediately, callback handles completion."""
    if not GOOGLE_CALENDAR_AVAILABLE:
        return {"success": False, "error": "Google Calendar not installed"}

    if not CREDENTIALS_FILE.exists():
        return {"success": False, "error": "credentials.json not found"}

    session = session_manager.get_or_create_session(user_id)

    # Already authenticated?
    if session.is_authenticated():
        return {
            "success": True,
            "authenticated": True,
            "email": session.email,
            "message": f"Already connected as {session.email}",
        }

    try:
        callback_url = CALLBACK_URL
        if not IS_CLOUD:
            callback_url = f"http://localhost:{CALLBACK_PORT}/callback"

        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_FILE), scopes=SCOPES, redirect_uri=callback_url
        )

        state = secrets.token_urlsafe(32)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            state=state,
            include_granted_scopes="true",
        )

        session.pending_auth = {
            "flow": flow,
            "state": state,
            "started_at": datetime.now().isoformat(),
        }
        session.auth_completed = False
        session.auth_error = None

        session_manager.register_auth_state(state, session.session_id)

        # Open browser for local development
        if open_browser and not IS_CLOUD:
            try:
                webbrowser.open(auth_url)
                logger.info("Browser opened for authentication")
            except Exception as e:
                logger.warning(f"Could not open browser: {e}")

        return {
            "success": True,
            "authenticated": False,
            "auth_url": auth_url,
            "user_id": user_id,
            "message": "Please complete authentication in browser"
            if open_browser
            else "Open the auth_url to authenticate",
        }

    except Exception as e:
        logger.error(f"OAuth flow error: {e}")
        return {"success": False, "error": str(e)}


def wait_for_oauth(user_id: str, timeout: int = 120) -> dict:
    """Wait for OAuth callback to complete."""
    session = session_manager.get_session_by_user(user_id)
    if not session:
        return {"success": False, "error": "No session found"}

    if session.is_authenticated():
        return {
            "success": True,
            "authenticated": True,
            "email": session.email,
        }

    # Wait for callback
    if session.wait_for_auth(timeout):
        return {
            "success": True,
            "authenticated": True,
            "email": session.email,
            "message": f"Connected as {session.email}",
        }

    if session.auth_error:
        return {"success": False, "error": session.auth_error}

    return {"success": False, "error": "Authentication timed out"}


# -------------------
# WEATHER HELPERS
# -------------------


async def get_coordinates(city: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en"},
                timeout=10.0,
            )
            data = resp.json()
            if data.get("results"):
                r = data["results"][0]
                return {
                    "lat": r["latitude"],
                    "lon": r["longitude"],
                    "name": r["name"],
                    "country": r.get("country", ""),
                }
        except Exception:
            pass
    return None


async def fetch_weather(lat: float, lon: float) -> dict | None:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                },
                timeout=10.0,
            )
            return resp.json()
        except Exception:
            pass
    return None


# -------------------
# MCP TOOLS
# -------------------


@mcp.tool()
async def get_weather(city: str, country: str | None = None) -> str:
    """Get current weather for a city.

    Args:
        city: City name (e.g., "Tokyo", "London", "Jakarta")
        country: Optional country code (e.g., "JP", "UK", "ID")
    """
    loc = await get_coordinates(city)
    if not loc:
        return f"❌ Could not find: {city}"

    weather = await fetch_weather(loc["lat"], loc["lon"])
    if not weather:
        return "❌ Could not fetch weather"

    c = weather.get("current", {})
    codes = {
        0: "Clear",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Foggy",
        61: "Rain",
        71: "Snow",
        95: "Thunderstorm",
    }

    return f"""🌤️ Weather for {loc["name"]}, {loc["country"]}:
• Temperature: {c.get("temperature_2m", "N/A")}°C
• Humidity: {c.get("relative_humidity_2m", "N/A")}%
• Conditions: {codes.get(c.get("weather_code", -1), "Unknown")}
• Wind: {c.get("wind_speed_10m", "N/A")} km/h"""


@mcp.tool()
def get_current_time(timezone: str = "UTC") -> str:
    """Get current date and time.

    Args:
        timezone: Timezone (e.g., "UTC", "Asia/Jakarta", "US/Eastern")
    """
    try:
        now = datetime.now(ZoneInfo(timezone))
        return f"""🕐 Current Time ({timezone}):
• Date: {now.strftime("%A, %B %d, %Y")}
• Time: {now.strftime("%I:%M:%S %p")}
• ISO: {now.isoformat()}"""
    except Exception:
        now = datetime.now(ZoneInfo("UTC"))
        return f"⚠️ Invalid timezone. UTC: {now.isoformat()}"


@mcp.tool()
def connect_google_calendar(user_id: str, wait: bool = True, timeout: int = 120) -> str:
    """Connect to Google Calendar with automatic OAuth.

    Opens browser for authentication and waits for completion.
    The callback automatically handles the OAuth response.

    Args:
        user_id: Unique identifier for this user/conversation
        wait: Wait for authentication to complete (default: True)
        timeout: Seconds to wait for auth (default: 120)
    """
    # Check if already connected
    session = session_manager.get_session_by_user(user_id)
    if session and session.is_authenticated():
        return f"""✅ Already connected to Google Calendar!

📧 Account: {session.email}
🔑 User ID: {user_id}

You can now use calendar tools."""

    # Start OAuth flow
    result = start_oauth_flow(user_id, open_browser=True)

    if not result.get("success"):
        return f"❌ Error: {result.get('error')}"

    if result.get("authenticated"):
        return f"""✅ Connected to Google Calendar!

📧 Account: {result.get("email")}

You can now use calendar tools."""

    if not wait:
        return f"""🔐 Authentication started!

Please complete sign-in in your browser.
Auth URL: {result.get("auth_url")}

After completing authentication, call check_calendar_connection to verify."""

    # Wait for callback
    print("⏳ Waiting for authentication...")
    auth_result = wait_for_oauth(user_id, timeout)

    if auth_result.get("success") and auth_result.get("authenticated"):
        return f"""✅ Successfully connected to Google Calendar!

📧 Account: {auth_result.get("email")}
🔑 User ID: {user_id}

You can now use calendar tools like:
• list_calendar_events
• add_calendar_event
• delete_calendar_event"""

    error = auth_result.get("error", "Authentication failed or timed out")
    return f"""❌ Authentication failed: {error}

Please try again with connect_google_calendar."""


@mcp.tool()
def check_calendar_connection(user_id: str) -> str:
    """Check if Google Calendar is connected for a user.

    Args:
        user_id: The user ID used in connect_google_calendar
    """
    session = session_manager.get_session_by_user(user_id)

    if not session:
        return f"""❌ No connection found for user: {user_id}

Use connect_google_calendar to connect."""

    if session.is_authenticated():
        return f"""✅ Connected to Google Calendar

📧 Account: {session.email}
🔑 User ID: {user_id}"""

    if session.pending_auth:
        return f"""⏳ Authentication in progress...

Please complete sign-in in your browser.
Started: {session.pending_auth.get("started_at", "Unknown")}"""

    if session.auth_error:
        return f"""❌ Authentication failed: {session.auth_error}

Use connect_google_calendar to try again."""

    return """❌ Not connected

Use connect_google_calendar to connect."""


@mcp.tool()
def disconnect_google_calendar(user_id: str) -> str:
    """Disconnect Google Calendar for a user.

    Args:
        user_id: The user ID to disconnect
    """
    session = session_manager.get_session_by_user(user_id)

    if not session:
        return f"No connection found for user: {user_id}"

    # Clear credentials
    session.credentials = None
    session.email = None
    session.auth_completed = False

    # Remove token file
    token_file = DATA_STORAGE_PATH / f"{user_id}.json"
    if token_file.exists():
        token_file.unlink()

    return f"✅ Disconnected Google Calendar for user {user_id}"


@mcp.tool()
def list_calendar_events(
    user_id: str,
    max_results: int = 10,
    time_min: str | None = None,
    time_max: str | None = None,
) -> str:
    """List upcoming Google Calendar events.

    Args:
        user_id: User ID from connect_google_calendar
        max_results: Max events to return (1-50, default 10)
        time_min: Optional start date/time filter (ISO format)
        time_max: Optional end date/time filter (ISO format)
    """
    session = session_manager.get_session_by_user(user_id)

    if not session or not session.is_authenticated():
        return f"""❌ Not connected to Google Calendar.

Use connect_google_calendar(user_id="{user_id}") first."""

    try:
        service = build("calendar", "v3", credentials=session.credentials)

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
            params["timeMax"] = time_max if time_max.endswith("Z") else time_max + "Z"

        events = service.events().list(**params).execute().get("items", [])

        if not events:
            return f"📅 No upcoming events for {session.email}"

        result = f"📅 **{session.email}** - Events ({len(events)}):\n{'=' * 40}\n"

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
            result += f"   🆔 `{e.get('id')}`\n"

        return result

    except HttpError as e:
        return f"❌ Error: {e.reason}"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def add_calendar_event(
    user_id: str,
    title: str,
    start_time: str,
    end_time: str,
    description: str | None = None,
    location: str | None = None,
    timezone: str = "Asia/Jakarta",
    attendees: str | None = None,
) -> str:
    """Add a new event to Google Calendar.

    Args:
        user_id: User ID from connect_google_calendar
        title: Event title
        start_time: Start time - ISO format "2025-01-15T14:00:00" or date "2025-01-15"
        end_time: End time - ISO format "2025-01-15T15:00:00" or date "2025-01-16"
        description: Optional description
        location: Optional location
        timezone: Timezone (default: Asia/Jakarta)
        attendees: Optional comma-separated email addresses
    """
    session = session_manager.get_session_by_user(user_id)

    if not session or not session.is_authenticated():
        return f"""❌ Not connected to Google Calendar.

Use connect_google_calendar(user_id="{user_id}") first."""

    try:
        service = build("calendar", "v3", credentials=session.credentials)

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
            event["attendees"] = [{"email": e.strip()} for e in attendees.split(",")]  # type: ignore

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

📅 **{title}**
⏰ {start_time} → {end_time}
🌍 {timezone}
{f"📍 {location}" if location else ""}
👤 {session.email}

🔗 {created.get("htmlLink", "")}
🆔 `{created.get("id")}`"""

    except HttpError as e:
        return f"❌ Error: {e.reason}"
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
def delete_calendar_event(user_id: str, event_id: str) -> str:
    """Delete an event from Google Calendar.

    Args:
        user_id: User ID from connect_google_calendar
        event_id: The event ID to delete (from list_calendar_events)
    """
    session = session_manager.get_session_by_user(user_id)

    if not session or not session.is_authenticated():
        return f"""❌ Not connected to Google Calendar.

Use connect_google_calendar(user_id="{user_id}") first."""

    try:
        service = build("calendar", "v3", credentials=session.credentials)
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return "✅ Event deleted!"

    except HttpError as e:
        return f"❌ Error: {e.reason}"
    except Exception as e:
        return f"❌ Error: {e}"


# -------------------
# MCP RESOURCES
# -------------------


@mcp.resource("config://settings")
def get_config() -> str:
    return json.dumps(
        {
            "version": "7.0.0",
            "multi_user": True,
            "auto_callback": True,
            "callback_url": CALLBACK_URL,
            "is_cloud": IS_CLOUD,
            "active_users": len(session_manager.user_sessions),
            "google_available": GOOGLE_CALENDAR_AVAILABLE,
        },
        indent=2,
    )


@mcp.resource("users://connected")
def get_connected_users() -> str:
    users = []
    for user_id, session_id in session_manager.user_sessions.items():
        session = session_manager.sessions.get(session_id)
        if session:
            users.append(
                {
                    "user_id": user_id,
                    "authenticated": session.is_authenticated(),
                    "email": session.email,
                }
            )
    return json.dumps({"users": users}, indent=2)


# -------------------
# MAIN
# -------------------


def main():
    logger.info("=" * 60)
    logger.info("MCP Multi-User Calendar Server (Auto-Callback)")
    logger.info("=" * 60)
    logger.info(f"Host: {HOST}:{PORT}")
    logger.info(f"Callback: {CALLBACK_URL}")
    logger.info(f"Mode: {'Cloud' if IS_CLOUD else 'Local'}")
    logger.info(f"Google: {'✓' if GOOGLE_CALENDAR_AVAILABLE else '✗'}")
    logger.info("=" * 60)

    # Start callback server
    start_callback_server()

    logger.info(f"MCP endpoint: http://{HOST}:{PORT}/mcp")
    logger.info("Ready!")

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
