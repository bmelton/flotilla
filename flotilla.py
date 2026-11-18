# /// script
# requires-python = ">=3.10"
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "Flotilla"
# description = "Watchtower-style camera grid for every printer OrcaSlicer knows about."
# author = "Barry Melton"
# version = "0.1.0"
# license = "MIT"
# ///
"""Flotilla tab: one tile per printer host, each an <img> on the printer's MJPEG stream.

Hosts come from the printer presets' print_host. Camera URLs are guessed from host_type and
can be overridden per printer in the plugin's config page.
"""
from __future__ import annotations

import html
import json

try:
    import orca  # type: ignore
except ImportError:  # running outside OrcaSlicer (self-check)
    orca = None

# ponytail: Bambu printers have no HTTP stream (proprietary TLS tunnel); they get a placeholder.
# Point a go2rtc/mediamtx relay at them and paste its MJPEG URL in config if you want tiles.
_STREAM_PATH = {
    "elegoolink": ":3031/video",  # Elegoo Centauri Carbon
    "prusalink": "/webcam/snapshot",
}
_DEFAULT_PATH = "/webcam/?action=stream"  # moonraker, fluidd, mainsail, octoprint, qidi


def base_url(print_host: str) -> str:
    h = print_host.strip().rstrip("/")
    return h if "://" in h else f"http://{h}"


def default_url(print_host: str, host_type: str) -> str:
    if not print_host:
        return ""
    return base_url(print_host) + _STREAM_PATH.get((host_type or "").lower(), _DEFAULT_PATH)


def dedupe(presets: list[dict]) -> list[dict]:
    """One entry per host; keep the shortest preset name (copies tend to grow suffixes)."""
    by_host: dict[str, dict] = {}
    for p in presets:
        host = (p.get("print_host") or "").strip()
        if not host:
            continue
        cur = by_host.get(host)
        if cur is None or len(p["name"]) < len(cur["name"]):
            by_host[host] = p
    return sorted(by_host.values(), key=lambda p: p["name"].lower())


def printers_from_bundle() -> list[dict]:
    coll = orca.host.preset_bundle().printers
    out = []
    for i in range(coll.size()):
        p = coll.preset(i)
        out.append({
            "name": p.name,
            "print_host": p.config_value("print_host") or "",
            "host_type": p.config_value("host_type") or "",
        })
    return dedupe(out)


def tiles(printers: list[dict], cfg: dict) -> list[dict]:
    return [{"name": p["name"],
             "url": (cfg.get(p["name"]) or "").strip() or default_url(p["print_host"], p["host_type"])}
            for p in printers]


_CSS = """
body{margin:0;background:var(--orca-bg,#1b1b1b);color:var(--orca-fg,#eee);font:14px var(--orca-font,system-ui)}
.bar{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--orca-border,#333)}
.bar input{flex:1;max-width:360px;background:transparent;color:inherit;border:1px solid var(--orca-border,#444);border-radius:6px;padding:6px 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--w,340px),1fr));gap:12px;padding:12px}
.tile{position:relative;background:#000;border:1px solid var(--orca-border,#333);border-radius:6px;overflow:hidden;aspect-ratio:4/3}
.tile img{width:100%;height:100%;object-fit:cover;display:block}
.tile .cap{position:absolute;left:0;right:0;bottom:0;padding:6px;text-align:center;background:rgba(0,0,0,.55);color:#fff}
.tile.dead img{display:none}
.tile.dead::before{content:'no stream';position:absolute;inset:0;display:grid;place-items:center;color:var(--orca-muted,#888)}
.tile.hidden{display:none}
.tile{cursor:zoom-in}
.tile.full{position:fixed;inset:0;z-index:9;aspect-ratio:auto;border:0;border-radius:0;cursor:zoom-out}
.tile.full img{object-fit:contain}
"""

_JS = """
const q=document.getElementById('q'), w=document.getElementById('w');
q.oninput=()=>{const s=q.value.toLowerCase();for(const t of document.querySelectorAll('.tile'))t.classList.toggle('hidden',!t.dataset.name.includes(s));};
w.oninput=()=>document.querySelector('.grid').style.setProperty('--w',w.value+'px');
for(const img of document.querySelectorAll('.tile img')){img.onerror=()=>img.parentElement.classList.add('dead');}
const unfull=()=>document.querySelectorAll('.tile.full').forEach(t=>t.classList.remove('full'));
for(const t of document.querySelectorAll('.tile')){t.onclick=()=>{const was=t.classList.contains('full');unfull();if(!was)t.classList.add('full');};}
document.onkeydown=e=>{if(e.key==='Escape')unfull();};
"""


