"""
MCP Server with Multi-User OAuth - Automatic Callback (Cloud-Ready)

AUTOMATIC AUTHENTICATION:
- OAuth callback automatically completes authentication
- No manual session ID copy-paste required
- Works on both local and cloud environments

STORAGE MODES:
- In-memory storage (no disk required!)

SUPPORTED ENVIRONMENTS:
1. Local Development (VSCode, Claude Code):
   - Callback: http://localhost:8000/callback
   - Browser opens automatically
   - In-memory token storage (works without disk!)

2. Cloud Deployment (Render Free Tier):
   - Callback: https://your-domain.com/callback (same server via custom_route)
   - In-memory token storage (works without disk!)
   - Tokens persist during server uptime
   - Set CALLBACK_URL environment variable

SETUP:
1. Create OAuth 2.0 Client ID (Web Application) in Google Cloud Console
2. Add authorized redirect URIs:
   - http://localhost:8085/callback (for local)
   - https://your-cloud-domain.com/callback (for cloud)
3. Set GOOGLE_CREDENTIALS_JSON env var (required for cloud)

Run:
    python http_server.py
"""

import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from geopy.geocoders import Nominatim
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from timezonefinder import TimezoneFinder

# Google Calendar imports
try:
    from google.auth.transport.requests import Request as GoogleRequest
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

# ---------
# CONFIGURATION
# ---------

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8000))
VERSION = os.environ.get("VERSION", "0.0.1")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Environment detection
IS_CLOUD = bool(
    os.environ.get("RENDER")
    or os.environ.get("RAILWAY_STATIC_URL")
    or os.environ.get("FLY_APP_NAME")
    or os.environ.get("CLOUD_RUN_SERVICE")
    or os.environ.get("CALLBACK_URL")
    or os.environ.get("IS_CLOUD")
)

# Storage mode: memory
STORAGE_MODE = "memory"

logger.info(f"Storage mode: {STORAGE_MODE}")


def get_callback_url() -> str:
    """Get the callback URL based on environment."""
    if os.environ.get("CALLBACK_URL"):
        return os.environ["CALLBACK_URL"]
    return f"http://localhost:{PORT}/callback"


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


# ---------
# TOKEN STORAGE (Memory)
# ---------


class TokenStorage:
    """Abstract token storage interface."""

    def save(self, user_id: str, data: dict) -> bool:
        raise NotImplementedError

    def load(self, user_id: str) -> Optional[dict]:
        raise NotImplementedError

    def delete(self, user_id: str) -> bool:
        raise NotImplementedError

    def list_users(self) -> list[str]:
        raise NotImplementedError


class MemoryTokenStorage(TokenStorage):
    """In-memory token storage for cloud environments without disk."""

    def __init__(self):
        self._tokens: dict[str, dict] = {}
        self._lock = threading.Lock()
        logger.info(
            "Using in-memory token storage (tokens persist during server uptime)"
        )

    def save(self, user_id: str, data: dict) -> bool:
        with self._lock:
            self._tokens[user_id] = data
            logger.info(f"Token saved in memory for user {user_id[:8]}...")
            return True

    def load(self, user_id: str) -> Optional[dict]:
        with self._lock:
            return self._tokens.get(user_id)

    def delete(self, user_id: str) -> bool:
        with self._lock:
            if user_id in self._tokens:
                del self._tokens[user_id]
                return True
            return False

    def list_users(self) -> list[str]:
        with self._lock:
            return list(self._tokens.keys())


# Initialize storage based on mode
def create_token_storage() -> TokenStorage:
    return MemoryTokenStorage()


token_storage = create_token_storage()


# ---------
# USER SESSION WITH AUTO-CALLBACK
# ---------


@dataclass
class UserSession:
    """User authentication session with callback support."""

    session_id: str
    user_id: str
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
                self.credentials.refresh(GoogleRequest())
                self.save_token()
            except Exception:
                return False
        return True

    def save_token(self):
        if not self.credentials:
            return

        data = {
            "token": self.credentials.token,
            "refresh_token": self.credentials.refresh_token,
            "token_uri": self.credentials.token_uri,
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
            "scopes": list(self.credentials.scopes or SCOPES),
            "email": self.email,
            "user_id": self.user_id,
        }
        token_storage.save(self.user_id, data)

    def load_token(self) -> bool:
        data = token_storage.load(self.user_id)
        if not data:
            return False

        try:
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
                self.credentials.refresh(GoogleRequest())
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


# ---------
# OAUTH CALLBACK - FASTMCP CUSTOM ROUTES
# ---------


