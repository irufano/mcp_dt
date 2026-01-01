"""
MCP Client using Streamable HTTP Transport with Google GenAI Chat Loop

This client:
1. Connects to an MCP server via Streamable HTTP transport
2. Retrieves available tools from the server
3. Converts MCP tools to Google GenAI function declarations
4. Runs an interactive chat loop where Gemini can call MCP tools

Prerequisites:
- Set GEMINI_API_KEY or GOOGLE_API_KEY environment variable
- Or create a .env file with the API key
- Start the HTTP server first: python servers/http_server.py

Run with:
    python client/http_client.py
"""

import asyncio
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("mcp-genai-http-client")

# Server URL - from command line, environment, or default
def get_server_url() -> str:
    # 1. Command line argument
    if len(sys.argv) > 1:
        return sys.argv[1]
    # 2. Environment variable
    if os.environ.get("MCP_SERVER_URL"):
        return os.environ["MCP_SERVER_URL"]
    # 3. Default local
    return "http://localhost:8000/mcp"

SERVER_URL = get_server_url()
logger.info(f"SERVER_URL: {SERVER_URL}")

# Gemini model to use
GEMINI_MODEL = "gemini-2.5-flash"


def mcp_tool_to_genai_declaration(tool: mcp_types.Tool) -> types.FunctionDeclaration:
    """Convert an MCP tool to a Google GenAI FunctionDeclaration."""
    # Extract parameters from MCP tool schema
    parameters = None
    if tool.inputSchema:
        # Convert MCP schema to GenAI format
        properties = {}
        required = tool.inputSchema.get("required", [])

        for prop_name, prop_info in tool.inputSchema.get("properties", {}).items():
            prop_type = prop_info.get("type", "string").upper()
            # Map common types
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


