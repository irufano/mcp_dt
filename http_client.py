"""
MCP Client with Automatic OAuth Callback

AUTOMATIC AUTHENTICATION:
- No manual session ID copy-paste required
- OAuth callback automatically completes authentication
- Client tracks user_id for seamless calendar access

WORKFLOW:
1. User asks to see calendar
2. AI calls connect_google_calendar(user_id="...")
3. Browser opens automatically (local) or shows auth URL (cloud)
4. User signs in with Google
5. Callback completes - user is connected
6. Calendar tools work automatically

Run:
    python http_client.py
    python http_client.py http://localhost:8000/mcp
    python http_client.py https://your-server.com/mcp
"""

import asyncio
import logging
import os
import sys
import uuid
from typing import Any, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("mcp-client")


def get_server_url() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.environ.get("MCP_SERVER_URL", "http://localhost:8000/mcp")


SERVER_URL = get_server_url()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def mcp_tool_to_genai_declaration(tool: mcp_types.Tool) -> types.FunctionDeclaration:
    """Convert MCP tool to GenAI FunctionDeclaration."""
    parameters = None

    if tool.inputSchema:
        properties = {}
        required = tool.inputSchema.get("required", [])

        for prop_name, prop_info in tool.inputSchema.get("properties", {}).items():
            prop_type = prop_info.get("type", "string").upper()
            type_mapping = {
                "STRING": "STRING",
                "INTEGER": "INTEGER",
                "NUMBER": "NUMBER",
                "BOOLEAN": "BOOLEAN",
                "ARRAY": "ARRAY",
                "OBJECT": "OBJECT",
            }
            properties[prop_name] = {
                "type": type_mapping.get(prop_type, "STRING"),
                "description": prop_info.get("description", ""),
            }

        if properties:
            parameters = {
                "type": "OBJECT",
                "properties": properties,
                "required": required,
            }

    return types.FunctionDeclaration(
        name=tool.name,
        description=tool.description or f"Tool: {tool.name}",
        parameters=parameters,  # type: ignore
    )


