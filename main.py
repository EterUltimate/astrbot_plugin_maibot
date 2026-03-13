import asyncio

from astrbot.api import logger, star
from astrbot.core import astrbot_config
from astrbot.core.agent.runners.registry import AgentRunnerEntry
from astrbot.core.provider.register import llm_tools

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

    @staticmethod
    async def _early_connect_maibot(ctx, prov_id: str) -> None:
        """Pre-create WS client and sync tools at startup."""
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
        )
        MaiBotAgentRunner._ws_clients[client_key] = ws_client

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
