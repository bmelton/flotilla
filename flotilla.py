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

import base64
import html
import json
import os
import platform
import socket
import sys
import struct
import threading
import time
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
    _names.update({p["print_host"]: p["name"] for p in printers})
    return [{"name": p["name"], "host": p["print_host"],
             "url": (cfg.get(p["name"]) or "").strip() or default_url(p["print_host"], p["host_type"])}
            for p in printers]


# --- logging: one JSON-lines file, flotilla.log in the plugin's storage dir -----------
_LOG_MAX = 2_000_000  # bytes; rotate to flotilla.log.1
_log_lock = threading.Lock()
_names: dict[str, str] = {}       # host -> preset name
_firmware: dict[str, str] = {}    # host -> firmware string, learned on first status


def log_path() -> "Path":
    from pathlib import Path
    try:
        d = Path(orca.host.plugin.storage())
    except Exception:
        d = Path(__file__).resolve().parent
    return d / "flotilla.log"


def log(event: str, host: str = "", level: str = "info", **fields) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "level": level, "event": event}
    if host:
        fn = _proto.get(host)
        rec.update(printer=_names.get(host, ""), host=host, proto=getattr(fn, "__name__", None),
                   firmware=_firmware.get(host))
    rec.update(fields)
    try:
        p = log_path()
        with _log_lock:
            if p.exists() and p.stat().st_size > _LOG_MAX:
                p.replace(p.with_suffix(".log.1"))
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass  # logging must never break the plugin


# --- Moonraker --------------------------------------------------------------------
# ponytail: Moonraker and Elegoo SDCP. OctoPrint/PrusaLink get "unsupported".
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
        if host not in _firmware:
            _firmware[host] = "moonraker " + str(_http(base + "/printer/info")["result"].get("software_version", "?"))
        return {"type": "status", "host": host, "data": summarize(_http(base + _QUERY)["result"])}
    _http(base + _ACTIONS[cmd], "POST")
    return {"type": "done", "host": host, "cmd": cmd}


# --- Elegoo SDCP (websocket on :3030) ------------------------------------------------
# Minimal RFC 6455 client: text frames only, no fragmentation. Enough for request/response.
def _ws_connect(host: str, port: int, path: str, timeout: float = 5) -> socket.socket:
    s = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    if b" 101 " not in buf.split(b"\r\n", 1)[0]:
        raise ConnectionError("websocket handshake refused")
    return s


def _ws_read(s: socket.socket, n: int) -> bytes:
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise ConnectionError("websocket closed")
        b += c
    return b


def _ws_recv(s: socket.socket) -> str:
    h = _ws_read(s, 2)
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _ws_read(s, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _ws_read(s, 8))[0]
    return _ws_read(s, ln).decode(errors="replace")


def _ws_send(s: socket.socket, text: str) -> None:
    p = text.encode()
    m = os.urandom(4)
    h = bytes([0x81]) + (bytes([0x80 | len(p)]) if len(p) < 126 else bytes([0x80 | 126]) + struct.pack(">H", len(p)))
    s.sendall(h + m + bytes(b ^ m[i % 4] for i, b in enumerate(p)))


_SDCP_CMD = {"pause": 129, "cancel": 130, "resume": 131}
_SDCP_STATE = {0: "standby", 1: "homing", 5: "pausing", 6: "paused", 7: "stopping", 8: "cancelled",
               9: "complete", 13: "printing", 15: "heating", 16: "printing", 20: "resuming"}


def summarize_sdcp(status: dict) -> dict:
    pi = status.get("PrintInfo", {})
    code = pi.get("Status", 0)
    done, total = pi.get("CurrentTicks") or 0, pi.get("TotalTicks") or 0
    return {"state": _SDCP_STATE.get(code, f"status {code}"), "file": pi.get("Filename", ""),
            "progress": pi.get("Progress", 0), "elapsed": int(done),
            "remaining": int(total - done) if total > done else None,
            "nozzle": [round(status.get("TempOfNozzle", 0)), round(status.get("TempTargetNozzle", 0))],
            "bed": [round(status.get("TempOfHotbed", 0)), round(status.get("TempTargetHotbed", 0))]}


