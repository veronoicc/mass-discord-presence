"""Constants for the Discord Rich Presence plugin."""

from __future__ import annotations

from typing import Final

# Discord API endpoints
DISCORD_API_BASE: Final[str] = "https://discord.com/api/v10"
DISCORD_AUTHORIZE_URL: Final[str] = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL: Final[str] = f"{DISCORD_API_BASE}/oauth2/token"
DISCORD_HEADLESS_SESSIONS_URL: Final[str] = f"{DISCORD_API_BASE}/users/@me/headless-sessions"
DISCORD_HEADLESS_SESSIONS_DELETE_URL: Final[str] = (
    f"{DISCORD_API_BASE}/users/@me/headless-sessions/delete"
)

# Discord OAuth2 scopes:
# sdk.social_layer_presence umbrella-grants activities.write and identify
DISCORD_OAUTH_SCOPES: Final[str] = "openid sdk.social_layer_presence identify"

# Configuration keys
CONF_CLIENT_ID: Final[str] = "client_id"
CONF_CLIENT_SECRET: Final[str] = "client_secret"
CONF_ACCESS_TOKEN: Final[str] = "access_token"
CONF_REFRESH_TOKEN: Final[str] = "refresh_token"
CONF_TOKEN_EXPIRES_AT: Final[str] = "token_expires_at"

CONF_PLAYER: Final[str] = "player"
CONF_KEEP_ALIVE_INTERVAL: Final[str] = "keep_alive_interval"
CONF_SHOW_TIMESTAMPS: Final[str] = "show_timestamps"
CONF_SHOW_ALBUM_ART: Final[str] = "show_album_art"
CONF_SMALL_IMAGE: Final[str] = "small_image"
CONF_SMALL_TEXT: Final[str] = "small_text"

# Default values
DEFAULT_KEEP_ALIVE_INTERVAL: Final[int] = 900  # 15 minutes (session expires at 20 minutes)
DEFAULT_SHOW_TIMESTAMPS: Final[bool] = True
DEFAULT_SHOW_ALBUM_ART: Final[bool] = True
DEFAULT_SMALL_IMAGE: Final[str] = "1554457458234364025"
DEFAULT_SMALL_TEXT: Final[str] = "Music Assistant"
# Discord Activity Types
# 0 = Game, 1 = Streaming, 2 = Listening, 3 = Watching, 4 = Custom, 5 = Competing
DISCORD_ACTIVITY_TYPE_LISTENING: Final[int] = 2
