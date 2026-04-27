"""§2 Feature Tour cells — 08 Tools."""

from __future__ import annotations


def _md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}


def _code(tag: str, source: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {"tags": [tag]},
        "source": source.splitlines(keepends=True),
        "execution_count": None,
        "outputs": [],
    }


def section_cells_python() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises the `@tool` decorator, `Tool()` factory, and agent tool registration.\n"
        ),
        _md("### `tool()` factory\n"),
        _code(
            "ft_tool_decorator",
            "from getpatter import tool\n"
            "with _setup.cell('tool_decorator', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        @tool\n"
            "        def get_weather(city: str, units: str = 'celsius') -> str:\n"
            '            """Get the current weather for a city."""\n'
            "            return f'Sunny, 22{units[0].upper()} in {city}'\n"
            "\n"
            "        print(f'name:        {get_weather.name}')\n"
            "        print(f'description: {get_weather.description}')\n"
            "        print(f'call:        {get_weather.handler(city=\"Paris\")}')\n"
            "        assert get_weather.name == 'get_weather'\n"
            "        assert 'city' in get_weather.description or get_weather.handler is not None\n",
        ),
        _md("### `Tool()` constructor\n"),
        _code(
            "ft_tool_inline",
            "from getpatter import Tool\n"
            "with _setup.cell('tool_inline', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        search_tool = Tool(\n"
            "            name='web_search',\n"
            "            description='Search the web for up-to-date information.',\n"
            "            parameters={\n"
            "                'query': {'type': 'string', 'description': 'The search query'},\n"
            "                'num_results': {'type': 'integer', 'default': 5},\n"
            "            },\n"
            "            handler=lambda query, num_results=5: f'Top {num_results} results for {query!r}',\n"
            "        )\n"
            "        print(f'name:        {search_tool.name}')\n"
            "        print(f'description: {search_tool.description}')\n"
            "        print(f'call:        {search_tool.handler(query=\"Patter SDK\", num_results=3)}')\n"
            "        assert search_tool.name == 'web_search'\n",
        ),
        _md("### Agent with tools list\n"),
        _code(
            "ft_tool_in_agent",
            "from getpatter import Patter, Twilio, OpenAIRealtime, tool, Tool\n"
            "with _setup.cell('tool_in_agent', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        @tool\n"
            "        def book_appointment(date: str, time: str) -> str:\n"
            '            """Book an appointment for the caller."""\n'
            "            return f'Appointment booked for {date} at {time}'\n"
            "\n"
            "        cancel_tool = Tool(\n"
            "            name='cancel_appointment',\n"
            "            description='Cancel an existing appointment.',\n"
            "            parameters={'confirmation_number': {'type': 'string'}},\n"
            "            handler=lambda confirmation_number: f'Cancelled {confirmation_number}',\n"
            "        )\n"
            "\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid='ACtest00000000000000000000000000', auth_token='test'),\n"
            "            phone_number='+15555550100',\n"
            "            webhook_url='https://example.com/webhook',\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a helpful booking assistant.',\n"
            "            engine=OpenAIRealtime(api_key='sk-test'),\n"
            "            tools=[book_appointment, cancel_tool],\n"
            "        )\n"
            "        print(f'Agent tools: {[t.name for t in agent.tools]}')\n"
            "        assert len(agent.tools) == 2\n"
            "        assert agent.tools[0].name == 'book_appointment'\n"
            "        assert agent.tools[1].name == 'cancel_appointment'\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises the `tool()` factory and agent tool registration.\n"
        ),
        _md("### `tool()` factory\n"),
        _code(
            "ft_tool_decorator",
            'import { tool } from "getpatter";\n'
            "await cell('tool_decorator', { tier: 1, env }, () => {\n"
            "  const getWeather = tool({\n"
            "    name: 'get_weather',\n"
            "    description: 'Get the current weather for a city.',\n"
            "    parameters: { city: { type: 'string' }, units: { type: 'string', default: 'celsius' } },\n"
            "    handler: ({ city, units = 'celsius' }: { city: string; units?: string }) =>\n"
            "      `Sunny, 22${units[0].toUpperCase()} in ${city}`,\n"
            "  });\n"
            "  console.log(`name:  ${getWeather.name}`);\n"
            "  console.log(`call:  ${getWeather.handler({ city: 'Paris' })}`);\n"
            "});\n",
        ),
        _md("### `Tool()` constructor\n"),
        _code(
            "ft_tool_inline",
            'import { tool } from "getpatter";\n'
            "await cell('tool_inline', { tier: 1, env }, () => {\n"
            "  const searchTool = tool({\n"
            "    name: 'web_search',\n"
            "    description: 'Search the web for up-to-date information.',\n"
            "    parameters: { query: { type: 'string' }, numResults: { type: 'integer', default: 5 } },\n"
            "    handler: ({ query, numResults = 5 }: { query: string; numResults?: number }) =>\n"
            "      `Top ${numResults} results for '${query}'`,\n"
            "  });\n"
            "  console.log(`name: ${searchTool.name}`);\n"
            "  console.log(`call: ${searchTool.handler({ query: 'Patter SDK', numResults: 3 })}`);\n"
            "  if (searchTool.name !== 'web_search') throw new Error('wrong name');\n"
            "});\n",
        ),
        _md("### Agent with tools list\n"),
        _code(
            "ft_tool_in_agent",
            'import { Patter, Twilio, OpenAIRealtime, tool } from "getpatter";\n'
            "await cell('tool_in_agent', { tier: 1, env }, () => {\n"
            "  const bookTool = tool({\n"
            "    name: 'book_appointment',\n"
            "    description: 'Book an appointment.',\n"
            "    parameters: { date: { type: 'string' }, time: { type: 'string' } },\n"
            "    handler: ({ date, time }: { date: string; time: string }) => `Booked ${date} ${time}`,\n"
            "  });\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: 'ACtest00000000000000000000000000', authToken: 'test' }),\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'You are a booking assistant.',\n"
            "    engine: new OpenAIRealtime({ apiKey: 'sk-test' }),\n"
            "    tools: [bookTool],\n"
            "  });\n"
            "  console.log(`Agent tools: ${agent.tools?.map((t: any) => t.name)}`);\n"
            "});\n",
        ),
    ]
