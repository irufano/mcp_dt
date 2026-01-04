# MCP Samples

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


## Set up API Key

Copy a `.env.example` and rename to `.env` in the project root:

Replace with your key:
```bash
GEMINI_API_KEY=your-api-key-here
```

Get your free API key from: https://aistudio.google.com/apikey


## Running the Examples

### Option 1: Stdio Transport (Recommended for local use)

The Stdio client automatically starts the server as a subprocess and provides
an interactive chat interface powered by Gemini.

```bash
cd mcp_samples
python clients/stdio_client.py
```

### Option 2: Streamable HTTP Transport (For remote/distributed use)

#### Terminal 1 - Start the HTTP server:
```bash
cd mcp_samples
python servers/http_server.py
```

The server will start at `http://localhost:8000/mcp`

#### Terminal 2 - Run the HTTP client with Gemini chat:
```bash
cd mcp_samples
python clients/http_client.py
```


Terima kasih kepada teman-teman yang telah mengikuti AI Class hingga tahap ini. Mohon kesediaannya untuk mengisi feedback melalui link dibawah ini.  Masukan Anda sangat berarti dalam mendukung pengembangan AI Class selanjutnya.

https://docs.google.com/forms/d/e/1FAIpQLSf44GYat6qAVX9hYjFLyD8HnrmvOqpR4JyGjwUikE6F_baIcA/viewform?usp=dialog