def sdcp(host: str, cmd: str) -> dict:
    if cmd == "estop":
        raise ValueError("Elegoo SDCP has no emergency stop; use Cancel")
    hostname = base_url(host).split("://", 1)[1].split("/")[0].split(":")[0]
    s = _ws_connect(hostname, 3030, "/websocket")
    try:
        # Ask for status (Cmd 0) explicitly: unsolicited pushes can be seconds apart while paused.
        _ws_send(s, json.dumps({"Id": "", "Data": {"Cmd": 0, "Data": {}, "RequestID": os.urandom(8).hex(),
                                "MainboardID": "", "TimeStamp": int(time.time() * 1000), "From": 1}}))
        for _ in range(8):
            msg = json.loads(_ws_recv(s))
            if "Status" in msg:
                break
        else:
            raise ConnectionError("no SDCP status frame")
        if cmd == "status":
            if host not in _firmware:  # Cmd 1 = request attributes; answered in <1s
                _ws_send(s, json.dumps({"Id": "", "Data": {"Cmd": 1, "Data": {}, "RequestID": os.urandom(8).hex(),
                                        "MainboardID": msg["MainboardID"], "TimeStamp": int(time.time() * 1000), "From": 1}}))
                for _ in range(6):
                    m2 = json.loads(_ws_recv(s))
                    if "Attributes" in m2:
                        a = m2["Attributes"]
                        _firmware[host] = f"{a.get('MachineName', '?')} {a.get('FirmwareVersion', '?')} sdcp {a.get('ProtocolVersion', '?')}"
                        break
            return {"type": "status", "host": host, "data": summarize_sdcp(msg["Status"])}
        rid = os.urandom(8).hex()
        _ws_send(s, json.dumps({"Id": "", "Data": {"Cmd": _SDCP_CMD[cmd], "Data": {}, "RequestID": rid,
                                "MainboardID": msg["MainboardID"], "TimeStamp": int(time.time() * 1000), "From": 1}}))
        for _ in range(10):
            r = json.loads(_ws_recv(s))
            if r.get("Data", {}).get("RequestID") == rid:
                if r["Data"].get("Data", {}).get("Ack", 1) != 0:
                    raise RuntimeError("printer refused; probably already in that state")
                return {"type": "done", "host": host, "cmd": cmd}
        raise TimeoutError("no SDCP ack")
    finally:
        s.close()


_proto: dict[str, object] = {}  # host -> client fn that last worked


def control(host: str, cmd: str) -> dict:
    fns = [_proto[host]] if host in _proto else [moonraker, sdcp]
    err: Exception = RuntimeError("no client")
    for fn in fns:
        t0 = time.time()
        try:
            out = fn(host, cmd)
            _proto[host] = fn
            log("command" if cmd != "status" else "status", host, cmd=cmd, ms=int((time.time() - t0) * 1000),
                state=out.get("data", {}).get("state") if cmd == "status" else None)
            return out
        except Exception as e:
            err = e
            log("command_failed" if cmd != "status" else "status_failed", host, level="warn", cmd=cmd,
                client=fn.__name__, ms=int((time.time() - t0) * 1000), error=f"{e.__class__.__name__}: {e}")
    _proto.pop(host, None)  # ponytail: forget the cached client so a rebooted printer gets re-detected
    raise err


_CSS = """
body{margin:0;background:var(--orca-bg,#1b1b1b);color:var(--orca-fg,#eee);font:14px var(--orca-font,system-ui)}
.bar{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--orca-border,#333)}
.bar button{background:transparent;color:inherit;border:1px solid var(--orca-border,#444);border-radius:6px;padding:6px 10px;cursor:pointer}
.bar .retry{margin-left:auto;display:flex;gap:6px;align-items:center;white-space:nowrap} .bar .retry input{flex:none}
.bar select{background:transparent;color:inherit;border:1px solid var(--orca-border,#444);border-radius:6px;padding:6px}
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
.hud .row{display:flex;gap:10px;align-items:center;flex:1;flex-wrap:wrap} .hud .row[hidden]{display:none}
.hud .yes.danger{background:#b33;border-color:#e55}
.hud button{background:#333;color:#fff;border:1px solid #555;border-radius:6px;padding:6px 12px;cursor:pointer}
.hud button:disabled{opacity:.4;cursor:default} .hud button.arm{background:#b33;border-color:#e55}
.hud button.estop{border-color:#b33}
"""

