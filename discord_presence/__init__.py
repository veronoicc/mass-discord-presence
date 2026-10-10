"""Discord Rich Presence plugin for Music Assistant."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, EventType, PlaybackState
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.models.plugin import PluginProvider

from .constants import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_KEEP_ALIVE_INTERVAL,
    CONF_PLAYER,
    CONF_REFRESH_TOKEN,
    CONF_SHOW_ALBUM_ART,
    CONF_SHOW_TIMESTAMPS,
    CONF_SMALL_IMAGE,
    CONF_SMALL_TEXT,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_KEEP_ALIVE_INTERVAL,
    DEFAULT_SHOW_ALBUM_ART,
    DEFAULT_SHOW_TIMESTAMPS,
    DEFAULT_SMALL_IMAGE,
    DEFAULT_SMALL_TEXT,
    DISCORD_ACTIVITY_TYPE_LISTENING,
    DISCORD_HEADLESS_SESSIONS_DELETE_URL,
    DISCORD_HEADLESS_SESSIONS_URL,
    DISCORD_TOKEN_URL,
)
if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: Final[set[Any]] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return DiscordPresenceProvider(mass, manifest, config, SUPPORTED_FEATURES)


class DiscordPresenceProvider(PluginProvider):
    """Plugin provider to update Discord Rich Presence via headless sessions."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[Any],
    ) -> None:
        """Initialize the Discord Presence plugin."""
        super().__init__(mass, manifest, config, supported_features)
        self._session_token: str | None = None
        self._keep_alive_task: asyncio.Task[None] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        self._last_activity_payload: dict[str, Any] | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return config entries to configure this provider."""
        player_options = [ConfigValueOption(title="Any / Active Player", value="")]
        if hasattr(self.mass.players, "iter_players"):
            players_list = list(self.mass.players.iter_players())
        elif hasattr(self.mass.players, "all_players"):
            players_list = self.mass.players.all_players()
        else:
            try:
                players_list = list(self.mass.players)
            except TypeError:
                players_list = []
        for player in players_list:
            player_options.append(ConfigValueOption(title=player.display_name, value=player.player_id))

        return (
            ConfigEntry(
                key=CONF_PLAYER,
                type=ConfigEntryType.STRING,
                label="Player",
                description="Select which player to track (or Any for automatic detection)",
                default_value="",
                options=player_options,
                required=False,
            ),
            ConfigEntry(
                key=CONF_KEEP_ALIVE_INTERVAL,
                type=ConfigEntryType.INTEGER,
                label="Keep-alive Interval (seconds)",
                description="Interval to renew the Discord headless session (sessions expire after 20 minutes)",
                default_value=DEFAULT_KEEP_ALIVE_INTERVAL,
                range=(300, 1140),
                required=False,
            ),
            ConfigEntry(
                key=CONF_SHOW_TIMESTAMPS,
                type=ConfigEntryType.BOOLEAN,
                label="Show Timestamps",
                description="Show elapsed and track duration in Discord status",
                default_value=DEFAULT_SHOW_TIMESTAMPS,
                required=False,
            ),
            ConfigEntry(
                key=CONF_SHOW_ALBUM_ART,
                type=ConfigEntryType.BOOLEAN,
                label="Show Album Art",
                description="Display cover artwork in Discord status when available",
                default_value=DEFAULT_SHOW_ALBUM_ART,
                required=False,
            ),
            ConfigEntry(
                key=CONF_SMALL_IMAGE,
                type=ConfigEntryType.STRING,
                label="Small Image Asset",
                description="Asset name or snowflake ID for the small icon badge in Discord status",
                default_value=DEFAULT_SMALL_IMAGE,
                required=False,
            ),
            ConfigEntry(
                key=CONF_SMALL_TEXT,
                type=ConfigEntryType.STRING,
                label="Small Image Tooltip",
                description="Tooltip text shown when hovering over the small icon badge",
                default_value=DEFAULT_SMALL_TEXT,
                required=False,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async setup of provider."""
        client_id = self.get_setup_value(CONF_CLIENT_ID)
        access_token = self.get_setup_value(CONF_ACCESS_TOKEN)
        refresh_token = self.get_setup_value(CONF_REFRESH_TOKEN)

        if not client_id or not access_token or not refresh_token:
            raise SetupFailedError("Missing Discord OAuth2 configuration. Please authenticate the plugin.")

    async def loaded_in_mass(self) -> None:
        """Call after provider is loaded in Music Assistant."""
        await super().loaded_in_mass()

        # Listen for player state and queue changes
        self._unsubs = [
            self.mass.subscribe(
                self._on_player_or_queue_event,
                (
                    EventType.PLAYER_UPDATED,
                    EventType.QUEUE_UPDATED,
                    EventType.QUEUE_TIME_UPDATED,
                ),
            )
        ]

        # Initial presence check in case music is already playing
        self.mass.create_task(self._update_presence())

    async def unload(self, is_dupe: bool = False) -> None:
        """Unload provider and clean up active sessions."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

        self._stop_keep_alive()
        await self._clear_presence()

        await super().unload(is_dupe)

    async def _on_player_or_queue_event(self, event: MassEvent) -> None:
        """Handle player or queue updates."""
        target_player = self.get_config_value(CONF_PLAYER)
        if target_player and event.object_id != target_player:
            return

        self.mass.create_task(self._update_presence())

    async def _get_access_token(self) -> str | None:
        """Get valid Discord OAuth access token, refreshing if necessary."""
        access_token = str(self.get_setup_value(CONF_ACCESS_TOKEN) or "")
        refresh_token = str(self.get_setup_value(CONF_REFRESH_TOKEN) or "")
        client_id = str(self.get_setup_value(CONF_CLIENT_ID) or "")
        client_secret = self.get_setup_value(CONF_CLIENT_SECRET)
        token_expires_at = float(self.get_setup_value(CONF_TOKEN_EXPIRES_AT) or 0)

        # If token expires in less than 5 minutes, refresh it
        if time.time() >= token_expires_at - 300:
            self.logger.debug("Discord access token expired or expiring soon, refreshing...")
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            }
            if client_secret:
                data["client_secret"] = str(client_secret)

            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            async with self.mass.http_session.post(
                DISCORD_TOKEN_URL, data=data, headers=headers
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    self.logger.error(
                        "Failed to refresh Discord access token (%s): %s",
                        response.status,
                        error_text,
                    )
                    return None

                token_res = await response.json()

            access_token = str(token_res["access_token"])
            new_refresh = token_res.get("refresh_token")
            expires_in = token_res.get("expires_in", 604800)
            token_expires_at = time.time() + float(expires_in)

            self._update_setup_data(CONF_ACCESS_TOKEN, access_token)
            if new_refresh:
                self._update_setup_data(CONF_REFRESH_TOKEN, str(new_refresh))
            self._update_setup_data(CONF_TOKEN_EXPIRES_AT, token_expires_at)
            self.logger.debug("Discord access token successfully refreshed")

        return access_token

    def _get_active_queue(self) -> PlayerQueue | None:
        """Find the relevant playing or active queue."""
        target_player_id = self.get_config_value(CONF_PLAYER)
        if target_player_id:
            queue = self.mass.player_queues.get(str(target_player_id))
            return queue

        # Search for first playing queue
        for queue in self.mass.player_queues.all():
            if queue.state == PlaybackState.PLAYING and queue.active:
                return queue

        # Search for first active queue
        for queue in self.mass.player_queues.all():
            if queue.active and queue.current_item:
                return queue

        return None

    async def _update_presence(self, force: bool = False) -> None:
        """Update Discord Rich Presence based on player state."""
        async with self._lock:
            queue = self._get_active_queue()

            # If not playing or no item, clear presence
            if not queue or queue.state != PlaybackState.PLAYING or not queue.current_item:
                if self._session_token is not None:
                    await self._clear_presence()
                return

            client_id = str(self.get_setup_value(CONF_CLIENT_ID) or "")
            current_item = queue.current_item

            # Extract Track Name
            track_name = current_item.name or "Music"

            # Extract Artist
            artist_name = ""
            if current_item.media_item and hasattr(current_item.media_item, "artists"):
                artists = [
                    a.name
                    for a in current_item.media_item.artists
                    if getattr(a, "name", None)
                ]
                artist_name = ", ".join(artists)

            # Fallback to stream details or stream title if available
            if not artist_name and current_item.streamdetails and current_item.streamdetails.stream_title:
                stream_title = current_item.streamdetails.stream_title
                if " - " in stream_title:
                    artist_name, track_name = stream_title.split(" - ", 1)
                else:
                    artist_name = stream_title

            if not artist_name:
                artist_name = "Unknown Artist"

            def _norm(s: str) -> str:
                return re.sub(r"[^a-z0-9]", "", s.lower())

            # Clean up track title:
            # Strip artist prefix if present in track_name (e.g. "One-T/Trinix - The Magic Key")
            if " - " in track_name:
                parts = track_name.split(" - ", 1)
                p_norm = _norm(parts[0])
                a_norm = _norm(artist_name)
                matches = bool(p_norm and a_norm and (p_norm in a_norm or a_norm in p_norm))
                if not matches and current_item.media_item and hasattr(current_item.media_item, "artists"):
                    for a in current_item.media_item.artists:
                        single_artist_norm = _norm(getattr(a, "name", "") or "")
                        if single_artist_norm and (single_artist_norm in p_norm or p_norm in single_artist_norm):
                            matches = True
                            break
                if matches:
                    track_name = parts[1].strip()
                elif parts[0].strip() and parts[1].strip():
                    # Channel/promoter in artist_name (e.g. "xKito", "Trap City"),
                    # while track_name has "RealArtist - RealTitle"
                    artist_name = parts[0].strip()
                    track_name = parts[1].strip()
            # Strip video tags (e.g. "(Official Video)", "[Official Music Video]")
            track_name = re.sub(
                r"\s*[\(\[](official\s*(video|music\s*video|audio|lyric\s*video)|video|audio|lyrics?|hd|4k)[\)\]]",
                "",
                track_name,
                flags=re.IGNORECASE,
            ).strip()

            # Extract Album
            album_name = ""
            if current_item.media_item and getattr(current_item.media_item, "album", None):
                album_obj = current_item.media_item.album
                album_name = getattr(album_obj, "name", "") or ""

            # Line mapping for Discord Rich Presence:
            # Header: "Listening to <TITLE>" (name)
            # Line 1: Track title (details)
            # If album exists and distinct from track title:
            #   Line 2: Album (state)
            #   Line 3: Artist (assets.large_text)
            # If no album or single (album == title):
            #   Line 2: Artist (state)
            #   Line 3: Omitted (no large_text)
            has_album = bool(
                album_name and _norm(album_name) not in (_norm(track_name), _norm(artist_name))
            )
            assets: dict[str, str] = {}
            if has_album:
                state_text = album_name
                assets["large_text"] = artist_name
            else:
                state_text = artist_name

            show_art = self.get_config_value(CONF_SHOW_ALBUM_ART, DEFAULT_SHOW_ALBUM_ART)
            if show_art:
                image = current_item.image or (
                    current_item.media_item.image if current_item.media_item else None
                )
                if image:
                    image_url = self.mass.metadata.get_image_url(image)
                    if image_url and image_url.startswith("http"):
                        assets["large_image"] = image_url

            # Small icon badge with tooltip
            small_image = self.get_config_value(CONF_SMALL_IMAGE, DEFAULT_SMALL_IMAGE)
            if small_image:
                assets["small_image"] = str(small_image)
                assets["small_text"] = str(
                    self.get_config_value(CONF_SMALL_TEXT, DEFAULT_SMALL_TEXT)
                )

            # Timestamps: Discord expects Unix timestamps in milliseconds
            timestamps: dict[str, int] = {}
            show_timestamps = self.get_config_value(CONF_SHOW_TIMESTAMPS, DEFAULT_SHOW_TIMESTAMPS)
            if show_timestamps:
                now_ms = int(time.time() * 1000)
                elapsed_ms = int(float(queue.corrected_elapsed_time) * 1000)
                start_ms = max(0, now_ms - elapsed_ms)
                timestamps["start"] = start_ms
                if current_item.duration:
                    timestamps["end"] = start_ms + int(float(current_item.duration) * 1000)

            activity: dict[str, Any] = {
                "type": DISCORD_ACTIVITY_TYPE_LISTENING,
                "name": track_name,
                "details": track_name,
                "state": state_text,
                "application_id": client_id,
                "platform": "desktop",
                "assets": assets,
            }
            if timestamps:
                activity["timestamps"] = timestamps
            # Deduplicate if activity hasn't changed and token is active
            if (
                not force
                and self._session_token
                and self._last_activity_payload == activity
            ):
                return

            access_token = await self._get_access_token()
            if not access_token:
                return

            payload: dict[str, Any] = {
                "activities": [activity],
            }
            if self._session_token:
                payload["token"] = self._session_token

            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }

            try:
                async with self.mass.http_session.post(
                    DISCORD_HEADLESS_SESSIONS_URL, json=payload, headers=headers
                ) as resp:
                    if resp.status not in (200, 201):
                        err_msg = await resp.text()
                        self.logger.warning(
                            "Failed to update Discord presence (%s): %s",
                            resp.status,
                            err_msg,
                        )
                        # If session token was rejected (e.g. expired on Discord side), reset it
                        if resp.status in (400, 404):
                            self._session_token = None
                        return

                    data = await resp.json()
                    new_token = data.get("token")
                    if new_token:
                        self._session_token = str(new_token)
                    self._last_activity_payload = activity
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Updated Discord presence: %s - %s",
                        artist_name,
                        track_name,
                    )

                self._start_keep_alive()

            except Exception as err:
                self.logger.warning("Error updating Discord presence: %s", err)

    async def _clear_presence(self) -> None:
        """Clear active headless session on Discord."""
        if not self._session_token:
            return

        token_to_delete = self._session_token
        self._session_token = None
        self._last_activity_payload = None
        self._stop_keep_alive()

        access_token = await self._get_access_token()
        if not access_token:
            return

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {"token": token_to_delete}

        try:
            async with self.mass.http_session.post(
                DISCORD_HEADLESS_SESSIONS_DELETE_URL, json=payload, headers=headers
            ) as resp:
                if resp.status not in (200, 204):
                    self.logger.debug(
                        "Discord headless session delete returned status %s", resp.status
                    )
                else:
                    self.logger.debug("Successfully cleared Discord presence")
        except Exception as err:
            self.logger.debug("Failed to delete Discord headless session: %s", err)

    def _start_keep_alive(self) -> None:
        """Ensure the keepalive loop task is running."""
        if self._keep_alive_task is None or self._keep_alive_task.done():
            self._keep_alive_task = self.mass.create_task(self._keep_alive_loop())

    def _stop_keep_alive(self) -> None:
        """Cancel the keepalive task."""
        if self._keep_alive_task and not self._keep_alive_task.done():
            self._keep_alive_task.cancel()
        self._keep_alive_task = None

    async def _keep_alive_loop(self) -> None:
        """Periodically renew headless session while music is playing."""
        interval = int(
            self.get_config_value(CONF_KEEP_ALIVE_INTERVAL, DEFAULT_KEEP_ALIVE_INTERVAL)
        )
        self.logger.debug("Discord presence keepalive loop started (interval: %ss)", interval)
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._session_token:
                    break
                self.logger.debug("Running Discord presence keepalive update")
                await self._update_presence(force=True)
        except asyncio.CancelledError:
            pass