class MCPAutoCallbackClient:
    """MCP Client with automatic OAuth callback handling."""

    def __init__(self, server_url: str = SERVER_URL):
        self.server_url = server_url
        self.session: Optional[ClientSession] = None
        self.genai_client: Optional[genai.Client] = None
        self.tools: list[mcp_types.Tool] = []
        self.resources: list = []
        self.prompts: list = []
        self.function_declarations: list[types.FunctionDeclaration] = []
        self.chat_history: list[types.Content] = []

        # User ID for this client session - used for calendar auth
        self.user_id: str = os.environ.get("USER_ID") or str(uuid.uuid4())[:16]

        # System prompt with automatic user_id injection
        self.SYSTEM_PROMPT = f"""You are a helpful assistant with access to weather, time, and Google Calendar tools.

IMPORTANT - AUTOMATIC AUTHENTICATION:
- The user_id for this session is: "{self.user_id}"
- When user asks about calendar, ALWAYS use this user_id
- Authentication happens automatically via browser callback
- No need to ask user for session IDs or codes

CALENDAR WORKFLOW:
1. First calendar request: Call connect_google_calendar(user_id="{self.user_id}")
   - For local: Browser opens automatically for user to sign in
   - For cloud: Shows auth URL that user must open
   - Wait for auth to complete (the tool handles this)
   
2. Subsequent requests: Use the same user_id="{self.user_id}" for all calendar tools:
   - list_calendar_events(user_id="{self.user_id}")
   - add_calendar_event(user_id="{self.user_id}", title="...", ...)
   - delete_calendar_event(user_id="{self.user_id}", event_id="...")

3. To check connection: check_calendar_connection(user_id="{self.user_id}")

4. To disconnect: disconnect_google_calendar(user_id="{self.user_id}")

NEVER ask the user for their user_id - always use "{self.user_id}".
The OAuth callback handles everything automatically.

For cloud deployments:
- If connect_google_calendar returns an auth URL, show it to the user
- Ask user to open the URL, sign in, then come back
- After they say they've signed in, call check_calendar_connection to verify"""

        # Initialize chat with system prompt
        self.chat_history.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"System: {self.SYSTEM_PROMPT}")],
            )
        )
        self.chat_history.append(
            types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text=f"Understood. I'll use user_id '{self.user_id}' for all calendar operations. Authentication will happen automatically through browser callback."
                    )
                ],
            )
        )

    async def initialize_genai(self):
        """Initialize Google GenAI client."""
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError(
                "Set GEMINI_API_KEY or GOOGLE_API_KEY environment variable.\n"
                "Get key from: https://aistudio.google.com/apikey"
            )
        self.genai_client = genai.Client(api_key=api_key)
        logger.info("GenAI client initialized")

    async def connect_to_server(self):
        """Connect to MCP server."""
        logger.info(f"Connecting to: {self.server_url}")

        try:
            self._http_cm = streamable_http_client(self.server_url)
            read_stream, write_stream, _ = await self._http_cm.__aenter__()

            self._session_cm = ClientSession(read_stream, write_stream)
            self.session = await self._session_cm.__aenter__()

            await self.session.initialize()
            logger.info("MCP session initialized")

            # Get tools
            tools_response = await self.session.list_tools()
            self.tools = tools_response.tools

            # Get resources
            try:
                resources_response = await self.session.list_resources()
                self.resources = resources_response.resources
            except Exception:
                self.resources = []

            # Convert to GenAI format
            self.function_declarations = [
                mcp_tool_to_genai_declaration(tool) for tool in self.tools
            ]

            logger.info(f"Loaded {len(self.tools)} tools")

        except ConnectionRefusedError:
            raise ConnectionRefusedError(
                f"Cannot connect to: {self.server_url}\n"
                "Start the server: python http_server.py"
            )

    async def cleanup(self):
        """Clean up connections."""
        if hasattr(self, "_session_cm"):
            await self._session_cm.__aexit__(None, None, None)
        if hasattr(self, "_http_cm"):
            await self._http_cm.__aexit__(None, None, None)

    async def call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool."""
        if not self.session:
            return "Error: Not connected"

        try:
            result = await self.session.call_tool(tool_name, arguments=arguments)

            if result.isError:
                return f"Tool error: {result.content}"

            text_parts = []
            for content in result.content:
                if isinstance(content, mcp_types.TextContent):
                    text_parts.append(content.text)
                else:
                    text_parts.append(str(content))

            return "\n".join(text_parts)

        except Exception as e:
            logger.error(f"Tool error {tool_name}: {e}")
            return f"Error: {str(e)}"

    async def process_message(self, user_message: str) -> str:
        """Process user message."""
        if not self.genai_client:
            return "Error: GenAI not initialized"

        # Add user message
        self.chat_history.append(
            types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
        )

        # Configure tools
        tools = None
        if self.function_declarations:
            tools = [types.Tool(function_declarations=self.function_declarations)]

        config = types.GenerateContentConfig(
            tools=tools,  # type: ignore
            automatic_function_calling=types.AutomaticFunctionCallingConfig(  # type: ignore
                disable=True
            ),
            temperature=0.7,
        )

        try:
            response = self.genai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=self.chat_history,
                config=config,
            )

            # Handle function calls
            if response.function_calls:
                function_responses = []

                for func_call in response.function_calls:
                    logger.info(f"Calling: {func_call.name}")

                    args = dict(func_call.args) if func_call.args else {}
                    logger.info(f"Args: {args}")

                    result = await self.call_mcp_tool(func_call.name, args)  # type: ignore

                    function_responses.append(
                        types.Part.from_function_response(
                            name=func_call.name,  # type: ignore
                            response={"result": result},
                        )
                    )

                    print(f"\n🔧 {func_call.name}")
                    preview = result[:400] + "..." if len(result) > 400 else result
                    print(f"   {preview}")

                # Add to history
                self.chat_history.append(
                    types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name=fc.name,  # type: ignore
                                args=dict(fc.args) if fc.args else {},
                            )
                            for fc in response.function_calls
                        ],
                    )
                )

                self.chat_history.append(
                    types.Content(role="user", parts=function_responses)
                )

                # Get final response
                final_response = self.genai_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=self.chat_history,
                    config=config,
                )

                assistant_message = final_response.text or "Done."

                self.chat_history.append(
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=assistant_message)],
                    )
                )

                return assistant_message
            else:
                assistant_message = response.text or "I'm not sure how to respond."

                self.chat_history.append(
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=assistant_message)],
                    )
                )

                return assistant_message

        except Exception as e:
            logger.error(f"Error: {e}")
            return f"Error: {str(e)}"

    async def read_resource(self, uri: str) -> str:
        """Read MCP resource."""
        if not self.session:
            return "Not connected"

        try:
            from pydantic import AnyUrl

            result = await self.session.read_resource(AnyUrl(uri))

            parts = []
            for content in result.contents:
                if hasattr(content, "text"):
                    parts.append(content.text)  # type: ignore
                else:
                    parts.append(str(content))

            return "\n".join(parts)
        except Exception as e:
            return f"Error: {e}"

    async def chat_loop(self):
        """Interactive chat loop."""
        print("\n" + "=" * 60)
        print("MCP CLIENT WITH AUTO-CALLBACK OAUTH")
        print("=" * 60)
        print(f"Server: {self.server_url}")
        print(f"Model: {GEMINI_MODEL}")
        print(f"User ID: {self.user_id}")
        print("=" * 60)
        print("\nCommands:")
        print("  'tools'     - List tools")
        print("  'resources' - List resources")
        print("  'status'    - Check calendar connection")
        print("  'connect'   - Connect to Google Calendar")
        print("  'config'    - Show server config")
        print("  'clear'     - Clear chat")
        print("  'quit'      - Exit")
        print("=" * 60)
        print("\nTry: 'show my calendar' or 'add meeting tomorrow at 2pm'")
        print("=" * 60 + "\n")

        while True:
            try:
                user_input = input("\n👤 You: ").strip()

                if not user_input:
                    continue

                # Commands
                if user_input.lower() in ["quit", "exit", "q"]:
                    print("\nGoodbye! 👋")
                    break

                if user_input.lower() == "tools":
                    print("\n📦 Tools:")
                    for tool in self.tools:
                        print(f"   • {tool.name}: {tool.description[:60]}...")  # type: ignore
                    continue

                if user_input.lower() == "resources":
                    print("\n📚 Resources:")
                    for resource in self.resources:
                        print(f"   • {resource.uri}")
                    continue

                if user_input.lower() == "status":
                    result = await self.call_mcp_tool(
                        "check_calendar_connection", {"user_id": self.user_id}
                    )
                    print(f"\n{result}")
                    continue

                if user_input.lower() == "connect":
                    print("\n🔐 Starting Google Calendar connection...")
                    result = await self.call_mcp_tool(
                        "connect_google_calendar",
                        {"user_id": self.user_id, "wait": True},
                    )
                    print(f"\n{result}")
                    continue

                if user_input.lower() == "config":
                    config = await self.read_resource("config://settings")
                    print(f"\n⚙️ Server Config:\n{config}")
                    continue

                if user_input.lower() == "clear":
                    self.chat_history = self.chat_history[:2]  # Keep system prompt
                    print("\n🗑️ Chat cleared")
                    continue

                # Process message
                response = await self.process_message(user_input)
                print(f"\n🤖 Gemini: {response}")

            except KeyboardInterrupt:
                print("\n\nInterrupted. Goodbye! 👋")
                break
            except Exception as e:
                logger.error(f"Error: {e}")
                print(f"\n❌ Error: {e}")


async def run_client():
    """Run the client."""
    client = MCPAutoCallbackClient(server_url=SERVER_URL)

    try:
        await client.initialize_genai()
        await client.connect_to_server()

        print("\n" + "=" * 60)
        print("CONNECTED TO MCP SERVER")
        print("=" * 60)
        print(f"URL: {SERVER_URL}")
        print(f"User ID: {client.user_id}")

        print(f"\n📦 Tools ({len(client.tools)}):")
        for tool in client.tools:
            print(f"   • {tool.name}")

        # Check server config
        try:
            config = await client.read_resource("config://settings")
            print(f"\n⚙️ Server:\n{config}")
        except Exception:
            pass

        await client.chat_loop()

    finally:
        await client.cleanup()


def main():
    print("\n" + "=" * 60)
    print("MCP AUTO-CALLBACK CLIENT")
    print("=" * 60)
    print(f"Model: {GEMINI_MODEL}")
    print(f"Server: {SERVER_URL}")
    print("\n⚠️ Start the server first: python http_server.py\n")

    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        print("\n\nStopped")
    except ConnectionRefusedError as e:
        print(f"\n❌ {e}")
    except Exception as e:
        logger.error(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