_JS = r"""
const q=document.getElementById('q'), w=document.getElementById('w');
document.getElementById('log').onclick=()=>orca.postMessage({cmd:'openlog'});
q.oninput=()=>{const s=q.value.toLowerCase();for(const t of document.querySelectorAll('.tile'))t.classList.toggle('hidden',!t.dataset.name.includes(s));};
w.oninput=()=>document.querySelector('.grid').style.setProperty('--w',w.value+'px');
const plog=(t,event,fields)=>orca.postMessage({cmd:'log',host:t.dataset.host,event,fields:fields||{}});
for(const img of document.querySelectorAll('.tile img')){const t=img.parentElement;img.dataset.src=img.getAttribute('src');
  img.onerror=()=>{if(!t.classList.contains('dead')){t.classList.add('dead');plog(t,'stream_dead',{url:img.dataset.src});}};
  img.onload=()=>{if(t.classList.contains('dead')){t.classList.remove('dead');plog(t,'stream_recovered');}};}
// ponytail: retry dead tiles every 60s. A stream that freezes mid-print without an error isn't detected; click the tile to force a reload.
const ar=document.getElementById('ar'), ai=document.getElementById('ai'); let retryTimer=null;
try{const sv=JSON.parse(localStorage.getItem('retry')||'{}');if('on'in sv)ar.checked=sv.on;if(sv.s)ai.value=String(sv.s);}catch(e){}
function retryDead(){for(const t of document.querySelectorAll('.tile.dead')){const img=t.querySelector('img');if(img.dataset.src)img.src=img.dataset.src+(img.dataset.src.includes('?')?'&':'?')+'_r='+Date.now();}}
function schedule(){clearInterval(retryTimer);ai.disabled=!ar.checked;if(ar.checked)retryTimer=setInterval(retryDead,ai.value*1000);try{localStorage.setItem('retry',JSON.stringify({on:ar.checked,s:+ai.value}));}catch(e){}}
ar.onchange=ai.onchange=schedule; schedule();
window.onerror=(m,src,line)=>orca.postMessage({cmd:'log',event:'js_error',level:'error',fields:{message:String(m),line}});
const unfull=()=>{document.querySelectorAll('.tile.full').forEach(t=>t.classList.remove('full'));if(typeof watch==='function')watch(null);};
const fmt=s=>s==null?'?':(s>=3600?Math.floor(s/3600)+'h':'')+Math.floor(s%3600/60)+'m';
let timer=null, cur=null;
const send=m=>orca.postMessage(m);
function poll(){if(cur)send({cmd:'status',host:cur.dataset.host});}
function watch(t){cur=t;clearInterval(timer);if(t){poll();timer=setInterval(poll,3000);}}
for(const t of document.querySelectorAll('.tile')){t.onclick=e=>{if(e.target.closest('.hud'))return;if(t.querySelector('.hud').dataset.state==='confirm'){disarm(t);return;}const was=t.classList.contains('full');unfull();if(!was){t.classList.add('full');watch(t);}else watch(null);};}
const QUESTION={pause:['Pause this print?','Yes, pause'],resume:['Resume this print?','Yes, resume'],cancel:['Cancel this print?','Yes, cancel'],estop:['Emergency stop?','Yes, stop']};
function disarm(t){const h=t.querySelector('.hud');clearTimeout(h._t);h.dataset.state='idle';h.querySelector('.confirm').hidden=true;h.querySelector('.actions').hidden=false;}
function arm(t,cmd){const h=t.querySelector('.hud');disarm(t);h.dataset.state='confirm';const [q,y]=QUESTION[cmd];
  h.querySelector('.q').textContent=q;const yes=h.querySelector('.yes');yes.textContent=y;yes.dataset.cmd=cmd;yes.classList.toggle('danger',cmd==='cancel'||cmd==='estop');
  h.querySelector('.actions').hidden=true;h.querySelector('.confirm').hidden=false;h._t=setTimeout(()=>disarm(t),6000);}
function fire(t,cmd){const h=t.querySelector('.hud');disarm(t);h.dataset.state='busy';h.querySelectorAll('button').forEach(b=>b.disabled=true);send({cmd,host:t.dataset.host});}
document.querySelectorAll('.hud .actions button').forEach(b=>b.onclick=()=>{const t=b.closest('.tile');if(b.dataset.cmd==='open')return send({cmd:'open',host:t.dataset.host});b.dataset.confirm?arm(t,b.dataset.cmd):fire(t,b.dataset.cmd);});
document.querySelectorAll('.hud .yes').forEach(b=>b.onclick=()=>fire(b.closest('.tile'),b.dataset.cmd));
document.querySelectorAll('.hud .no').forEach(b=>b.onclick=()=>disarm(b.closest('.tile')));
document.onkeydown=e=>{if(e.key!=='Escape')return;const c=document.querySelector('.hud[data-state=confirm]');c?disarm(c.closest('.tile')):unfull();};
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
  if(m.type==='done'){t.querySelector('.hud').dataset.state='idle';t.querySelectorAll('.hud button').forEach(b=>b.disabled=false);poll();}
  if(m.type==='error'){t.querySelector('.hud').dataset.state='idle';t.querySelectorAll('[data-cmd=estop],[data-cmd=open],.no').forEach(b=>b.disabled=false);}
});
"""


