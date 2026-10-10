# Discord Rich Presence Provider for Music Assistant

A plugin provider for [Music Assistant](https://music-assistant.io) that updates your Discord status with Rich Presence ("Listening to ...") using Discord's headless session API.

## Features

- **Rich Presence:** Displays track title, artist name, and album name on your Discord profile.
- **Dynamic Album Art:** Resolves and displays the active track's cover art as the large image.
- **Playback Timestamps:** Shows elapsed and remaining track duration.
- **Multi-Instance:** Run multiple instances side-by-side to track different speakers/players for different Discord accounts.
- **Headless Sessions:** Uses modern Discord headless sessions with automatic session keep-alive.

---

## Installation via Home Assistant Watcher Add-on

If using Home Assistant OS with the **Music Assistant Multi-Provider Watcher** add-on:

1. Push this repository to GitHub: `https://github.com/<your-username>/discord-presence-provider`.
2. Open Home Assistant → **Settings → Add-ons → Music Assistant Multi-Provider Watcher → Configuration**.
3. Add this repository URL to `providers`:
   ```yaml
   providers:
     - "https://github.com/sproft/ytmusic-free-provider"
     - "https://github.com/<your-username>/discord-presence-provider"
   ```
4. Save and restart the add-on.
5. The watcher will download the provider, copy `discord_presence` into the Music Assistant container, and restart Music Assistant.

---

## Discord Developer Application Setup

To use this provider, create a Discord Application:

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and give it a name (e.g. `Music Assistant`).
3. Note your **Application ID** (this is your `Client ID`).
4. In the left sidebar, navigate to **OAuth2**.
5. Under **Client Information**, copy or reset the **Client Secret**.
6. Under **Redirects**, click **Add Redirect** and enter your Music Assistant OAuth redirect URL:
   ```
   https://oauth.music-assistant.io/discord_presence
   ```
   *(or your local redirect URL if prompted during setup)*.
7. Save changes.

---

## Configuration in Music Assistant

1. In Music Assistant, go to **Settings → Apps (or Providers) → Add**.
2. Select **Discord Rich Presence**.
3. Enter your:
   - **Client ID**
   - **Client Secret**
4. Click **Authenticate with Discord** and authorize the application with your Discord account.
5. In the provider settings:
   - **Player:** Select which Music Assistant player/speaker triggers presence updates.
   - **Show timestamps:** Display elapsed / remaining duration.
   - **Show album art:** Show the current track's artwork.
   - **Keep-alive interval:** Interval (in seconds) to keep the Discord session active (default: 900s / 15 min).
6. Save and start playing music on your selected player. Your Discord profile will now show your music status!