def make_html_response(code: int, body: str) -> HTMLResponse:
    """Create HTML response with styling."""
    html = f"""<!DOCTYPE html>
<html><head><title>MCP OAuth</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; 
       text-align: center; padding: 50px; background: #f5f5f5; }}
h1 {{ color: #333; }}
</style></head>
<body>{body}</body></html>"""
    return HTMLResponse(content=html, status_code=code)


@mcp.custom_route("/callback", methods=["GET"])
async def handle_oauth_callback(request: Request) -> Response:
    """Handle OAuth callback - automatically complete auth (CLOUD MODE)."""
    params = dict(request.query_params)
    code = params.get("code")
    state = params.get("state")
    error = params.get("error")

    logger.info(
        f"OAuth callback received - state: {state[:16] if state else 'None'}..."
    )

    if error:
        session = session_manager.get_session_by_state(state) if state else None
        if session:
            session.auth_error = error
            session.auth_completed = True

        return make_html_response(
            400,
            f"""
            <h1>Authorization Failed</h1>
            <p>Error: {error}</p>
            <p>{params.get("error_description", "")}</p>
            <p>You can close this window.</p>
        """,
        )

    if not code or not state:
        return make_html_response(
            400, "<h1>Invalid callback</h1><p>Missing code or state parameter.</p>"
        )

    session = session_manager.get_session_by_state(state)
    if not session:
        logger.error(f"No session found for state: {state[:16]}...")
        return make_html_response(
            400,
            "<h1>Session expired or not found</h1><p>Please try authenticating again.</p>",
        )

    if not session.pending_auth:
        logger.error(f"No pending auth for session: {session.session_id[:8]}...")
        return make_html_response(
            400,
            "<h1>No pending authentication</h1><p>Please start authentication again.</p>",
        )

    try:
        flow = session.pending_auth.get("flow")
        if not flow:
            raise ValueError("No OAuth flow found in session")

        # Exchange code for tokens
        logger.info("Exchanging authorization code for tokens...")
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

        logger.info(f"OAuth complete for {session.email}")

        return make_html_response(
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
        return make_html_response(500, f"<h1>Error</h1><p>{e}</p>")


@mcp.custom_route("/health", methods=["GET"])
async def handle_health(request: Request) -> Response:
    """Health check endpoint."""
    return PlainTextResponse("OK")


@mcp.custom_route("/auth/check", methods=["GET"])
async def handle_auth_check(request: Request) -> Response:
    """Check auth status for polling."""
    user_id = request.query_params.get("user_id")

    if not user_id:
        return JSONResponse({"error": "Missing user_id"}, status_code=400)

    session = session_manager.get_session_by_user(user_id)
    if not session:
        return JSONResponse({"authenticated": False, "pending": False}, status_code=404)

    return JSONResponse(
        {
            "authenticated": session.is_authenticated(),
            "pending": session.pending_auth is not None,
            "completed": session.auth_completed,
            "email": session.email,
            "error": session.auth_error,
        }
    )


@mcp.custom_route("/", methods=["GET"])
async def handle_root(request: Request) -> Response:
    """Root endpoint with server info."""
    return JSONResponse(
        {
            "name": "DayaTech MCP Server",
            "version": "0.0.1",
            "is_cloud": IS_CLOUD,
            "callback_url": CALLBACK_URL,
            "storage_mode": STORAGE_MODE,
            "endpoints": {
                "mcp": "/mcp",
                "callback": "/callback",
                "health": "/health",
                "auth_check": "/auth/check",
            },
        }
    )


# ---------
# AUTHENTICATION FLOW
# ---------


def start_oauth_flow(user_id: str, open_browser: bool = True) -> dict:
    """Start OAuth flow for a user - returns immediately, callback handles completion."""
    if not GOOGLE_CALENDAR_AVAILABLE:
        return {"success": False, "error": "Google Calendar libraries not installed"}

    if not CREDENTIALS_FILE.exists():
        return {
            "success": False,
            "error": "credentials.json not found. Set GOOGLE_CREDENTIALS_JSON env var.",
        }

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
        # use the main server's /callback route
        callback_url = CALLBACK_URL

        logger.info(f"Starting OAuth flow with callback: {callback_url}")

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
        logger.info(
            f"Registered auth state: {state[:16]}... -> session {session.session_id[:8]}..."
        )

        # Open browser
        if open_browser:
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


# ---------
# WEATHER HELPERS
# ---------


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


# ---------
# DAYAAWAY HELPERS
# ---------


async def fetch_employee() -> dict | None:
    url = "https://script.googleusercontent.com/macros/echo?user_content_key=AehSKLhnfPHRcF-ABZF8yYQ5SSHe2mKdPSWsoV6RjMz14KhdvNIBQ2zqxtwMIiMCFz3aqp3DPNRCtfoWhkpoxh2133fbvypLT460l9HY2hVY-YGMt39ZByDEt5fL5sbp6R5idJhOh0czcbgrDWMouTGQjJ3NcRPwNXDvXhjJZh-aSwkJa1IGwp52mYtB2T059H0DnbfcjR4kNTJ53kBF7NypQmdGONpwGkKHzbEmZ_EVPfnxm8vk81Br-7sfl-YRqYsS4OM_EtftRqAHPqAIemGU94tmKHvbzXeXbJew66NGbcwG5hY6eyIE0Jj0r-Yrd1-jObsouUjv59uLCowqDak&lib=MccofaXFiKY93gvVI0QdCdHoCq6hyld7b"
    headers = {
        "Accept": "application/json, text/plain, */*",
    }
    async with httpx.AsyncClient(follow_redirects=True, max_redirects=10) as client:
        try:
            resp = await client.get(
                url,
                headers=headers,
                timeout=15.0,
            )
            return json.loads(resp.text)
        except Exception:
            return None


async def fetch_onleave_employee() -> dict | None:
    url = "https://script.googleusercontent.com/macros/echo?user_content_key=AehSKLgarGzN7GaLXxKiuYiDofl2W-yey7EoeYfrLlgwkCgTafasgxQCWJd-F3e8SRYW-6PAOMtIctRKbB1Dd49xw40q69KgMg_mPf9V2Vrxee3yQzwHqMtrGqK6nIolps1X2-wfq_FeeFr-uTwKDbCuvsBjgmg2BgIKIL9cLYF_O3fG4mSPr-Uqrw-m2Pe5-HJ2W-GSn3Zi6f1VZf1aRDJnQ43WeGxtWPRhLJcjk5RVbh3wHovwI9JcpYTOqTU34IHlwRnvQFDUfisAxVMPMC0xS0QqXXHGtZJtvswggq9LMeSHPaqoLTmBPhhIR-K0d2Q26ksNnVCZ19E5lrwJQgY&lib=MccofaXFiKY93gvVI0QdCdHoCq6hyld7b"
    headers = {
        "Accept": "application/json, text/plain, */*",
    }
    async with httpx.AsyncClient(follow_redirects=True, max_redirects=10) as client:
        try:
            resp = await client.get(
                url,
                headers=headers,
                timeout=15.0,
            )
            return json.loads(resp.text)
        except Exception:
            return None


# ---------
# MCP TOOLS
# ---------


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
def get_current_time(city: str) -> str:
    """Get current date and time by city name.

    Args:
        city: City name (e.g., "Tokyo", "London", "Jakarta", "Bandung")
    """
    try:
        geolocator = Nominatim(user_agent="city_time_lookup")
        tf = TimezoneFinder()

        location = geolocator.geocode(city)
        if not location:
            return f"⚠️ City '{city}' not found. Please check the spelling."

        timezone_str = tf.timezone_at(lng=location.longitude, lat=location.latitude)  # type: ignore

        if not timezone_str:
            return f"⚠️ Could not determine the timezone for '{city}'."

        tz = ZoneInfo(timezone_str)
        now = datetime.now(tz)
        return f"🕐 Current time in {city}: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"

    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def get_dayatech_employee() -> str:
    """Get dayatech employee."""

    employee = await fetch_employee()

    if not employee:
        return "❌ Could not fetch employee"

    employee_data = employee.get("data", [])

    result = "\n".join(
        f"{i['emp_id']} | {i['nama']} | {i['tim']}" for i in employee_data
    )

    return result


@mcp.tool()
async def get_dayatech_on_leave_employee() -> str:
    """Get dayatech on leave employee."""

    employee = await fetch_onleave_employee()

    if not employee:
        return "❌ Could not fetch on leave employee"

    total_onleave = employee.get("cuti", 0)
    total_permit = employee.get("izin", 0)
    employee_data = employee.get("rows", [])

    employee_leaves = "\n".join(
        f"- {i['nama']} | {i['tim']} | {i['jenis']} | {i['keterangan']} | {i['tanggal']} "
        for i in employee_data
    )

    result = f"""- Total on leave: {total_onleave}
- Total on izin: {total_permit}

Employee:
{employee_leaves}
"""
    return result


@mcp.tool()
def connect_google_calendar(user_id: str) -> str:
    """Connect to Google Calendar via OAuth.

    Args:
        user_id: Unique identifier for this user (any string you choose combine with UUID)
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "❌ Google Calendar libraries not installed"

    result = start_oauth_flow(user_id, open_browser=True)

    if result.get("authenticated"):
        return f"""✅ Already connected as {result.get("email")}

You can now use:
• list_calendar_events(user_id="{user_id}")
• add_calendar_event(user_id="{user_id}", ...)
• delete_calendar_event(user_id="{user_id}", event_id="...")"""

    if result.get("success"):
        auth_url = result.get("auth_url", "")
        return f"""🔐 Please authenticate with Google Calendar:

**Open this URL in your browser:**
{auth_url}

After completing authentication, the connection will be automatic.

Your user_id: `{user_id}`

**Environment:** {"Cloud" if IS_CLOUD else "Local"}
**Callback URL:** {CALLBACK_URL}"""

    return f"❌ Error: {result.get('error', 'Unknown error')}"


@mcp.tool()
def check_calendar_connection(user_id: str) -> str:
    """Check Google Calendar connection status.

    Args:
        user_id: The user ID to check
    """
    session = session_manager.get_session_by_user(user_id)

    if not session:
        return f"""❌ No session found for user: {user_id}

Use connect_google_calendar(user_id="{user_id}") to connect."""

    if session.is_authenticated():
        return f"""✅ Connected to Google Calendar

📧 Email: {session.email}
🆔 User ID: {user_id}

You can now use calendar tools with user_id="{user_id}"."""

    if session.pending_auth:
        return f"""⏳ Authentication pending...

Please complete the OAuth flow in your browser.
If the browser didn't open, use connect_google_calendar(user_id="{user_id}") to get a new link."""

    if session.auth_error:
        return f"""❌ Authentication failed: {session.auth_error}

Try again with connect_google_calendar(user_id="{user_id}")"""

    return f"""❌ Not connected.

Use connect_google_calendar(user_id="{user_id}") to connect."""


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

    # Remove from storage
    token_storage.delete(user_id)

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


# ---------
# MCP RESOURCES
# ---------


@mcp.resource("config://settings")
def get_config() -> str:
    return json.dumps(
        {
            "version": VERSION,
            "multi_user": True,
            "auto_callback": True,
            "callback_url": CALLBACK_URL,
            "is_cloud": IS_CLOUD,
            "storage_mode": STORAGE_MODE,
            "active_users": len(session_manager.user_sessions),
            "stored_users": len(token_storage.list_users()),
            "google_available": GOOGLE_CALENDAR_AVAILABLE,
        },
        indent=2,
    )


# ---------
# MCP PROMPTS
# ---------


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
    return json.dumps({"users": users, "storage_mode": STORAGE_MODE}, indent=2)


@mcp.prompt()
def travel_advisory(cities: str) -> str:
    """Generate a travel weather advisory prompt for multiple cities.

    Args:
        cities: Comma-separated list of cities (e.g., "London, Paris, Tokyo")

    Use this prompt to compare weather across multiple destinations.
    """
    logger.info(f"Generating travel advisory prompt for: {cities}")

    city_list = [c.strip() for c in cities.split(",")]

    return f"""Please provide a travel weather advisory comparing these destinations: {", ".join(city_list)}

For each city, use the get_weather tool to fetch current conditions.

Then provide:

1. **Weather Comparison Table**
   - Compare temperatures, conditions, and humidity across all cities

2. **Packing Recommendations**
   - What to pack that works for all destinations
   - Destination-specific items needed

3. **Best Time to Visit**
   - Rank the cities by current weather pleasantness
   - Note any weather concerns for each location

4. **Travel Tips**
   - Weather-related travel advice for each destination
   - Any alerts or warnings to be aware of

Cities to analyze: {", ".join(city_list)}

Please fetch weather for each city and then compile your advisory."""


# ---------
# MAIN
# ---------


def main():
    logger.info("=" * 60)
    logger.info("MCP Multi-User Calendar Server (Cloud-Ready)")
    logger.info("=" * 60)
    logger.info(f"Version: {VERSION}")
    logger.info(f"Host: {HOST}:{PORT}")
    logger.info(f"Callback: {CALLBACK_URL}")
    logger.info(f"Mode: {'Cloud' if IS_CLOUD else 'Local'}")
    logger.info(f"Storage: {STORAGE_MODE}")
    logger.info(f"Google: {'✓' if GOOGLE_CALENDAR_AVAILABLE else '✗'}")
    logger.info("=" * 60)

    if IS_CLOUD:
        logger.info("Cloud mode")
    else:
        logger.info("Local mode")

    logger.info(f"MCP endpoint: http://{HOST}:{PORT}/mcp")
    logger.info("Custom routes: /, /callback, /health, /auth/check")
    logger.info("Ready!")

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
