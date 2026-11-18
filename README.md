# Flotilla

OrcaSlicer plugin that adds a **Flotilla** tab: a live camera grid for every printer OrcaSlicer knows about, Watchtower-style. Orca's Device tab only shows the selected printer; this shows all of them.

## Install

Requires OrcaSlicer 2.5+ (plugin pages).

Plugins dialog → **Install local plugin** → pick `flotilla.py`. Or copy it to `<Orca data dir>/orca_plugins/flotilla/flotilla.py` and restart.

## How it works

- Every printer preset with a `print_host` gets a tile (deduped by host).
- Click a tile to fill the tab. The HUD shows status and offers Pause/Resume, Cancel, E-Stop, and Open in browser. Controls work on Moonraker printers and Elegoo (SDCP) printers; the protocol is auto-detected per host.
- The stream URL is guessed from `host_type`: Moonraker/Fluidd/Mainsail/OctoPrint/Qidi use `/webcam/?action=stream`, Elegoo uses `:3031/video`, PrusaLink `/webcam/snapshot`.
- Override any URL in the plugin's config page. Useful for Bambu printers, which have no HTTP stream: point a [go2rtc](https://github.com/AlexxIT/go2rtc) relay at them and paste its MJPEG URL.
- Search box and tile-size slider in the tab bar.

## Dev

```
python3 flotilla.py   # runs the self-check, prints "ok"
```