def page_html(items: list[dict]) -> str:
    if not items:
        body = "<p style='padding:24px'>No printer presets have a print host set.</p>"
    else:
        body = "".join(
            f"<div class='tile{'' if t['url'] else ' dead'}' data-name='{html.escape(t['name'].lower())}'>"
            f"<img src='{html.escape(t['url'])}' alt=''><div class='cap'>{html.escape(t['name'])}</div></div>"
            for t in items)
        body = f"<div class='grid'>{body}</div>"
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{_CSS}</style></head><body>"
            f"<div class='bar'><input id='q' placeholder='Search printers…'>"
            f"<input id='w' type='range' min='200' max='800' value='340' style='max-width:160px'></div>"
            f"{body}<script>{_JS}</script></body></html>")


def config_html(printers: list[dict]) -> str:
    rows = "".join(
        f"<label><span>{html.escape(p['name'])}</span>"
        f"<input name='{html.escape(p['name'])}' placeholder='{html.escape(default_url(p['print_host'], p['host_type']))}'></label>"
        for p in printers)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{font:14px var(--orca-font,system-ui);color:var(--orca-fg);background:var(--orca-bg);padding:12px}}
label{{display:block;margin-bottom:10px}} label span{{display:block;font-weight:600;margin-bottom:3px}}
input{{width:100%;box-sizing:border-box;padding:6px 8px;background:transparent;color:inherit;border:1px solid var(--orca-border,#444);border-radius:6px}}
</style></head><body><p>Camera stream URL per printer. Leave empty to use the placeholder default.</p>
<form id="f">{rows}<button type="submit">Save</button></form><script>
const f=document.getElementById('f');
orca.onConfig(cfg=>{{for(const el of f.elements)if(el.name)el.value=cfg[el.name]||'';}});
f.addEventListener('submit',e=>{{e.preventDefault();const cfg={{}};for(const el of f.elements)if(el.name)cfg[el.name]=el.value.trim();orca.saveConfig(cfg);}});
</script></body></html>"""


if orca is not None:
    _PagesBase = getattr(getattr(orca, "pages", None), "PagesPluginCapabilityBase", None)

    if _PagesBase is not None:
        class FlotillaPage(_PagesBase):
            def get_name(self):
                return "Flotilla"

            def _cfg(self) -> dict:
                try:
                    return json.loads(self.get_config() or "{}") or {}
                except (ValueError, TypeError):
                    return {}

            def get_ui(self):
                try:
                    return page_html(tiles(printers_from_bundle(), self._cfg()))
                except Exception as e:  # never take the tab down
                    return f"<pre style='padding:16px;color:var(--orca-fg)'>Flotilla error: {html.escape(str(e))}</pre>"

            def has_config_ui(self):
                return True

            def get_config_ui(self):
                return config_html(printers_from_bundle())

            def get_default_config(self):
                return {}

        _Capability = FlotillaPage
    else:
        class FlotillaNeedsPages(orca.script.ScriptPluginCapabilityBase):
            def get_name(self):
                return "Flotilla (needs OrcaSlicer 2.5+)"

            def execute(self):
                return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError,
                                                    "Flotilla needs an OrcaSlicer build with plugin pages (orca.pages).")

        _Capability = FlotillaNeedsPages

    @orca.plugin
    class FlotillaPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(_Capability)


if __name__ == "__main__":
    assert default_url("sv08.local", "moonraker") == "http://sv08.local/webcam/?action=stream"
    assert default_url("https://q2.lan:7125/", "") == "https://q2.lan:7125/webcam/?action=stream"
    assert default_url("cc.lan", "ElegooLink") == "http://cc.lan:3031/video"
    assert default_url("", "moonraker") == ""
    d = dedupe([{"name": "SV08 - Copy", "print_host": "a", "host_type": ""}, {"name": "SV08", "print_host": "a", "host_type": ""},
                {"name": "sys", "print_host": "", "host_type": ""}, {"name": "Q2", "print_host": "b", "host_type": ""}])
    assert [p["name"] for p in d] == ["Q2", "SV08"], d
    t = tiles([{"name": "Q2", "print_host": "b", "host_type": ""}], {"Q2": "http://relay/x"})
    assert t[0]["url"] == "http://relay/x"
    assert "no-such" not in page_html(t) and "Q2" in page_html(t) and "Q2" in config_html(d)
    print("ok")
