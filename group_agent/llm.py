"""
Client for any OpenAI-compatible chat completions API (OpenAI, DeepSeek, Groq,
Together, vLLM, Ollama, LM Studio, ...), with tool (function) calling.

Configured with LLM_BASE_URL (e.g. https://api.deepseek.com/v1), LLM_API_KEY and
LLM_MODEL. Without them, StubLLM is used so the WhatsApp side can be tested first.
With LLM_FALLBACK_API_KEY set, FallbackLLM answers from a second model (Gemini by
default) whenever the main one fails.
"""

import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("group-agent.llm")


class LLMError(RuntimeError):
    tool_calling_refused = False


TOOL_MODES = ("auto", "tools", "json")

_JSON_INSTRUCTIONS = """

TOOLS ARE NOT AVAILABLE AS FUNCTION CALLS. Instead, answer with ONE JSON object and nothing else:
{tool_list}
- To call a tool: {{"tool": "<tool name>", "arguments": {{...}}}}
- To do nothing (stay silent): {{"tool": null}}"""


class ChatLLM:
    """tool_mode: "tools" = native function calling (vLLM needs --enable-auto-tool-choice),
    "json" = the model answers with a JSON object instead, "auto" = tools, switching to JSON
    for good if the server refuses tool calling."""

    def __init__(
        self, base_url: str, api_key: Optional[str], model: str, temperature: float = 0.3,
        timeout: float = 60, tool_mode: str = "auto",
    ):
        if tool_mode not in TOOL_MODES:
            raise ValueError(f"LLM_TOOL_MODE must be one of {', '.join(TOOL_MODES)}")
        self.url = f"{base_url}/chat/completions"
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.tool_mode = tool_mode

    @property
    def name(self) -> str:
        return f"{self.model} ({'JSON answers' if self.tool_mode == 'json' else 'tool calling'})"

    async def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Returns the assistant message: {"content": str|None, "tool_calls": [...]}."""
        if tools and self.tool_mode != "json":
            try:
                return await self._request(messages, tools)
            except LLMError as e:
                if self.tool_mode == "tools" or not e.tool_calling_refused:
                    raise
                logger.warning("The LLM server refused tool calling - switching to JSON answers: %s", e)
                self.tool_mode = "json"
        if tools:
            return await self._json_request(messages, tools)
        return await self._request(messages, None)

    async def _request(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: Dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.url, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"LLM server unreachable at {self.url}: {type(e).__name__}") from e
        if response.status_code != 200:
            error = LLMError(f"LLM request failed ({response.status_code}): {response.text[:300]}")
            text = response.text.lower()
            error.tool_calling_refused = bool(tools) and response.status_code in (400, 422) and (
                "tool" in text or "function" in text
            )
            raise error
        try:
            message = response.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"Unexpected LLM response: {response.text[:300]}") from e
        if not message.get("tool_calls") and tools:
            # Some vLLM tool parsers leave the call as text in the content; recover it
            recovered = _tool_call_from_text(message.get("content"), tools)
            if recovered:
                return {"content": None, "tool_calls": [recovered]}
        return message

    async def _json_request(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        tool_list = "\n".join(
            f"- {t['function']['name']}: {t['function']['description']} "
            f"Arguments: {json.dumps(t['function']['parameters']['properties'])}"
            for t in tools
        )
        system = messages[0]["content"] + _JSON_INSTRUCTIONS.format(tool_list=tool_list)
        # Earlier tool rounds are replayed as plain text, since this server has no tool roles
        flat = [{"role": "system", "content": system}]
        for m in messages[1:]:
            if m["role"] == "tool":
                flat.append({"role": "user", "content": f"Tool result: {m['content']}"})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                call = m["tool_calls"][0]["function"]
                flat.append({"role": "assistant", "content": json.dumps(
                    {"tool": call["name"], "arguments": json.loads(call["arguments"] or "{}")})})
            else:
                flat.append({"role": m["role"], "content": m.get("content") or ""})
        message = await self._request(flat, None)
        call = _tool_call_from_text(message.get("content"), tools)
        return {"content": None if call else message.get("content"), "tool_calls": [call] if call else []}


def _tool_call_from_text(text: Optional[str], tools: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find a {"tool"/"name": ..., "arguments": {...}} object in model text (ignoring <think> blocks)."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    names = {t["function"]["name"] for t in tools}
    decoder = json.JSONDecoder()
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool", obj.get("name"))
        if name in names:
            args = obj.get("arguments", obj.get("parameters", {}))
            return {
                "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)},
            }
        if "tool" in obj and obj["tool"] is None:
            return None  # the model chose silence
    return None


class StubLLM:
    """Test mode until a real LLM is configured: replies only when the assistant is addressed by name."""

    name = "test-mode (no LLM configured)"

    def __init__(self, assistant_name: str):
        self.trigger = assistant_name.split()[-1].lower()  # "assistant"

    async def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        if any(m.get("role") == "tool" for m in messages):
            return {"content": None, "tool_calls": []}  # already replied this round
        new_part = messages[-1]["content"].split("NEW MESSAGES", 1)[-1]
        newest = [line for line in new_part.splitlines() if line.startswith("[msg ") and " You (" not in line]
        addressed = [line for line in newest if self.trigger in line.lower()]
        if not addressed:
            return {"content": None, "tool_calls": []}
        msg_id = addressed[-1].split("]", 1)[0].removeprefix("[msg ").strip()
        return {"content": None, "tool_calls": [{
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": "send_reply", "arguments": json.dumps({
                "text": "Hello! I'm the Max Migold Assistant and I can see this chat. "
                        "(Test mode: my full replies will be switched on soon.)",
                "reply_to": msg_id,
            })},
        }]}


class FallbackLLM:
    """The main model, with a second one answering whenever it fails. After a failure the main
    model is skipped for `retry_after` seconds, so an outage doesn't add a timeout to every decision."""

    def __init__(self, primary: ChatLLM, fallback: ChatLLM, retry_after: float = 300):
        self.primary = primary
        self.fallback = fallback
        self.retry_after = retry_after
        self._primary_down_until = 0.0

    @property
    def name(self) -> str:
        return f"{self.primary.name}, fallback {self.fallback.name}"

    async def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        if time.time() >= self._primary_down_until:
            try:
                return await self.primary.complete(messages, tools)
            except LLMError as e:
                self._primary_down_until = time.time() + self.retry_after
                logger.warning("Main LLM failed, using the fallback (%s) for the next %d min: %s",
                               self.fallback.model, self.retry_after // 60, e)
        return await self.fallback.complete(messages, tools)


def build_llm(settings) -> Any:
    if not settings.llm_configured:
        return StubLLM(settings.assistant_name)
    llm = ChatLLM(
        settings.llm_base_url, settings.llm_api_key, settings.llm_model,
        settings.llm_temperature, settings.llm_timeout, settings.llm_tool_mode,
    )
    if settings.llm_fallback_configured:
        fallback = ChatLLM(
            settings.llm_fallback_base_url, settings.llm_fallback_api_key, settings.llm_fallback_model,
            settings.llm_temperature, settings.llm_timeout, "tools",
        )
        return FallbackLLM(llm, fallback)
    return llm
