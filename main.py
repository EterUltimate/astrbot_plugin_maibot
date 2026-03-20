import asyncio

from astrbot.api import logger, star
from astrbot.core import astrbot_config
from astrbot.core.agent.runners.base import BaseAgentRunner, AgentState
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.register import llm_tools, register_provider_adapter
from astrbot.core.provider.entities import ProviderType
from astrbot.core.star.register import register_regex
import astrbot.core.message.components as Comp

from .maibot_agent_runner import (
    DEFAULT_PLATFORM,
    DEFAULT_TIMEOUT,
    DEFAULT_WS_URL,
    MAIBOT_PROVIDER_TYPE,
    MaiBotAgentRunner,
)
from .maibot_ws_client import MaiBotWSClient


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.context = context

        # Register the MaiBot Provider with AstrBot (v4+ compatible)
        # In the new architecture, Agent Runner is a built-in tool_loop_agent_runner
        # that wraps a Provider. We register MaiBot as a Provider.
        _register_maibot_provider()

        logger.info("[MaiBot Plugin] MaiBot provider registered.")

    @register_regex(".*")
    async def _passthrough_to_maibot(self, event: AstrMessageEvent):
        """把所有消息透传给麦麦（fire-and-forget，不产生回复）。

        This handler matches every message but returns None, so it doesn't
        produce any response or interfere with normal pipeline processing.
        Messages that also trigger the agent pipeline will be deduplicated
        in MaiBotAgentRunner (it skips send and only waits for response).
        """
        ws_client = MaiBotAgentRunner.get_ws_client()
        if not ws_client:
            return  # MaiBot not configured

        # Skip messages that will be handled by the agent runner
        # (via send_and_receive), to avoid sending to MaiBot twice
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
                        pass  # Skip unreadable images

        if not text.strip() and not image_b64_list:
            return  # Skip empty messages (no text, no images)

        sender_id = event.get_sender_id() or session_id
        sender_name = event.get_sender_name() or sender_id

        if is_group:
            ws_user_id = sender_id
            ws_user_nickname = sender_name
            ws_group_id = umo  # Full UMO as group_id
            ws_group_name = session_id
        else:
            ws_user_id = umo  # Full UMO as user_id
            ws_user_nickname = sender_name or umo
            ws_group_id = None
            ws_group_name = ""

        # Fire-and-forget: send to MaiBot without waiting for response
        asyncio.create_task(ws_client.send_only(
            text=text,
            user_id=ws_user_id,
            user_nickname=ws_user_nickname,
            group_id=ws_group_id,
            group_name=ws_group_name,
            images=image_b64_list or None,
        ))
        # Return None: no response, doesn't stop event propagation


def _register_maibot_provider():
    """Register MaiBot as a Provider in the new AstrBot v4+ architecture.

    In AstrBot v4+, the agent runner system was replaced:
    - Old: AgentRunnerEntry + context.register_agent_runner()
    - New: register_provider_adapter() + Agent (via @register_agent decorator)

    MaiBot acts as a Provider that communicates via WebSocket,
    and its responses are routed through AstrBot's built-in tool_loop_agent_runner.
    """
    register_provider_adapter(
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
