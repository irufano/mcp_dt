# DayaTech MCP

## Install UV
- macOS/Linux
```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

- Windows
```sh
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Create virtual environment and activate it
- macOS/Linux
```sh
uv venv
source .venv/bin/activate
```

- Windows
```sh
uv venv
.venv\Scripts\activate
```

## Install dependencies
```sh
uv add "mcp[cli]" httpx google-genai python-dotenv google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
```


## Set up Environtment  (API Key)

Copy a `.env.example` and rename to `.env` in the project root:

Replace with your key:
```bash
GEMINI_API_KEY=your-api-key-here
```

Get your free API key from: https://aistudio.google.com/apikey

## Setup Google Calendar (Optional if want use google calendar service) :
1. Create OAuth 2.0 Client ID (Web Application) in Google Cloud Console
2. Add authorized redirect URIs:
   - http://localhost:8000/callback (for local)
   - https://your-cloud-domain.com/callback (for cloud)
3. Set GOOGLE_CREDENTIALS_JSON env var (required for cloud)
4. Download `credentials.json` and put in root of project (required for local)

## Running the Examples

### Option 1: Stdio Transport (Recommended for local use)

The Stdio client automatically starts the server as a subprocess and provides
an interactive chat interface powered by Gemini.

```bash
python stdio_client.py
```

### Option 2: Streamable HTTP Transport (For remote/distributed use)

#### Terminal 1 - Start the HTTP server:
```bash
python http_server.py
```

The server will start at `http://localhost:8000/mcp`

#### Terminal 2 - Run the HTTP client with Gemini chat:
```bash
python http_client.py
```