def page_html(items: list[dict]) -> str:
    if not items:
        body = "<p style='padding:24px'>No printer presets have a print host set.</p>"
    else:
        body = "".join(
            f"<div class='tile{'' if t['url'] else ' dead'}' data-name='{html.escape(t['name'].lower())}' data-host='{html.escape(t['host'])}'>"
            f"<img src='{html.escape(t['url'])}' alt=''><div class='cap'>{html.escape(t['name'])}</div>"
            f"<div class='hud' data-state='idle'><span class='st'>{html.escape(t['name'])} · connecting…</span>"
            f"<div class='row actions'><button data-cmd='pause' data-confirm='1' disabled>⏸ Pause</button><button data-cmd='cancel' data-confirm='1' disabled>⏹ Cancel</button>"
            f"<button class='estop' data-cmd='estop' data-confirm='1'>⚠ E-Stop</button><button data-cmd='open'>↗ Open in browser</button></div>"
            f"<div class='row confirm' hidden><span class='q'></span><button class='yes'></button><button class='no'>No</button></div></div></div>"
            for t in items)
        body = f"<div class='grid'>{body}</div>"
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{_CSS}</style></head><body>"
            f"<div class='bar'><input id='q' placeholder='Search printers…'>"
            f"<input id='w' type='range' min='200' max='800' value='340' style='max-width:160px'>"
            f"<label class='retry'><input id='ar' type='checkbox' checked> Auto-retry</label>"
            f"<select id='ai'><option value='1'>1 second</option><option value='5'>5 seconds</option><option value='10'>10 seconds</option>"
            f"<option value='30'>30 seconds</option><option value='60' selected>1 minute</option><option value='300'>5 minutes</option></select>"
            f"<button id='log' title='Open flotilla.log'>Log</button></div>"
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
                    log("get_ui_failed", level="error", error=repr(e))
                    return f"<pre style='padding:16px;color:var(--orca-fg)'>Flotilla error: {html.escape(str(e))}</pre>"

            def on_load(self):
                try:
                    v = orca.host.preset_bundle  # proves the host module is live
                    bundle = printers_from_bundle()
                except Exception:
                    bundle = []
                log("plugin_loaded", plugin_version="0.1.0", python=sys.version.split()[0], platform=platform.platform(),
                    printers=[{"name": p["name"], "host": p["print_host"], "host_type": p["host_type"]} for p in bundle])

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
                    log("open_browser", host)
                    webbrowser.open(base_url(host))
                    return
                if cmd == "openlog":
                    webbrowser.open(log_path().as_uri())
                    return
                if cmd == "log":  # page-side events: stream dead/recovered, js errors
                    log(msg.get("event", "page"), host, level=msg.get("level", "info"), **msg.get("fields", {}))
                    return
                def work():
                    try:
                        self.post_message(control(host, cmd))
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
                ps = printers_from_bundle()
                hosts = [base_url(p["print_host"]) for p in ps]
                socks = [str((base_url(p["print_host"]).split("://", 1)[1].split("/")[0].split(":")[0], 3030)) for p in ps]
                orca.request_permissions(network_http=hosts, network_socket=socks, threading=True)
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
    assert "data-host='b'" in page_html(t) and "⚠ E-Stop" in page_html(t) and "class='row confirm'" in page_html(t)
    ss = summarize_sdcp({"PrintInfo": {"Status": 13, "CurrentLayer": 9, "TotalLayer": 73, "CurrentTicks": 2615.4,
                                       "TotalTicks": 10314, "Filename": "x.gcode", "Progress": 20},
                         "TempOfNozzle": 234.85, "TempTargetNozzle": 235, "TempOfHotbed": 80.1, "TempTargetHotbed": 80})
    assert ss["state"] == "printing" and ss["remaining"] == 7698 and ss["nozzle"] == [235, 235], ss
    assert summarize_sdcp({"PrintInfo": {"Status": 6}})["state"] == "paused"
    import tempfile, pathlib
    _d = tempfile.mkdtemp(); _old = log_path
    log_path = lambda: pathlib.Path(_d) / "flotilla.log"  # noqa: E731
    _names["h"] = "P"; _firmware["h"] = "fw1"
    log("status", "h", cmd="status", state="printing"); log("plugin_loaded", python="3")
    lines = [json.loads(l) for l in log_path().read_text().splitlines()]
    assert lines[0]["printer"] == "P" and lines[0]["firmware"] == "fw1" and lines[1]["event"] == "plugin_loaded", lines
    log_path = _old
    print("ok")