class MCPGenAIClient:
    """MCP Client that integrates with Google GenAI for chat functionality."""

    def __init__(self, server_url: str = SERVER_URL):
        self.server_url = server_url
        self.session: ClientSession | None = None
        self.genai_client: genai.Client | None = None
        self.tools: list[mcp_types.Tool] = []
        self.resources: list = []  # MCP resources
        self.prompts: list = []  # MCP prompts
        self.function_declarations: list[types.FunctionDeclaration] = []
        self.chat_history: list[types.Content] = []

    async def initialize_genai(self):
        """Initialize the Google GenAI client."""
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError(
                "Please set GEMINI_API_KEY or GOOGLE_API_KEY environment variable.\n"
                "Get your API key from: https://aistudio.google.com/apikey"
            )

        self.genai_client = genai.Client(api_key=api_key)
        logger.info("Google GenAI client initialized")

    async def connect_to_server(self):
        """Connect to an MCP server via HTTP and retrieve tools."""
        logger.info(f"Connecting to MCP server at: {self.server_url}")

        try:
            # Create the connection context manager
            self._http_cm = streamable_http_client(self.server_url)
            read_stream, write_stream, _ = await self._http_cm.__aenter__()

            self._session_cm = ClientSession(read_stream, write_stream)
            self.session = await self._session_cm.__aenter__()

            # Initialize the connection
            await self.session.initialize()
            logger.info("MCP session initialized")

            # Get available tools
            tools_response = await self.session.list_tools()
            self.tools = tools_response.tools

            # Get available resources
            resources_response = await self.session.list_resources()
            self.resources = resources_response.resources

            # Get available prompts
            prompts_response = await self.session.list_prompts()
            self.prompts = prompts_response.prompts

            # Convert MCP tools to GenAI function declarations
            self.function_declarations = [
                mcp_tool_to_genai_declaration(tool) for tool in self.tools
            ]

            logger.info(
                f"Loaded {len(self.tools)} tools, {len(self.resources)} resources, {len(self.prompts)} prompts"
            )

        except ConnectionRefusedError:
            raise ConnectionRefusedError(
                f"Could not connect to MCP server at: {self.server_url}\n"
                "Make sure the HTTP server is running:\n"
                "    python servers/http_server.py"
            )

    async def cleanup(self):
        """Clean up connections."""
        if hasattr(self, "_session_cm"):
            await self._session_cm.__aexit__(None, None, None)
        if hasattr(self, "_http_cm"):
            await self._http_cm.__aexit__(None, None, None)

    async def call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return the result as a string."""
        if not self.session:
            return "Error: Not connected to MCP server"

        try:
            result = await self.session.call_tool(tool_name, arguments=arguments)

            if result.isError:
                return f"Tool error: {result.content}"

            # Extract text content from result
            text_parts = []
            for content in result.content:
                if isinstance(content, mcp_types.TextContent):
                    text_parts.append(content.text)
                else:
                    text_parts.append(str(content))

            return "\n".join(text_parts)

        except Exception as e:
            logger.error(f"Error calling tool {tool_name}: {e}")
            return f"Error calling tool: {str(e)}"

    async def process_message(self, user_message: str) -> str:
        """Process a user message and return the assistant's response."""
        if not self.genai_client:
            return "Error: GenAI client not initialized"

        # Add user message to history
        self.chat_history.append(
            types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
        )

        # Create tool configuration
        tools = None
        if self.function_declarations:
            tools = [types.Tool(function_declarations=self.function_declarations)]

        # Generate response with function calling disabled for manual handling
        config = types.GenerateContentConfig(
            tools=tools,  # type: ignore
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
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

            # Check if the model wants to call a function
            if response.function_calls:
                # Process each function call
                function_responses = []
                for func_call in response.function_calls:
                    logger.info(f"Calling MCP tool: {func_call.name}")
                    logger.info(f"Arguments: {func_call.args}")

                    # Call the MCP tool
                    result = await self.call_mcp_tool(
                        func_call.name,  # type: ignore
                        dict(func_call.args) if func_call.args else {},
                    )

                    function_responses.append(
                        types.Part.from_function_response(
                            name=func_call.name,  # type: ignore
                            response={"result": result},
                        )
                    )

                    print(f"\n🔧 Called tool: {func_call.name}")
                    print(
                        f"   Result: {result[:200]}..."
                        if len(result) > 200
                        else f"   Result: {result}"
                    )

                # Add model's function call to history
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

                # Add function responses to history
                self.chat_history.append(
                    types.Content(role="user", parts=function_responses)
                )

                # Get final response after function execution
                final_response = self.genai_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=self.chat_history,
                    config=config,
                )

                assistant_message = final_response.text or "I processed your request."

                # Add assistant response to history
                self.chat_history.append(
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=assistant_message)],
                    )
                )

                return assistant_message
            else:
                # No function call, just return the text response
                assistant_message = (
                    response.text or "I'm not sure how to respond to that."
                )

                # Add assistant response to history
                self.chat_history.append(
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=assistant_message)],
                    )
                )

                return assistant_message

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return f"Error: {str(e)}"

    async def chat_loop(self):
        """Run an interactive chat loop."""
        print("\n" + "=" * 60)
        print("CHAT WITH GEMINI + MCP TOOLS (HTTP)")
        print("=" * 60)
        print("Commands:")
        print("  'tools'     - List available tools")
        print("  'resources' - List available resources")
        print("  'prompts'   - List available prompts")
        print("  'read <uri>'- Read a resource (e.g., 'read config://settings')")
        print("  'use <name>'- Use a prompt (e.g., 'use weather_report')")
        print("  'clear'     - Clear chat history")
        print("  'quit'      - Exit")
        print("=" * 60 + "\n")

        if self.session is None:
            raise ValueError("Initialize first")

        while True:
            try:
                user_input = input("\n👤 You: ").strip()

                if not user_input:
                    continue

                if user_input.lower() in ["quit", "exit", "q"]:
                    print("\nGoodbye! 👋")
                    break

                if user_input.lower() == "tools":
                    print("\n📦 Available MCP Tools:")
                    for tool in self.tools:
                        print(f"   • {tool.name}: {tool.description}")
                    continue

                if user_input.lower() == "resources":
                    print("\n📚 Available MCP Resources:")
                    for resource in self.resources:
                        print(
                            f"   • {resource.uri}: {resource.name or 'No description'}"
                        )
                    # Also show resource templates
                    templates_response = await self.session.list_resource_templates()
                    if templates_response.resourceTemplates:
                        print("\n📋 Resource Templates (dynamic):")
                        for template in templates_response.resourceTemplates:
                            print(
                                f"   • {template.uriTemplate}: {template.name or 'No description'}"
                            )
                    continue

                if user_input.lower() == "prompts":
                    print("\n💬 Available MCP Prompts:")
                    for prompt in self.prompts:
                        args = ", ".join([a.name for a in (prompt.arguments or [])])
                        print(
                            f"   • {prompt.name}({args}): {prompt.description or 'No description'}"
                        )
                    continue

                if user_input.lower().startswith("read "):
                    uri = user_input[5:].strip()
                    try:
                        from pydantic import AnyUrl

                        result = await self.session.read_resource(AnyUrl(uri))
                        print(f"\n📖 Resource content ({uri}):")
                        for content in result.contents:
                            if hasattr(content, "text"):
                                print(content.text)  # type: ignore
                            else:
                                print(str(content))
                    except Exception as e:
                        print(f"\n❌ Error reading resource: {e}")
                    continue

                if user_input.lower().startswith("use "):
                    prompt_name = user_input[4:].strip()
                    # Find the prompt
                    prompt_info = next(
                        (p for p in self.prompts if p.name == prompt_name), None
                    )
                    if not prompt_info:
                        print(f"\n❌ Prompt '{prompt_name}' not found")
                        continue

                    # Collect arguments if needed
                    args = {}
                    if prompt_info.arguments:
                        print(f"\nEnter arguments for '{prompt_name}':")
                        for arg in prompt_info.arguments:
                            required = "(required)" if arg.required else "(optional)"
                            value = input(f"   {arg.name} {required}: ").strip()
                            if value:
                                args[arg.name] = value

                    try:
                        result = await self.session.get_prompt(
                            prompt_name, arguments=args
                        )
                        # Use the prompt content as the user message
                        prompt_text = ""
                        for msg in result.messages:
                            if hasattr(msg.content, "text"):
                                prompt_text += msg.content.text + "\n"  # type: ignore

                        print(f"\n📝 Using prompt '{prompt_name}'...")
                        response = await self.process_message(prompt_text)
                        print(f"\n🤖 Gemini: {response}")
                    except Exception as e:
                        print(f"\n❌ Error using prompt: {e}")
                    continue

                if user_input.lower() == "clear":
                    self.chat_history = []
                    print("\n🗑️  Chat history cleared")
                    continue

                # Process the message
                response = await self.process_message(user_input)
                print(f"\n🤖 Gemini: {response}")

            except KeyboardInterrupt:
                print("\n\nInterrupted. Goodbye! 👋")
                break
            except Exception as e:
                logger.error(f"Chat error: {e}")
                print(f"\n❌ Error: {e}")


