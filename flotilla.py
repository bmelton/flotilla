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
import threading
import urllib.request
import webbrowser

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
    return [{"name": p["name"], "host": p["print_host"],
             "url": (cfg.get(p["name"]) or "").strip() or default_url(p["print_host"], p["host_type"])}
            for p in printers]


# --- Moonraker --------------------------------------------------------------------
# ponytail: Moonraker only. Elegoo's SDCP websocket and OctoPrint's REST get "unsupported".
_QUERY = "/printer/objects/query?print_stats&display_status&extruder&heater_bed"
_ACTIONS = {"pause": "/printer/print/pause", "resume": "/printer/print/resume",
            "cancel": "/printer/print/cancel", "estop": "/printer/emergency_stop"}


def _http(url: str, method: str = "GET") -> dict:
    req = urllib.request.Request(url, method=method, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.loads(r.read().decode())


def summarize(result: dict) -> dict:
    """Flatten a Moonraker objects/query reply into what the strip shows."""
    st = result.get("status", {})
    ps, ds, ex, hb = (st.get(k, {}) for k in ("print_stats", "display_status", "extruder", "heater_bed"))
    prog = ds.get("progress") or 0
    dur = ps.get("print_duration") or 0
    return {"state": ps.get("state", "?"), "file": ps.get("filename", ""), "progress": round(prog * 100),
            "elapsed": int(dur), "remaining": int(dur / prog - dur) if prog > 0.01 else None,
            "nozzle": [round(ex.get("temperature", 0)), round(ex.get("target", 0))],
            "bed": [round(hb.get("temperature", 0)), round(hb.get("target", 0))]}


def moonraker(host: str, cmd: str) -> dict:
    base = base_url(host)
    if cmd == "status":
        return {"type": "status", "host": host, "data": summarize(_http(base + _QUERY)["result"])}
    _http(base + _ACTIONS[cmd], "POST")
    return {"type": "done", "host": host, "cmd": cmd}


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
.hud{display:none;position:absolute;left:0;right:0;bottom:0;padding:10px 14px;background:rgba(0,0,0,.7);color:#fff;cursor:default}
.tile.full .hud{display:flex;gap:10px;align-items:center;flex-wrap:wrap} .tile.full .cap{display:none}
.hud .st{flex:1;min-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hud button{background:#333;color:#fff;border:1px solid #555;border-radius:6px;padding:6px 12px;cursor:pointer}
.hud button:disabled{opacity:.4;cursor:default} .hud button.arm{background:#b33;border-color:#e55}
.hud button.estop{border-color:#b33}
"""

_JS = r"""
const q=document.getElementById('q'), w=document.getElementById('w');
q.oninput=()=>{const s=q.value.toLowerCase();for(const t of document.querySelectorAll('.tile'))t.classList.toggle('hidden',!t.dataset.name.includes(s));};
w.oninput=()=>document.querySelector('.grid').style.setProperty('--w',w.value+'px');
for(const img of document.querySelectorAll('.tile img')){img.onerror=()=>img.parentElement.classList.add('dead');}
const unfull=()=>{document.querySelectorAll('.tile.full').forEach(t=>t.classList.remove('full'));if(typeof watch==='function')watch(null);};
for(const t of document.querySelectorAll('.tile')){t.onclick=()=>{const was=t.classList.contains('full');unfull();if(!was)t.classList.add('full');};}
document.onkeydown=e=>{if(e.key==='Escape')unfull();};
const fmt=s=>s==null?'?':(s>=3600?Math.floor(s/3600)+'h':'')+Math.floor(s%3600/60)+'m';
let timer=null, cur=null;
const send=m=>orca.postMessage(m);
function poll(){if(cur)send({cmd:'status',host:cur.dataset.host});}
function watch(t){cur=t;clearInterval(timer);if(t){poll();timer=setInterval(poll,3000);}}
for(const t of document.querySelectorAll('.tile')){t.onclick=e=>{if(e.target.closest('.hud'))return;const was=t.classList.contains('full');unfull();if(!was){t.classList.add('full');watch(t);}else watch(null);};}
document.querySelectorAll('.hud button').forEach(b=>b.onclick=()=>{
  const t=b.closest('.tile'), host=t.dataset.host, cmd=b.dataset.cmd;
  if(cmd==='open')return send({cmd:'open',host});
  if(!b.dataset.confirm||b.classList.contains('arm')){b.classList.remove('arm');if(b.dataset.label)b.textContent=b.dataset.label;b.disabled=true;send({cmd,host});return;}
  b.classList.add('arm');b.dataset.label=b.textContent;b.textContent='✓ Confirm '+b.dataset.label.replace(/^\S+ /,'')+'?';
  setTimeout(()=>{if(b.classList.contains('arm')){b.classList.remove('arm');b.textContent=b.dataset.label;}},4000);
});
function render(t,d){
  t.querySelector('.st').textContent=`${d.state}${d.file?' · '+d.file:''}${d.progress?' · '+d.progress+'%':''} · ${fmt(d.elapsed)}${d.remaining!=null?' / ~'+fmt(d.remaining)+' left':''} · nozzle ${d.nozzle[0]}/${d.nozzle[1]} · bed ${d.bed[0]}/${d.bed[1]}`;
  const printing=d.state==='printing', paused=d.state==='paused';
  const pr=t.querySelector('[data-cmd=pause],[data-cmd=resume]');
  pr.dataset.cmd=paused?'resume':'pause'; pr.textContent=paused?'▶ Resume':'⏸ Pause'; pr.disabled=!(printing||paused);
  t.querySelector('[data-cmd=cancel]').disabled=!(printing||paused);
}
orca.onMessage(m=>{
  if(typeof m==='string')m=JSON.parse(m);
  const t=document.querySelector(`.tile[data-host="${CSS.escape(m.host)}"]`); if(!t)return;
  if(m.type==='status')render(t,m.data);
  else if(m.type==='error'){t.querySelector('.st').textContent=m.message;t.querySelectorAll('[data-cmd=pause],[data-cmd=resume],[data-cmd=cancel]').forEach(b=>b.disabled=true);}
  if(m.type==='done'){t.querySelectorAll('.hud button').forEach(b=>b.disabled=false);poll();}
  if(m.type==='error')t.querySelector('[data-cmd=estop]').disabled=false;
});
"""


def page_html(items: list[dict]) -> str:
    if not items:
        body = "<p style='padding:24px'>No printer presets have a print host set.</p>"
    else:
        body = "".join(
            f"<div class='tile{'' if t['url'] else ' dead'}' data-name='{html.escape(t['name'].lower())}' data-host='{html.escape(t['host'])}'>"
            f"<img src='{html.escape(t['url'])}' alt=''><div class='cap'>{html.escape(t['name'])}</div>"
            f"<div class='hud'><span class='st'>{html.escape(t['name'])} · connecting…</span>"
            f"<button data-cmd='pause' data-confirm='1' disabled>⏸ Pause</button><button data-cmd='cancel' data-confirm='1' disabled>⏹ Cancel</button>"
            f"<button class='estop' data-cmd='estop' data-confirm='1'>⚠ E-Stop</button><button data-cmd='open'>↗ Open in browser</button></div></div>"
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

            def on_message(self, msg):
                if isinstance(msg, str):
                    msg = json.loads(msg)
                host, cmd = msg.get("host", ""), msg.get("cmd", "")
                if cmd == "open":
                    webbrowser.open(base_url(host))
                    return
                def work():
                    try:
                        self.post_message(moonraker(host, cmd))
                    except Exception as e:
                        self.post_message({"type": "error", "host": host, "message": f"unsupported or offline ({e.__class__.__name__})"})
                # on_message runs on the UI thread; a dead host would freeze Orca for the timeout.
                try:
                    threading.Thread(target=work, daemon=True).start()
                except Exception:
                    work()

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
            try:
                hosts = [base_url(p["print_host"]) for p in printers_from_bundle()]
                orca.request_permissions(network_http=hosts, threading=True)
            except Exception:
                pass  # bundle not ready or older host: first use prompts instead
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
    sm = summarize({"status": {"print_stats": {"state": "printing", "filename": "a.gcode", "print_duration": 600},
                               "display_status": {"progress": 0.25}, "extruder": {"temperature": 210.4, "target": 210},
                               "heater_bed": {"temperature": 59.6, "target": 60}}})
    assert sm["progress"] == 25 and sm["remaining"] == 1800 and sm["nozzle"] == [210, 210], sm
    assert summarize({"status": {}})["remaining"] is None
    assert "data-host='b'" in page_html(t) and "⚠ E-Stop" in page_html(t)
    print("ok")
