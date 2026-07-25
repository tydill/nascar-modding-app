# NASCAR Modding App

A modding tool for **NASCAR '15**, with mapped support for **NASCAR '14**.

Edits paint schemes, driver and team names, game text, driver ratings, audio,
race settings, AI and physics, and menu graphics — through a local web interface,
with a backup taken before the first change to any game archive.

Windows only. Unofficial and unaffiliated.

## Download

Get the latest zip from the [**Releases**](../../releases) page. No account needed.

## Setup

1. **Install Python 3.10 or newer.** Easiest is `winget install 9NQ7512CXL7T` in a
   terminal, or download from [python.org/downloads](https://www.python.org/downloads/).
   python.org and the Microsoft Store ship the same official install manager, so
   either is fine. If setup asks about paths longer than 260 characters, answer **y**.
2. **Extract the zip** somewhere simple like `C:\NascarApp`. Avoid OneDrive-synced
   folders — OneDrive can lock files while the app is writing to your game.
3. **Run `START_APP.bat`.** The first run installs Flask, Pillow and NumPy, which
   needs internet once. After that the app works fully offline.
4. **Pick your game, then set the game folder** — the folder that *contains*
   `data\`, not `data` itself.

Full instructions and troubleshooting ship inside the zip as `README_FIRST.txt`
and `TROUBLESHOOTING.txt`.

## Does it need internet?

Only for that first package install. The app makes no outbound connections,
has no telemetry and no update check, and serves its interface from your own PC
at `127.0.0.1`.

## Your mods and updates

Changes are written into the game's own files, with the app's backups beside
the
