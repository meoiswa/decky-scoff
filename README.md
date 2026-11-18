# ScOFF

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that turns the Steam Deck screen off
while everything keeps running: music, video audio, streaming, downloads, long-running tasks. Press a button and the
screen comes back.

ScOFF isn't in the Decky Plugin Store. You install it directly from GitHub, which takes about two minutes.
Short link to this page: **[scoff.meoiswa.cat](https://scoff.meoiswa.cat)**

## Installing

**You need:** a Steam Deck in Gaming Mode with [Decky Loader](https://decky.xyz) installed, and an internet
connection.

### 1. Turn on Decky's developer mode (one time only)

1. Press the **…** button to open the Quick Access Menu and go to the **Decky** tab (the plug icon).
2. Select the **gear icon** at the top to open Decky's settings.
3. Under **General**, turn on **Developer mode**. A new **Developer** section appears in the settings.

### 2. Install ScOFF

1. In Decky's settings, open **Developer**.
2. Under **Third-Party Plugins**, find **Install Plugin from URL** and enter this address:
   ```
   https://scoff.meoiswa.cat/zip
   ```
3. Select **Install**, then confirm the **Third-Party Plugin Installation** warning.
4. ScOFF now appears in the Decky tab.

The short address always points to the latest release. If it doesn't work, use the full GitHub address instead:
`https://github.com/meoiswa/decky-scoff/releases/latest/download/ScOFF.zip`

You can also download `ScOFF.zip` from the [latest release](https://github.com/meoiswa/decky-scoff/releases/latest)
to the Deck (for example in Desktop Mode), then in step 2 use **Install Plugin from ZIP File** → **Browse** and pick
the file.

### Updating

Repeat step 2. The new version replaces the old one and keeps your settings.

### Uninstalling

In Decky's settings, open **Plugins**, select the **…** button next to ScOFF and choose **Uninstall**.

## Using it

1. Open the Quick Access Menu (the **…** button), go to the Decky tab and open **ScOFF**.
2. Press **Turn screen off**. The menu closes and the screen goes dark.
3. To wake the screen, use any input you've enabled (see [Settings](#settings)). The input that wakes the screen is
   swallowed, so it doesn't also act in the UI or the game.

You can also **double-press the power button** to turn the screen off, and double-press again to turn it back on.
This works without opening the menu, in games too.

While the screen is off, all controller input is blocked: the Steam and QAM buttons don't open their menus and a
running game doesn't receive input. The power button works like this:

| Power button | What happens |
|---|---|
| Single press | The Deck goes to sleep as usual. When it wakes up, the screen comes back on by itself. |
| Hold (about 1 s) | The screen comes back on with Steam's power menu open (if enabled; otherwise the menu is blocked and the screen stays off). |
| Double press | Toggles the screen off or on, without sleeping (if enabled). |

## Settings

| Setting | Default | What it does |
|---|---|---|
| Hold power to wake | On | Holding power wakes the screen with the power menu open. When off, the power menu is blocked while the screen is off |
| Buttons wake the screen | On | Face buttons (A/B/X/Y), D-pad, bumpers (L1/R1), back buttons (L4/L5/R4/R5), View, Menu, Steam, QAM, stick clicks (L3/R3) |
| Sticks and triggers wake the screen | Off | Moving either stick past about 40 %, or pulling a trigger past about 25 % |
| Trackpads wake the screen | Off | Touching or clicking either trackpad |
| Double-press power to toggle | On | Double-pressing power turns the screen off, and on again |

At least one setting must be on, otherwise **Turn screen off** is disabled, since there'd be no way to wake the screen.
A single power press (sleep, then wake) always brings the screen back, whatever the settings.

With double-press enabled, a single power press waits an extra 0.4 s before the Deck sleeps, to check whether a second
press follows. Turn the setting off if you don't use it and want sleep to start immediately.

Resting a thumb on a stick never wakes the screen, only moving it does (when sticks are enabled).

## Good to know

- **The screen is really off.** ScOFF tells gamescope to switch the display output off, which turns off the panel and
  backlight. It isn't just a black picture.
- **It doesn't use Steam's low-power download mode.** CPU/GPU limits and the fan are left alone, so whatever's running
  keeps its full performance.
- **Steam doesn't know the screen is off.** If you've set Steam to sleep after a period of inactivity, the Deck may
  still go to sleep while the screen is off (not yet tested). Either way, the screen comes back on when it wakes.
- **A PIN lock on wake** shows as usual after the Deck wakes from sleep.

## If something goes wrong

- **Black screen and buttons do nothing:** press power once to put the Deck to sleep, wait a few seconds, then press
  power to wake it. The screen comes back after waking.
- **Backlight on but no picture:** same fix: one sleep/wake cycle.
- **From another computer over SSH:**
  ```
  XDG_RUNTIME_DIR=/run/user/1000 GAMESCOPE_WAYLAND_DISPLAY=gamescope-0 gamescopectl drm_sleep_internal_screen 0
  ```
- **Logs:** `~/homebrew/logs/decky-scoff/`

## Building from source

```
npm install
npm run build
sudo ./install.sh     # copies to ~/homebrew/plugins/decky-scoff and restarts Decky
```

`./package.sh` builds `out/ScOFF.zip`, the same file the releases contain.

### Publishing a release

Releases are built by GitHub Actions (`.github/workflows/release.yml`):

1. Bump `version` in `package.json` (for example to `1.0.1`) and commit.
2. Tag the commit with the same version and push it:
   ```
   git tag v1.0.1
   git push origin v1.0.1
   ```
3. The workflow builds `ScOFF.zip` and attaches it to a new release. The install address above always points to
   the latest release.

## Compatibility

Requires SteamOS in Gaming Mode on a Steam Deck. Tested on a Steam Deck LCD with SteamOS 3.9.6 (Beta); the OLED
model and other SteamOS versions are untested. ScOFF relies on internal parts of the Steam client, so a Steam update
can break it.
