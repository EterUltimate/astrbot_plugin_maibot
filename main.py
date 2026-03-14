import asyncio

from astrbot.api import logger, star
from astrbot.core import astrbot_config
from astrbot.core.agent.runners.registry import AgentRunnerEntry
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.register import llm_tools
from astrbot.core.star.register import register_regex
import astrbot.core.message.components as Comp

from .maibot_agent_runner import (
    DEFAULT_PLATFORM,
    DEFAULT_TIMEOUT,
    DEFAULT_WS_URL,
    MAIBOT_AGENT_RUNNER_PROVIDER_ID_KEY,
    MAIBOT_PROVIDER_TYPE,
    MaiBotAgentRunner,
)
from .maibot_ws_client import MaiBotWSClient


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.context = context

        # Register the MaiBot agent runner with AstrBot
        context.register_agent_runner(
            AgentRunnerEntry(
                runner_type=MAIBOT_PROVIDER_TYPE,
                runner_cls=MaiBotAgentRunner,
                provider_id_key=MAIBOT_AGENT_RUNNER_PROVIDER_ID_KEY,
                display_name="MaiBot",
                on_initialize=self._early_connect_maibot,
                conversation_id_key=None,
                provider_config_fields={
                    "maibot_ws_url": {
                        "description": "MaiBot WebSocket URL",
                        "type": "string",
                        "hint": "MaiBot API-Server 的 WebSocket 地址。默认为 ws://127.0.0.1:18040/ws",
                    },
                    "maibot_api_key": {
                        "description": "MaiBot API Key",
                        "type": "string",
                        "hint": "MaiBot API-Server 允许的 API Key。需要在 MaiBot 配置的 api_server_allowed_api_keys 中添加此 Key。",
                    },
                    "maibot_platform": {
                        "description": "平台标识",
                        "type": "string",
                        "hint": "发送给 MaiBot 的平台标识名称。默认为 astrbot。",
                    },
                },
            )
        )

        logger.info("[MaiBot Plugin] MaiBot agent runner registered.")

    async def _early_connect_maibot(self, ctx, prov_id: str) -> None:
        """Pre-create WS client, sync tools, and register proactive handler."""
        prov_cfg: dict = next(
            (p for p in astrbot_config["provider"] if p["id"] == prov_id),
            {},
        )
        if not prov_cfg:
            logger.warning("[MaiBot] Early connect: provider config not found")
            return

        ws_url = prov_cfg.get("maibot_ws_url", DEFAULT_WS_URL)
        api_key = prov_cfg.get("maibot_api_key", "")
        platform = prov_cfg.get("maibot_platform", DEFAULT_PLATFORM)
        timeout = prov_cfg.get("timeout", DEFAULT_TIMEOUT)

        if not api_key:
            logger.warning(
                "[MaiBot] Early connect: API key not configured, skipping"
            )
            return

        client_key = f"{ws_url}|{api_key}|{platform}"
        if client_key in MaiBotAgentRunner._ws_clients:
            logger.debug("[MaiBot] Early connect: client already exists")
            return

        ws_client = MaiBotWSClient(
            ws_url=ws_url,
            api_key=api_key,
            platform=platform,
            timeout=int(timeout) if isinstance(timeout, str) else timeout,
            bot_user_id=prov_cfg.get("maibot_bot_qq", ""),
            bot_nickname=prov_cfg.get("maibot_bot_nickname", ""),
        )
        MaiBotAgentRunner._ws_clients[client_key] = ws_client

        # Register proactive message handler
        ws_client.set_proactive_message_handler(
            self._make_proactive_handler(ws_client)
        )

        # Connect and sync tools
        await ws_client.ensure_connected()

        # Collect and sync AstrBot tools
        tool_defs = []
        for func_tool in llm_tools.func_list:
            if not func_tool.active:
                continue
            tool_defs.append(
                {
                    "name": func_tool.name,
                    "description": func_tool.description or "",
                    "parameters": func_tool.parameters or {},
                }
            )

        if tool_defs:
            await ws_client.sync_tools(tool_defs)
            logger.info(f"[MaiBot] Early connect: synced {len(tool_defs)} tools")
        else:
            logger.info("[MaiBot] Early connect: no tools to sync")

    def _make_proactive_handler(self, ws_client: MaiBotWSClient):
        """Create a proactive message handler closure."""

        async def handler(msg: dict) -> None:
            await self._handle_proactive_message(msg, ws_client)

        return handler

    async def _handle_proactive_message(
        self, msg: dict, ws_client: MaiBotWSClient
    ) -> None:
        """Handle a proactive message from MaiBot and deliver it to the target platform.

        MaiBot's reply payload contains:
        - For group chat: group_info.group_id = original UMO (e.g. "qq:GroupMessage:123456")
        - For private chat: user_info.user_id = original UMO (e.g. "qq:FriendMessage:user789")
        """
        payload = msg.get("payload", {})

        # Extract components from the message segment
        chain_components = self._parse_segment_to_components(
            payload.get("message_segment", {})
        )
        if not chain_components:
            logger.debug("[MaiBot] 主动消息内容为空，忽略")
            return

        # Recover UMO from the reply payload
        # The message_info in MaiBot's reply has group_info from ChatStream
        msg_info = payload.get("message_info", {})

        # MaiBot reply messages: group_info comes from chat_stream
        # We stored the full UMO as group_id (group chat) or user_id (private chat)
        group_info = msg_info.get("group_info")
        umo = None

        if group_info and group_info.get("group_id"):
            # Group chat: group_id = UMO
            umo = group_info["group_id"]
        else:
            # Private chat: try user_info.user_id
            # In MaiBot reply, message_info.user_info is the bot's own info,
            # but we also check sender_info for the original user
            sender_info = msg_info.get("sender_info", {})
            user_info = sender_info.get("user_info", {})
            if not user_info:
                user_info = msg_info.get("user_info", {})
            uid = user_info.get("user_id", "")
            if uid and ":" in uid:
                umo = uid

        if not umo:
            # Build a preview from the chain for logging
            preview = ""
            for c in chain_components:
                if isinstance(c, Comp.Plain):
                    preview += c.text[:100]
                    break
            logger.warning(
                f"[MaiBot] 无法从主动消息中恢复 UMO，丢弃消息: {preview}"
            )
            return

        # Build a short text preview for logging
        text_preview = ""
        for c in chain_components:
            if isinstance(c, Comp.Plain):
                text_preview = c.text[:80]
                break
        if not text_preview:
            text_preview = f"[{len(chain_components)} segments]"

        logger.info(f"[MaiBot] 主动消息投递: UMO={umo}, 内容={text_preview}")

        try:
            session = MessageSession.from_str(umo)
        except Exception as e:
            logger.error(f"[MaiBot] 解析 UMO 失败: {umo}, 错误: {e}")
            return

        # Access platform manager through the core lifecycle
        from astrbot.core.core_lifecycle import core_lifecycle

        platform_manager = core_lifecycle.platform_manager
        platform_inst = next(
            (
                inst
                for inst in platform_manager.platform_insts
                if inst.meta().id == session.platform_name
            ),
            None,
        )

        if not platform_inst:
            logger.warning(
                f"[MaiBot] 未找到目标平台: {session.platform_name}，"
                f"可用平台: {[inst.meta().id for inst in platform_manager.platform_insts]}"
            )
            return

        # Build MessageChain and send
        chain = MessageChain(chain=chain_components)
        try:
            await platform_inst.send_by_session(session, chain)
            logger.info(
                f"[MaiBot] 主动消息已投递到 {session.platform_name}: {text_preview}"
            )
        except Exception as e:
            logger.error(
                f"[MaiBot] 投递主动消息失败: {e}", exc_info=True
            )

    # ─── Segment Parsing ─────────────────────────────────────────────

    def _parse_segment_to_components(
        self, segment: dict
    ) -> list:
        """Convert MaiBot Seg to AstrBot message components.

        Handles: text, image (base64), emoji (base64), seglist (recursive).
        """
        result = []
        seg_type = segment.get("type", "")
        data = segment.get("data")

        if seg_type == "text" and isinstance(data, str) and data.strip():
            result.append(Comp.Plain(data))
        elif seg_type in ("image", "emoji") and isinstance(data, str) and data:
            # MaiBot sends raw base64 (no data URI prefix)
            # Strip data URI prefix if present
            b64 = data
            for prefix in ("data:image/png;base64,", "data:image/gif;base64,",
                           "data:image/jpeg;base64,", "data:image/webp;base64,"):
                if b64.startswith(prefix):
                    b64 = b64[len(prefix):]
                    break
            result.append(Comp.Image.fromBase64(b64))
        elif seg_type == "imageurl" and isinstance(data, str) and data:
            result.append(Comp.Image.fromURL(data))
        elif seg_type == "seglist" and isinstance(data, list):
            for sub_seg in data:
                if isinstance(sub_seg, dict):
                    result.extend(self._parse_segment_to_components(sub_seg))
        # voice/video/other types are not supported yet, silently skip

        return result

    # ─── Message Passthrough ─────────────────────────────────────────

    def _get_ws_client(self) -> MaiBotWSClient | None:
        """Get the first available MaiBot WS client."""
        clients = MaiBotAgentRunner._ws_clients
        if clients:
            return next(iter(clients.values()))
        return None

    @register_regex(".*")
    async def _passthrough_to_maibot(self, event: AstrMessageEvent):
        """把所有消息透传给麦麦（fire-and-forget，不产生回复）。

        This handler matches every message but returns None, so it doesn't
        produce any response or interfere with normal pipeline processing.
        Messages that also trigger the agent pipeline will be deduplicated
        in MaiBotAgentRunner (it skips send and only waits for response).
        """
        ws_client = self._get_ws_client()
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
