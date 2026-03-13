import sys
import typing as T

import astrbot.core.message.components as Comp
from astrbot.core import logger
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.response import AgentResponse, AgentResponseData
from astrbot.core.agent.run_context import ContextWrapper, TContext
from astrbot.core.agent.runners.base import AgentState, BaseAgentRunner
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import (
    LLMResponse,
    ProviderRequest,
)
from astrbot.core.provider.register import llm_tools

from .maibot_ws_client import MaiBotWSClient

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


# Constants
MAIBOT_PROVIDER_TYPE = "maibot"
MAIBOT_AGENT_RUNNER_PROVIDER_ID_KEY = "maibot_agent_runner_provider_id"

DEFAULT_WS_URL = "ws://127.0.0.1:18040/ws"
DEFAULT_PLATFORM = "astrbot"
DEFAULT_TIMEOUT = 120


class MaiBotAgentRunner(BaseAgentRunner[TContext]):
    """MaiBot Agent Runner.

    Communicates with MaiBot's API-Server via WebSocket,
    using the maim_message envelope protocol.
    """

    # Class-level WS client cache: persists across runner instances
    _ws_clients: T.ClassVar[dict[str, MaiBotWSClient]] = {}

    @override
    async def reset(
        self,
        request: ProviderRequest,
        run_context: ContextWrapper[TContext],
        agent_hooks: BaseAgentRunHooks[TContext],
        provider_config: dict,
        **kwargs: T.Any,
    ) -> None:
        self.req = request
        self.streaming = kwargs.get("streaming", False)
        self.final_llm_resp = None
        self._state = AgentState.IDLE
        self.agent_hooks = agent_hooks
        self.run_context = run_context

        # Extract MaiBot-specific config
        self.ws_url = provider_config.get("maibot_ws_url", DEFAULT_WS_URL)
        self.api_key = provider_config.get("maibot_api_key", "")
        self.platform = provider_config.get("maibot_platform", DEFAULT_PLATFORM)
        self.timeout = provider_config.get("timeout", DEFAULT_TIMEOUT)

        if isinstance(self.timeout, str):
            self.timeout = int(self.timeout)

        if not self.api_key:
            raise ValueError(
                "MaiBot API Key 不能为空。请在 AstrBot 配置中填写 MaiBot 的 API Key。"
            )

        if not self.ws_url:
            raise ValueError(
                "MaiBot WebSocket URL 不能为空。请在 AstrBot 配置中填写 MaiBot 的 WS 地址。"
            )

        # Create or reuse WS client (class-level singleton per config)
        client_key = f"{self.ws_url}|{self.api_key}|{self.platform}"
        if client_key in MaiBotAgentRunner._ws_clients:
            self.ws_client = MaiBotAgentRunner._ws_clients[client_key]
            logger.debug("[MaiBot] Reusing existing persistent WS client.")
        else:
            self.ws_client = MaiBotWSClient(
                ws_url=self.ws_url,
                api_key=self.api_key,
                platform=self.platform,
                timeout=self.timeout,
            )
            MaiBotAgentRunner._ws_clients[client_key] = self.ws_client
            logger.info("[MaiBot] Created new persistent WS client.")

        # Set up tool call handler so MaiBot can call AstrBot tools
        self.ws_client.set_tool_call_handler(self._handle_tool_call)

        # Sync tools to MaiBot after connection is established
        await self._sync_tools_to_maibot()

    @override
    async def step(self):
        """Execute one step: send message to MaiBot and get response."""
        if not self.req:
            raise ValueError("Request is not set. Please call reset() first.")

        if self._state == AgentState.IDLE:
            try:
                await self.agent_hooks.on_agent_begin(self.run_context)
            except Exception as e:
                logger.error(f"Error in on_agent_begin hook: {e}", exc_info=True)

        self._transition_state(AgentState.RUNNING)

        try:
            async for response in self._execute_maibot_request():
                yield response
        except Exception as e:
            error_msg = f"MaiBot 请求失败：{str(e)}"
            logger.error(error_msg)
            self._transition_state(AgentState.ERROR)
            self.final_llm_resp = LLMResponse(role="err", completion_text=error_msg)
            yield AgentResponse(
                type="err",
                data=AgentResponseData(chain=MessageChain().message(error_msg)),
            )

    @override
    async def step_until_done(
        self, max_step: int = 30
    ) -> T.AsyncGenerator[AgentResponse, None]:
        while not self.done():
            async for resp in self.step():
                yield resp

    async def _execute_maibot_request(self):
        """Core logic: send request to MaiBot and process the response."""
        prompt = self.req.prompt or ""
        session_id = self.req.session_id or "unknown"
        image_urls = self.req.image_urls or []

        if not prompt and not image_urls:
            logger.warning("[MaiBot] Empty prompt and no images, skipping request.")
            self._transition_state(AgentState.DONE)
            chain = MessageChain(chain=[Comp.Plain("")])
            self.final_llm_resp = LLMResponse(role="assistant", result_chain=chain)
            yield AgentResponse(
                type="llm_result",
                data=AgentResponseData(chain=chain),
            )
            return

        logger.info(
            f"[MaiBot] Sending to MaiBot: '{prompt[:100]}...' "
            f"(session={session_id}, images={len(image_urls)})"
        )

        # Send message and wait for response segments
        response_segments = await self.ws_client.send_and_receive(
            text=prompt,
            user_id=session_id,
            user_nickname=session_id,
            images=image_urls if image_urls else None,
        )

        logger.info(
            f"[MaiBot] Received {len(response_segments)} segment(s)"
        )

        # Each segment becomes a separate Comp.Plain in the chain.
        # AstrBot's respond stage sends each component individually
        # with natural delays, achieving the multi-message effect.
        chain_components = [Comp.Plain(seg) for seg in response_segments]
        chain = MessageChain(chain=chain_components)

        self.final_llm_resp = LLMResponse(role="assistant", result_chain=chain)
        self._transition_state(AgentState.DONE)

        try:
            await self.agent_hooks.on_agent_done(self.run_context, self.final_llm_resp)
        except Exception as e:
            logger.error(f"Error in on_agent_done hook: {e}", exc_info=True)

        yield AgentResponse(
            type="llm_result",
            data=AgentResponseData(chain=chain),
        )

    # ─── Tool Injection ──────────────────────────────────────────────

    async def _sync_tools_to_maibot(self) -> None:
        """Push AstrBot's available tools to MaiBot via the WS client."""
        try:
            await self.ws_client.ensure_connected()
        except Exception as e:
            logger.warning(f"[MaiBot] Cannot sync tools, connection failed: {e}")
            return

        tool_defs: list[dict] = []
        for func_tool in llm_tools.func_list:
            if not func_tool.active:
                continue
            tool_defs.append({
                "name": func_tool.name,
                "description": func_tool.description or "",
                "parameters": func_tool.parameters or {},
            })

        if tool_defs:
            await self.ws_client.sync_tools(tool_defs)
        else:
            logger.debug("[MaiBot] No active AstrBot tools to sync.")

    async def _handle_tool_call(self, tool_name: str, tool_args: dict) -> str:
        """Handle a tool_call from MaiBot by executing the AstrBot tool."""
        func_tool = llm_tools.get_func(tool_name)
        if not func_tool:
            return f"Tool '{tool_name}' not found in AstrBot."

        if not func_tool.handler:
            return f"Tool '{tool_name}' has no handler."

        try:
            logger.info(f"[MaiBot] Executing AstrBot tool: {tool_name}({tool_args})")
            # AstrBot plugin tool handlers have signature (self, event, **kwargs).
            # The handler is a functools.partial that already bound `self`, so
            # the next positional arg is `event`.
            #
            # We try to get the real event from the current run_context so that
            # tool handlers can access user info, message context, etc.
            import inspect
            sig = inspect.signature(func_tool.handler)
            params = list(sig.parameters.keys())
            if params and params[0] == "event":
                # Get the real AstrBot event from the current run context
                event = None
                if hasattr(self, "run_context") and self.run_context:
                    ctx = getattr(self.run_context, "context", None)
                    if ctx:
                        event = getattr(ctx, "event", None)
                result = await func_tool.handler(event, **tool_args)  # type: ignore
            else:
                result = await func_tool.handler(**tool_args)  # type: ignore
            if result is None:
                return "Tool executed successfully (no output)."
            return str(result)
        except Exception as e:
            logger.error(f"[MaiBot] Tool '{tool_name}' execution error: {e}", exc_info=True)
            return f"Tool execution error: {e}"

    async def close(self):
        """Clean up WebSocket resources."""
        await self.ws_client.close()

    @override
    def done(self) -> bool:
        """Check if the agent has completed."""
        return self._state in (AgentState.DONE, AgentState.ERROR)

    @override
    def get_final_llm_resp(self) -> LLMResponse | None:
        return self.final_llm_resp
