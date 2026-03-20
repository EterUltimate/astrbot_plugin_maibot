import asyncio

from astrbot.api import logger, star
from astrbot.core import astrbot_config
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.provider.register import (
    register_provider_adapter,
)
from astrbot.core.provider.entities import (
    LLMResponse,
    ProviderType,
    ToolCallsResult,
)
from astrbot.core.provider.provider import Provider
from astrbot.core.agent.runners.base import AgentState, BaseAgentRunner
from astrbot.core.agent.response import AgentResponse, AgentResponseData
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.star.register import register_regex
import astrbot.core.message.components as Comp
from astrbot.core.message.message_event_result import MessageChain

from .maibot_agent_runner import (
    DEFAULT_PLATFORM,
    DEFAULT_TIMEOUT,
    DEFAULT_WS_URL,
    MAIBOT_PROVIDER_TYPE,
    MaiBotAgentRunner,
)
from .maibot_ws_client import MaiBotWSClient


@register_provider_adapter(
    MAIBOT_PROVIDER_TYPE,
    "MaiBot - 通过 WebSocket 连接 MaiBot API-Server，替代 AstrBot 内置 LLM Agent",
    provider_type=ProviderType.CHAT_COMPLETION,
    default_config_tmpl={
        "type": MAIBOT_PROVIDER_TYPE,
        "enable": False,
        "id": "maibot_default",
        "maibot_ws_url": DEFAULT_WS_URL,
        "maibot_api_key": "",
        "maibot_platform": DEFAULT_PLATFORM,
        "maibot_bot_qq": "",
        "maibot_bot_nickname": "",
    },
    provider_display_name="MaiBot",
)
class MaiBotProvider(Provider):
    """AstrBot Provider that bridges to MaiBot via WebSocket.

    This is registered as a CHAT_COMPLETION provider so it appears in the
    WebUI provider list. When selected as the LLM provider, AstrBot's built-in
    ToolLoopAgentRunner will call text_chat() for each inference step, which
    we forward to MaiBot's API-Server.
    """

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)

        self.ws_url = provider_config.get("maibot_ws_url", DEFAULT_WS_URL)
        self.api_key = provider_config.get("maibot_api_key", "")
        self.platform = provider_config.get("maibot_platform", DEFAULT_PLATFORM)
        self.timeout = provider_config.get("timeout", DEFAULT_TIMEOUT)
        self.bot_user_id = provider_config.get("maibot_bot_qq", "")
        self.bot_nickname = provider_config.get("maibot_bot_nickname", "")

        if isinstance(self.timeout, str):
            self.timeout = int(self.timeout)

        self.ws_client: MaiBotWSClient | None = None
        self._initialized = False

    async def _ensure_client(self) -> MaiBotWSClient:
        """Lazily create or reuse the WebSocket client."""
        if self.ws_client is None:
            self.ws_client = MaiBotWSClient(
                ws_url=self.ws_url,
                api_key=self.api_key,
                platform=self.platform,
                timeout=self.timeout,
                bot_user_id=self.bot_user_id,
                bot_nickname=self.bot_nickname,
            )
        await self.ws_client.ensure_connected()
        return self.ws_client

    def get_current_key(self) -> str:
        return self.api_key

    def set_key(self, key: str) -> None:
        self.api_key = key

    async def get_models(self) -> list[str]:
        """Return a dummy model name since MaiBot handles model selection internally."""
        return ["maibot"]

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        func_tool=None,
        contexts=None,
        system_prompt: str | None = None,
        tool_calls_result=None,
        model: str | None = None,
        extra_user_content_parts=None,
        **kwargs,
    ) -> LLMResponse:
        """Forward the chat request to MaiBot via WebSocket.

        This is called by AstrBot's ToolLoopAgentRunner during each inference step.
        We convert the MaiBot response back into an LLMResponse.
        """
        if not self.api_key:
            return LLMResponse(
                role="assistant",
                completion_text="[MaiBot] 未配置 API Key，请在 AstrBot 配置中填写 MaiBot 的 API Key。",
            )

        try:
            client = await self._ensure_client()

            # Parse UMO for group/private context
            is_group = False
            ws_user_id = session_id or "unknown"
            ws_group_id = None
            if session_id:
                parts = session_id.split(":", 2)
                if len(parts) >= 2 and "Group" in parts[1]:
                    is_group = True
                    ws_group_id = session_id

            # Send and wait for MaiBot response
            response_payloads = await client.send_and_receive(
                text=prompt or "",
                user_id=ws_user_id if not is_group else (session_id.split(":")[-1] if session_id else ""),
                user_nickname="",
                group_id=ws_group_id,
                group_name="",
                images=image_urls if image_urls else None,
            )

            # Convert MaiBot response to message chain
            chain_components = []
            for payload in response_payloads:
                segment = payload.get("message_segment", {})
                chain_components.extend(
                    MaiBotAgentRunner._parse_segment_to_components(segment)
                )

            if not chain_components:
                for payload in response_payloads:
                    text = client._extract_text_from_payload(payload)
                    if text:
                        chain_components.append(Comp.Plain(text))

            chain = MessageChain(chain=chain_components)
            return LLMResponse(role="assistant", result_chain=chain)

        except asyncio.TimeoutError:
            return LLMResponse(
                role="err",
                completion_text=f"[MaiBot] 请求超时（{self.timeout}s）。",
            )
        except Exception as e:
            logger.error(f"[MaiBot] text_chat error: {e}", exc_info=True)
            return LLMResponse(
                role="err",
                completion_text=f"[MaiBot] 通信错误：{e}",
            )


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.context = context
        logger.info("[MaiBot Plugin] MaiBot provider registered.")

    @register_regex(".*")
    async def _passthrough_to_maibot(self, event: AstrMessageEvent):
        """把所有消息透传给麦麦（fire-and-forget，不产生回复）。

        This handler matches every message but returns None, so it doesn't
        produce any response or interfere with normal pipeline processing.
        """
        # Find any available MaiBot WS client from existing providers
        ws_client = self._get_any_maibot_client()
        if not ws_client:
            return  # MaiBot not configured

        if getattr(event, "is_at_or_wake_command", False):
            return

        umo = event.unified_msg_origin
        if not umo:
            return

        platform_name, message_type, session_id = MaiBotAgentRunner._parse_umo(umo)
        is_group = "Group" in message_type

        text = event.message_str or ""

        # Extract images from event message chain
        image_b64_list: list[str] = []
        if hasattr(event, "message_obj") and event.message_obj:
            for comp in event.message_obj:
                if isinstance(comp, Comp.Image):
                    try:
                        b64 = await comp.convert_to_base64()
                        if b64:
                            image_b64_list.append(b64)
                    except Exception:
                        pass

        if not text.strip() and not image_b64_list:
            return

        sender_id = event.get_sender_id() or session_id
        sender_name = event.get_sender_name() or sender_id

        if is_group:
            ws_user_id = sender_id
            ws_user_nickname = sender_name
            ws_group_id = umo
            ws_group_name = session_id
        else:
            ws_user_id = umo
            ws_user_nickname = sender_name or umo
            ws_group_id = None
            ws_group_name = ""

        asyncio.create_task(ws_client.send_only(
            text=text,
            user_id=ws_user_id,
            user_nickname=ws_user_nickname,
            group_id=ws_group_id,
            group_name=ws_group_name,
            images=image_b64_list or None,
        ))

    def _get_any_maibot_client(self) -> MaiBotWSClient | None:
        """Try to get an active MaiBot WS client from provider instances."""
        try:
            provider_manager = self.context.provider_manager
            for inst_id, inst_info in provider_manager._inst_map.items():
                prov = inst_info.get("inst")
                if prov and isinstance(prov, MaiBotProvider) and prov.ws_client:
                    return prov.ws_client
        except Exception:
            pass
        return None
