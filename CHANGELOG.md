# Changelog

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
