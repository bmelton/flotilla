# Changelog

## 0.3.0
Rendering rewrite. 0.2.5 to 0.2.7 were unreleased steps toward this.
- Idle printers no longer hold a video stream: Python takes one JPEG snapshot every 10s and the page draws it on a canvas. Every tile is status-polled every 10s to tell idle from printing (#7)
- Tiles scrolled out of view, filtered out, or on a hidden Orca tab stop streaming
- Live tiles try fetch-and-canvas first and fall back to the multipart <img> that Orca's web view currently requires; WebKit's <img> loader leaks a Mach port per frame until macOS kills the tab (#9), so limiting it to printers mid-print is what keeps the tab alive overnight
- Elegoo: video is cycled before a live reload only; unhandled JS rejections and the fetch refusal reason are logged

## 0.2.4
- Elegoo: cycle the camera before each tile reload and wait 2s, so the 15-minute refresh no longer kills the tile for up to a minute

## 0.2.3
- Elegoo: cycle video Enable=0 then 1 over SDCP; the camera leaks client slots and answers "500 too many client" until it is disabled once

## 0.2.2
- Elegoo: enable the camera over SDCP at page load and whenever its tile dies; the Centauri Carbon boots with video off (#12)
- Request one network grant per printer host at load instead of re-prompting per URL after a printer reboot (#11)
- Close the old stream before reloading a tile so single-viewer cameras do not see two clients (#10)

## 0.2.1
- Re-request live tiles every 15 minutes and reload a tile on click; a stalled MJPEG stream never fires an error (#6)
- A stream URL that has delivered a frame no longer falls back to the next candidate on a later error

## 0.2.0
- Adds support for Snapmaker U1 printers at `/webcam/stream.mjpg`

## 0.1.0
- Initial release
