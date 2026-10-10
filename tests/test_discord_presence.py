"""Unit tests for the Discord Rich Presence plugin."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import EventType, PlaybackState
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import Artist, ItemMapping, MediaItemImage, Track
from music_assistant_models.player import Player
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.provider import ProviderManifest
from music_assistant_models.queue_item import QueueItem

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.discord_presence import DiscordPresenceProvider, setup
from music_assistant.providers.discord_presence.constants import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_KEEP_ALIVE_INTERVAL,
    CONF_PLAYER,
    CONF_REFRESH_TOKEN,
    CONF_SHOW_ALBUM_ART,
    CONF_SHOW_TIMESTAMPS,
    CONF_TOKEN_EXPIRES_AT,
    DISCORD_ACTIVITY_TYPE_LISTENING,
    DISCORD_HEADLESS_SESSIONS_DELETE_URL,
    DISCORD_HEADLESS_SESSIONS_URL,
    DISCORD_OAUTH_SCOPES,
    DISCORD_TOKEN_URL,
)
from music_assistant.providers.discord_presence.setup_flow import run_setup

TEST_CLIENT_ID = "123456789012345678"
TEST_CLIENT_SECRET = "super_secret_client_key"
TEST_ACCESS_TOKEN = "test_discord_access_token"
TEST_REFRESH_TOKEN = "test_discord_refresh_token"


@pytest.fixture
def mock_manifest() -> ProviderManifest:
    """Return a mock ProviderManifest for discord_presence."""
    return ProviderManifest(
        type="plugin",
        domain="discord_presence",
        name="Discord Rich Presence",
        description="Set Discord Rich Presence using headless sessions for Music Assistant playback",
        codeowners=["@music-assistant"],
    )


@pytest.fixture
def mock_mass() -> MagicMock:
    """Return a mock MusicAssistant server instance."""
    mass = MagicMock()
    mass.loop = MagicMock()
    mass.config.encrypt_string = lambda x: x
    mass.config.decrypt_string = lambda x: x
    mass.config.get = MagicMock(return_value={"providers": {"discord_presence--test": {}}})
    mass.config.set = MagicMock()

    # Track tasks created by create_task
    def _create_task(coro: Any, *args: Any, **kwargs: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)

    mass.create_task = MagicMock(side_effect=_create_task)
    mass.metadata.get_image_url = MagicMock(return_value="https://cdn.example.com/cover.jpg")
    mass.players.iter_players = MagicMock(return_value=[])
    mass.player_queues.all = MagicMock(return_value=[])
    mass.subscribe = MagicMock(return_value=lambda: None)
    return mass


def _make_provider(
    mass: MagicMock,
    manifest: ProviderManifest,
    setup_data: dict[str, Any] | None = None,
    config_values: dict[str, Any] | None = None,
) -> DiscordPresenceProvider:
    """Create a DiscordPresenceProvider with pre-populated config."""
    stored_setup = dict(setup_data or {})

    def _config_get(key: str, default: Any = None) -> Any:
        if "setup_data" in key:
            return stored_setup
        return {"providers": {"discord_presence--test": {}}}

    def _config_set(key: str, value: Any, immediate: bool = True) -> None:
        if "/setup_data/" in key:
            sub_key = key.split("/setup_data/")[-1]
            stored_setup[sub_key] = value

    mass.config.get = MagicMock(side_effect=_config_get)
    mass.config.set = MagicMock(side_effect=_config_set)

    prov_config = ProviderConfig(
        values=config_values or {},
        type="plugin",
        domain="discord_presence",
        instance_id="discord_presence--test",
        name="Discord Rich Presence",
        setup_data=stored_setup,
    )
    return DiscordPresenceProvider(mass, manifest, prov_config, set())


# ==============================================================================
# Manifest Tests
# ==============================================================================


def test_manifest_file() -> None:
    """Test loading and contents of manifest.json."""
    with open("music_assistant/providers/discord_presence/manifest.json", "r") as f:
        data = json.load(f)
    assert data["type"] == "plugin"
    assert data["domain"] == "discord_presence"
    assert "pkce>=1.0.3" in data["requirements"]


# ==============================================================================
# Setup Flow Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_setup_flow_successful(mock_mass: MagicMock) -> None:
    """Test the complete OAuth2 setup flow with PKCE and token exchange."""
    finish_mock = AsyncMock()
    context = SetupFlowContext(kind="setup", reason="user", domain="discord_presence")
    session = SetupSession(mock_mass, "flow_test", context, finish_mock)

    # Step 1: User fills credentials form
    session.form = AsyncMock(
        return_value={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_CLIENT_SECRET: TEST_CLIENT_SECRET,
        }
    )

    # Step 2: External browser OAuth step
    session.external = AsyncMock(
        return_value={
            "code": "test_auth_code_123",
            "state": "test_state",
        }
    )

    # Mock HTTP exchange with Discord token endpoint
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "access_token": TEST_ACCESS_TOKEN,
            "refresh_token": TEST_REFRESH_TOKEN,
            "expires_in": 604800,
        }
    )
    mock_mass.http_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    await run_setup(session)

    # Verify external auth URL was constructed properly
    assert session.external.called
    auth_url = session.external.call_args[0][0]
    assert "https://discord.com/oauth2/authorize" in auth_url
    assert f"client_id={TEST_CLIENT_ID}" in auth_url
    assert "code_challenge=" in auth_url
    assert "code_challenge_method=S256" in auth_url

    # Verify finish was called with tokens
    assert finish_mock.called
    saved_data = finish_mock.call_args[0][1]
    assert saved_data[CONF_CLIENT_ID] == TEST_CLIENT_ID
    assert saved_data[CONF_CLIENT_SECRET] == TEST_CLIENT_SECRET
    assert saved_data[CONF_ACCESS_TOKEN] == TEST_ACCESS_TOKEN
    assert saved_data[CONF_REFRESH_TOKEN] == TEST_REFRESH_TOKEN
    assert saved_data[CONF_TOKEN_EXPIRES_AT] > time.time()


# ==============================================================================
# Provider Lifecycle & Token Refresh Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_provider_init_validation(
    mock_mass: MagicMock, mock_manifest: ProviderManifest
) -> None:
    """Test handle_async_init checks for required credentials."""
    # Missing credentials should raise SetupFailedError
    prov_no_creds = _make_provider(mock_mass, mock_manifest, setup_data={})
    with pytest.raises(SetupFailedError):
        await prov_no_creds.handle_async_init()

    # With credentials, handle_async_init succeeds
    prov_valid = _make_provider(
        mock_mass,
        mock_manifest,
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
            CONF_TOKEN_EXPIRES_AT: time.time() + 3600,
        },
    )
    await prov_valid.handle_async_init()


@pytest.mark.asyncio
async def test_get_access_token_refresh(
    mock_mass: MagicMock, mock_manifest: ProviderManifest
) -> None:
    """Test _get_access_token refreshes an expired or expiring token."""
    prov = _make_provider(
        mock_mass,
        mock_manifest,
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_CLIENT_SECRET: TEST_CLIENT_SECRET,
            CONF_ACCESS_TOKEN: "old_access_token",
            CONF_REFRESH_TOKEN: "old_refresh_token",
            CONF_TOKEN_EXPIRES_AT: time.time() - 10,  # Expired
        },
    )

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        }
    )
    mock_mass.http_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    token = await prov._get_access_token()
    assert token == "new_access_token"
    assert prov.get_setup_value(CONF_ACCESS_TOKEN) == "new_access_token"
    assert prov.get_setup_value(CONF_REFRESH_TOKEN) == "new_refresh_token"


# ==============================================================================
# Presence Headless Sessions Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_update_presence_playing_sets_headless_session(
    mock_mass: MagicMock, mock_manifest: ProviderManifest
) -> None:
    """Test setting presence when player starts playing a track."""
    prov = _make_provider(
        mock_mass,
        mock_manifest,
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
            CONF_TOKEN_EXPIRES_AT: time.time() + 3600,
        },
    )

    # Create mock queue with playing track
    queue = MagicMock(spec=PlayerQueue)
    queue.queue_id = "player_1"
    queue.state = PlaybackState.PLAYING
    queue.active = True
    queue.corrected_elapsed_time = 15.0

    track = Track(
        item_id="track_1",
        provider="spotify",
        name="Bohemian Rhapsody",
        artists=[
            Artist(
                item_id="art_1",
                provider="spotify",
                name="Queen",
                provider_mappings=set(),
            )
        ],
        provider_mappings=set(),
        duration=354,
    )
    queue_item = QueueItem(
        queue_id="player_1",
        queue_item_id="qi_1",
        name="Bohemian Rhapsody",
        duration=354,
        media_item=track,
        image=MediaItemImage(type="thumb", path="https://example.com/queen.jpg", provider="spotify", remotely_accessible=True),
    )
    queue.current_item = queue_item
    mock_mass.player_queues.all = MagicMock(return_value=[queue])

    # Mock Discord headless-sessions response
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"token": "h:session_token_xyz"})
    mock_mass.http_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    await prov._update_presence()

    # Verify headless session POST was sent
    assert mock_mass.http_session.post.called
    call_args = mock_mass.http_session.post.call_args
    assert call_args[0][0] == DISCORD_HEADLESS_SESSIONS_URL

    payload = call_args[1]["json"]
    assert len(payload["activities"]) == 1
    activity = payload["activities"][0]
    assert activity["type"] == DISCORD_ACTIVITY_TYPE_LISTENING
    assert activity["name"] == "Bohemian Rhapsody"
    assert activity["details"] == "Bohemian Rhapsody"
    assert activity["state"] == "Queen"
    assert activity["application_id"] == TEST_CLIENT_ID
    assert activity["platform"] == "desktop"
    assert "assets" in activity
    assert "large_image" in activity["assets"]
    assert activity["assets"]["small_image"] == "1554457458234364025"
    assert activity["assets"]["small_text"] == "Music Assistant"
    assert activity["timestamps"]["start"] > 1000000000000

    # Verify session token was stored
    assert prov._session_token == "h:session_token_xyz"


@pytest.mark.asyncio
async def test_update_presence_pause_clears_session(
    mock_mass: MagicMock, mock_manifest: ProviderManifest
) -> None:
    """Test pausing playback sends headless-sessions delete request."""
    prov = _make_provider(
        mock_mass,
        mock_manifest,
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
            CONF_TOKEN_EXPIRES_AT: time.time() + 3600,
        },
    )
    # Simulate existing active session
    prov._session_token = "h:existing_session_token"

    # Queue is now paused
    queue = MagicMock(spec=PlayerQueue)
    queue.queue_id = "player_1"
    queue.state = PlaybackState.PAUSED
    queue.active = True
    queue.current_item = None
    mock_mass.player_queues.all = MagicMock(return_value=[queue])

    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_mass.http_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    await prov._update_presence()

    # Verify headless sessions delete request
    assert mock_mass.http_session.post.called
    call_args = mock_mass.http_session.post.call_args
    assert call_args[0][0] == DISCORD_HEADLESS_SESSIONS_DELETE_URL
    assert call_args[1]["json"] == {"token": "h:existing_session_token"}

    # Verify session token was cleared
    assert prov._session_token is None


@pytest.mark.asyncio
async def test_keepalive_loop_and_unload(
    mock_mass: MagicMock, mock_manifest: ProviderManifest
) -> None:
    """Test keep-alive task starts on play and unload cleans up cleanly."""
    prov = _make_provider(
        mock_mass,
        mock_manifest,
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
            CONF_TOKEN_EXPIRES_AT: time.time() + 3600,
        },
    )
    prov._session_token = "h:token_to_unload"
    prov._start_keep_alive()
    assert prov._keep_alive_task is not None
    assert not prov._keep_alive_task.done()

    # Unload provider
    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_mass.http_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    await prov.unload()

    assert prov._keep_alive_task is None
    assert prov._session_token is None


@pytest.mark.asyncio
async def test_setup_factory(mock_mass: MagicMock, mock_manifest: ProviderManifest) -> None:
    """Test provider setup factory function."""
    prov_config = ProviderConfig(
        values={},
        type="plugin",
        domain="discord_presence",
        instance_id="discord_presence--test",
        name="Discord Rich Presence",
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
        },
    )
    instance = await setup(mock_mass, mock_manifest, prov_config)
    assert isinstance(instance, DiscordPresenceProvider)


@pytest.mark.asyncio
async def test_get_config_entries(mock_mass: MagicMock, mock_manifest: ProviderManifest) -> None:
    """Test get_config_entries includes configured players and default settings."""
    player_mock = MagicMock()
    player_mock.display_name = "Living Room"
    player_mock.player_id = "player_lr_1"
    mock_mass.players.iter_players = MagicMock(return_value=[player_mock])

    prov = _make_provider(mock_mass, mock_manifest)
    entries = await prov.get_config_entries()
    entry_keys = [e.key for e in entries]
    assert CONF_PLAYER in entry_keys
    assert CONF_KEEP_ALIVE_INTERVAL in entry_keys
    assert CONF_SHOW_TIMESTAMPS in entry_keys
    assert CONF_SHOW_ALBUM_ART in entry_keys

    # Check player options
    player_entry = next(e for e in entries if e.key == CONF_PLAYER)
    assert any(opt.value == "player_lr_1" for opt in player_entry.options)


@pytest.mark.asyncio
async def test_stream_title_fallback(mock_mass: MagicMock, mock_manifest: ProviderManifest) -> None:
    """Test parsing radio stream titles when track artist is not structured."""
    prov = _make_provider(
        mock_mass,
        mock_manifest,
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
            CONF_TOKEN_EXPIRES_AT: time.time() + 3600,
        },
    )

    queue = MagicMock(spec=PlayerQueue)
    queue.queue_id = "player_radio"
    queue.state = PlaybackState.PLAYING
    queue.active = True
    queue.corrected_elapsed_time = 0

    streamdetails = MagicMock()
    streamdetails.stream_title = "Daft Punk - One More Time"
    queue_item = QueueItem(
        queue_id="player_radio",
        queue_item_id="qi_radio",
        name="Radio Stream",
        duration=0,
        streamdetails=streamdetails,
    )
    queue.current_item = queue_item
    mock_mass.player_queues.all = MagicMock(return_value=[queue])

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"token": "h:stream_token"})
    mock_mass.http_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    await prov._update_presence()

    call_args = mock_mass.http_session.post.call_args
    activity = call_args[1]["json"]["activities"][0]
    assert activity["name"] == "One More Time"
    assert activity["details"] == "One More Time"
    assert activity["state"] == "Daft Punk"


@pytest.mark.asyncio
async def test_session_rejected_resets_token(mock_mass: MagicMock, mock_manifest: ProviderManifest) -> None:
    """Test that a 400/404 from Discord headless sessions resets the stored session token."""
    prov = _make_provider(
        mock_mass,
        mock_manifest,
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
            CONF_TOKEN_EXPIRES_AT: time.time() + 3600,
        },
    )
    prov._session_token = "h:expired_token"

    queue = MagicMock(spec=PlayerQueue)
    queue.queue_id = "player_1"
    queue.state = PlaybackState.PLAYING
    queue.active = True
    queue.current_item = QueueItem(
        queue_id="player_1",
        queue_item_id="qi_1",
        name="Track",
        duration=180,
    )
    mock_mass.player_queues.all = MagicMock(return_value=[queue])

    mock_resp = AsyncMock()
    mock_resp.status = 404
    mock_resp.text = AsyncMock(return_value="Unknown session")
    mock_mass.http_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    await prov._update_presence()
    assert prov._session_token is None


@pytest.mark.asyncio
async def test_track_title_cleaning_and_single_album(
    mock_mass: MagicMock, mock_manifest: ProviderManifest
) -> None:
    """Test that artist prefix is stripped and single album does not duplicate line."""
    prov = _make_provider(
        mock_mass,
        mock_manifest,
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
            CONF_TOKEN_EXPIRES_AT: time.time() + 3600,
        },
    )

    queue = MagicMock(spec=PlayerQueue)
    queue.queue_id = "player_1"
    queue.state = PlaybackState.PLAYING
    queue.active = True
    queue.corrected_elapsed_time = 34.0

    track = Track(
        item_id="track_onet",
        provider="ytmusic_free",
        name="One-T/Trinix - The Magic Key",
        artists=[
            Artist(item_id="art_1", provider="ytmusic_free", name="One-T", provider_mappings=set()),
            Artist(item_id="art_2", provider="ytmusic_free", name="Trinix", provider_mappings=set()),
        ],
        provider_mappings=set(),
        duration=171,
    )
    queue.current_item = QueueItem(
        queue_id="player_1",
        queue_item_id="qi_onet",
        name="One-T/Trinix - The Magic Key",
        duration=171,
        media_item=track,
    )
    mock_mass.player_queues.all = MagicMock(return_value=[queue])

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"token": "h:onet_token"})
    mock_mass.http_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    await prov._update_presence()

    call_args = mock_mass.http_session.post.call_args
    activity = call_args[1]["json"]["activities"][0]
    assert activity["name"] == "The Magic Key"
    assert activity["details"] == "The Magic Key"
    assert activity["state"] == "One-T, Trinix"
    assert "large_text" not in activity["assets"]
    assert activity["assets"]["small_image"] == "1554457458234364025"


@pytest.mark.asyncio
async def test_channel_artist_extraction(
    mock_mass: MagicMock, mock_manifest: ProviderManifest
) -> None:
    """Test that curator channel in artist is replaced by real artist from title."""
    prov = _make_provider(
        mock_mass,
        mock_manifest,
        setup_data={
            CONF_CLIENT_ID: TEST_CLIENT_ID,
            CONF_ACCESS_TOKEN: TEST_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
            CONF_TOKEN_EXPIRES_AT: time.time() + 3600,
        },
    )

    queue = MagicMock(spec=PlayerQueue)
    queue.queue_id = "player_1"
    queue.state = PlaybackState.PLAYING
    queue.active = True
    queue.corrected_elapsed_time = 28.0

    track = Track(
        item_id="track_bb",
        provider="ytmusic_free",
        name="Blackbear - 4U (Renzyx Remix)",
        artists=[
            Artist(item_id="art_xkito", provider="ytmusic_free", name="xKito", provider_mappings=set()),
        ],
        provider_mappings=set(),
        duration=190,
    )
    queue.current_item = QueueItem(
        queue_id="player_1",
        queue_item_id="qi_bb",
        name="Blackbear - 4U (Renzyx Remix)",
        duration=190,
        media_item=track,
    )
    mock_mass.player_queues.all = MagicMock(return_value=[queue])

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"token": "h:bb_token"})
    mock_mass.http_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    await prov._update_presence()

    call_args = mock_mass.http_session.post.call_args
    activity = call_args[1]["json"]["activities"][0]
    assert activity["name"] == "4U (Renzyx Remix)"
    assert activity["details"] == "4U (Renzyx Remix)"
    assert activity["state"] == "Blackbear"
    assert "large_text" not in activity["assets"]
