"""Setup flow for the Discord Rich Presence plugin."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pkce
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.oauth import (
    OAUTH_STEP_TIMEOUT,
    authorization_code_from_params,
    hosted_bounce_redirect,
)
from music_assistant.models.setup_flow import SetupFlowError

from .constants import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DISCORD_AUTHORIZE_URL,
    DISCORD_OAUTH_SCOPES,
    DISCORD_TOKEN_URL,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

HELP_LINK_URL = "https://discord.com/developers/applications"
LOGGER = logging.getLogger(__name__)

async def run_setup(session: SetupSession) -> None:
    """Run the Discord Rich Presence OAuth setup flow."""
    setup_data = dict(session.context.setup_data)
    client_id_default = str(setup_data.get(CONF_CLIENT_ID) or "")
    client_secret_default = str(setup_data.get(CONF_CLIENT_SECRET) or "")
    errors: dict[str, str | SetupFlowError] | None = None

    while True:
        submitted = await session.form(
            [
                ConfigEntry(
                    key="intro",
                    type=ConfigEntryType.LABEL,
                    help_link=HELP_LINK_URL,
                ),
                ConfigEntry(
                    key=CONF_CLIENT_ID,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=client_id_default,
                ),
                ConfigEntry(
                    key=CONF_CLIENT_SECRET,
                    type=ConfigEntryType.SECURE_STRING,
                    required=False,
                    default_value=client_secret_default,
                ),
            ],
            step_id="credentials",
            errors=errors,
        )
        client_id = str(submitted.get(CONF_CLIENT_ID) or "").strip()
        client_secret = str(submitted.get(CONF_CLIENT_SECRET) or "").strip()

        if not client_id:
            errors = {"base": "missing_client_id"}
            continue

        try:
            tokens = await _authenticate_discord(
                session=session,
                client_id=client_id,
                client_secret=client_secret if client_secret else None,
            )
            setup_data[CONF_CLIENT_ID] = client_id
            if client_secret:
                setup_data[CONF_CLIENT_SECRET] = client_secret
            elif CONF_CLIENT_SECRET in setup_data:
                del setup_data[CONF_CLIENT_SECRET]

            setup_data[CONF_ACCESS_TOKEN] = tokens["access_token"]
            setup_data[CONF_REFRESH_TOKEN] = tokens["refresh_token"]
            setup_data[CONF_TOKEN_EXPIRES_AT] = tokens["token_expires_at"]

            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            LOGGER.error("Discord setup flow error: %s", err)
            errors = {"base": str(err)}


async def _authenticate_discord(
    session: SetupSession,
    client_id: str,
    client_secret: str | None = None,
) -> dict[str, str | float]:
    """Run PKCE OAuth flow with Discord and exchange authorization code for tokens."""
    code_verifier, code_challenge = pkce.generate_pkce_pair()
    redirect_uri, state = hosted_bounce_redirect(session.callback_url)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": DISCORD_OAUTH_SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "state": state,
        "prompt": "consent",
    }
    authorize_url = f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"
    callback_params = await session.external(
        authorize_url, step_id="authenticate", expires_in=OAUTH_STEP_TIMEOUT
    )
    code = authorization_code_from_params(callback_params)

    token_payload: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        token_payload["client_secret"] = client_secret

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with session.mass.http_session.post(
        DISCORD_TOKEN_URL, data=token_payload, headers=headers
    ) as response:
        if response.status != 200:
            res_text = await response.text()
            raise SetupFlowError(
                f"Failed to exchange authorization code ({response.status}): {res_text}"
            )
        token_result = await response.json()

    access_token = token_result.get("access_token")
    refresh_token = token_result.get("refresh_token")
    if not access_token or not refresh_token:
        raise SetupFlowError("Missing access_token or refresh_token in Discord response")

    expires_in = token_result.get("expires_in", 604800)
    token_expires_at = time.time() + float(expires_in)

    return {
        "access_token": str(access_token),
        "refresh_token": str(refresh_token),
        "token_expires_at": token_expires_at,
    }