async def run_client():
    """Run the MCP GenAI client."""
    client = MCPGenAIClient(server_url=SERVER_URL)

    try:
        # Initialize GenAI
        await client.initialize_genai()

        # Connect to MCP server
        await client.connect_to_server()

        # Show available tools
        print("\n" + "=" * 60)
        print("CONNECTED TO MCP SERVER")
        print("=" * 60)
        print(f"Server URL: {SERVER_URL}")
        print(f"\n📦 Tools ({len(client.tools)}):")
        for tool in client.tools:
            print(f"   • {tool.name}: {tool.description}")

        print(f"\n📚 Resources ({len(client.resources)}):")
        for resource in client.resources:
            print(f"   • {resource.uri}")

        print(f"\n💬 Prompts ({len(client.prompts)}):")
        for prompt in client.prompts:
            print(f"   • {prompt.name}: {prompt.description or 'No description'}")

        # Run the chat loop
        await client.chat_loop()

    finally:
        await client.cleanup()


def main():
    """Entry point for the HTTP client with GenAI."""
    print("\n" + "=" * 60)
    print("MCP HTTP CLIENT WITH GOOGLE GENAI")
    print("=" * 60)
    print(f"Model: {GEMINI_MODEL}")
    print(f"Server URL: {SERVER_URL}")
    print("\n⚠️  Make sure the server is running first!")
    print("   Run: python server/http_server.py\n")

    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        print("\n\nClient interrupted by user")
    except ConnectionRefusedError as e:
        print(f"\n❌ {e}")
    except Exception as e:
        logger.error(f"Client error: {e}")
        raise


if __name__ == "__main__":
    main()
