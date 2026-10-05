"""Loopback-only live telemetry companion for the Gazebo 3D scene."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RIOSE MVP3 Live Twin</title><style>
:root{color-scheme:dark;--bg:#101923;--card:#182633;--edge:#304553;--muted:#a7bac4;--green:#71dcad;--blue:#75c9ed;--amber:#f2c879}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#f1f6f8;font:15px/1.4 system-ui,sans-serif}header{padding:16px 20px;border-bottom:1px solid var(--edge);display:flex;gap:16px;align-items:center;justify-content:space-between}h1{font-size:20px;margin:0}h2{font-size:14px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 12px}.meta{color:var(--muted);font-size:12px}.badge{border:1px solid #548e79;color:var(--green);border-radius:99px;padding:5px 10px;font-size:12px}.grid{padding:16px;display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--edge);border-radius:12px;padding:14px;min-height:108px}.wide{grid-column:span 2}.value{font-size:24px;font-weight:650;letter-spacing:-.02em}.small{font-size:14px}.unit{color:var(--muted);font-size:12px;margin-left:5px}.axes{display:flex;gap:14px;margin-top:8px}.axis{font-variant-numeric:tabular-nums}.x{color:#ff8c83}.y{color:#83dfa8}.z{color:#83cafa}#beam{width:100%;height:115px;display:block}.node{fill:#1d3140;stroke:#7094a5;stroke-width:2}.nodeText{fill:#f1f6f8;font:12px system-ui}.beam{stroke:#547382;stroke-width:3;stroke-dasharray:7 8}.beam.flash{stroke:#ffc45b;stroke-width:5;filter:drop-shadow(0 0 5px #ffcb6b);animation:pulse .75s ease-out 2}@keyframes pulse{from{stroke-dashoffset:30;opacity:1}to{stroke-dashoffset:0;opacity:.4}}.event{min-height:22px;color:var(--amber)}footer{padding:12px 18px 20px;color:var(--muted);font-size:12px}.warn{color:var(--amber)}@media(max-width:800px){.grid{grid-template-columns:repeat(2,minmax(140px,1fr))}.wide{grid-column:span 2}}@media(max-width:420px){.grid{grid-template-columns:1fr}.wide{grid-column:span 1}}
</style></head><body><header><div><h1>RIOSE MVP3 · live companion panel</h1><div class="meta">Gazebo Harmonic + Zephyr/Renode · simulated data</div></div><span class="badge" id="connection">CONNECTING</span></header>
<main class="grid">
<section class="card"><h2>Simulation / Renode clock</h2><div class="value"><span id="sim">—</span><span class="unit">s</span></div><div class="meta">Firmware clock <span id="renode">—</span> s · Δ <span id="error">—</span> s</div></section>
<section class="card"><h2>Firmware state</h2><div class="value" id="state">—</div><div class="meta">TX radio state: <span id="radio">—</span> · tag ID <span id="tag">—</span></div></section>
<section class="card"><h2>Latest IMU · sensor frame</h2><div class="axes"><span class="axis x">X <b id="ax">—</b> g</span><span class="axis y">Y <b id="ay">—</b> g</span><span class="axis z">Z <b id="az">—</b> g</span></div><div class="meta">orientation (x,y,z,w): <span id="quat">—</span></div></section>
<section class="card"><h2>Firmware behavior</h2><div class="value small" id="behavior">Awaiting packet</div><div class="meta">Decoded from the latest firmware telemetry payload; no Gazebo activity label is passed to firmware.</div></section>
<section class="card"><h2>Radio / logical anchor</h2><div class="value"><span id="tx">0</span><span class="unit">completed TX</span></div><div class="meta">Last TX: <span id="lasttx">—</span> s · logical anchor accepts: <span id="anchor">0</span> · RSSI: <span id="rssi">not modeled</span></div></section>
<section class="card"><h2>Power estimate</h2><div class="value"><span id="energy">pending</span><span class="unit">μAh</span></div><div class="meta">Provisional ASSUMED MCU state load + completed TX · packet battery <span id="battery">—</span></div></section>
<section class="card wide"><h2>Logical event path</h2><svg id="beam" viewBox="0 0 620 120" role="img" aria-label="Logical tag to anchor event indicator"><line id="pulse" class="beam" x1="170" y1="58" x2="450" y2="58"/><circle class="node" cx="130" cy="58" r="37"/><circle class="node" cx="490" cy="58" r="37"/><text class="nodeText" x="130" y="62" text-anchor="middle">EAR TAG</text><text class="nodeText" x="490" y="62" text-anchor="middle">ANCHOR</text></svg><div class="event" id="event">No logical TX yet.</div><div class="meta">Pulse indicates a completed logical packet accepted by the virtual anchor; it is not an RF wave or physical reception.</div></section>
<section class="card wide"><h2>Live IMU history</h2><svg id="plot" viewBox="0 0 620 120" role="img" aria-label="Recent simulated IMU samples"><path d="M0 60H620" stroke="#304553"/><polyline id="px" fill="none" stroke="#ff8c83" stroke-width="2"/><polyline id="py" fill="none" stroke="#83dfa8" stroke-width="2"/><polyline id="pz" fill="none" stroke="#83cafa" stroke-width="2"/></svg><div class="meta">Recent Gazebo samples · g · X red, Y green, Z blue</div></section>
</main><footer><span class="warn">SIMULATED</span> values only. LIS2DW12 wake comparator and current loads are approximate/assumed. Physical RF and RSSI are not asserted. Last update: <span id="updated">—</span> · run <span id="runstate">—</span></footer>
<script>
let lastTx=0;const el=id=>document.getElementById(id),fmt=(v,n=3)=>Number.isFinite(Number(v))?Number(v).toFixed(n):'—';
function plot(history){for(const [id,key] of [['px','x_g'],['py','y_g'],['pz','z_g']]){const vals=(history||[]).slice(-120).map(v=>Number(v[key]));if(vals.length<2){el(id).setAttribute('points','');continue}const max=Math.max(1,...vals.map(Math.abs));el(id).setAttribute('points',vals.map((v,i)=>`${i*620/(vals.length-1)},${60-v/max*48}`).join(' '))}}
function draw(d){el('connection').textContent='CONNECTED';el('connection').style.color=d.status==='SIMULATION_COMPLETE'?'#a7bac4':'#71dcad';el('sim').textContent=fmt(d.simulation_time_s,3);el('renode').textContent=fmt(d.renode_time_s,3);el('error').textContent=fmt(d.clock_error_s,9);el('state').textContent=d.firmware_state||'UNKNOWN';el('radio').textContent=d.radio_state||'UNKNOWN';el('tag').textContent=d.tag_id??'—';for(const [k,v] of [['ax',d.imu_g?.x],['ay',d.imu_g?.y],['az',d.imu_g?.z]])el(k).textContent=fmt(v,3);el('quat').textContent=d.orientation_xyzw?.map(v=>fmt(v,2)).join(', ')||'—';el('behavior').textContent=d.behavior_estimate||'Awaiting packet';el('tx').textContent=d.tx_count??0;el('lasttx').textContent=fmt(d.last_tx_time_s,3);el('anchor').textContent=d.anchor_accept_count??0;el('energy').textContent=d.modeled_charge_uah==null?'pending':fmt(d.modeled_charge_uah,6);el('battery').textContent=d.battery_mv==null?'unknown':`${d.battery_mv} mV (packet field)`;el('rssi').textContent=d.rssi_dbm==null?'not modeled':`${d.rssi_dbm} dBm`;el('event').textContent=d.last_anchor_event?`Logical packet ${d.last_anchor_event.sequence} accepted at ${fmt(d.last_anchor_event.timestamp_s,3)} s · tag ${d.last_anchor_event.tag_id}`:'No logical TX yet.';el('runstate').textContent=d.status||'RUNNING';el('updated').textContent=d.updated_utc||'—';plot(d.imu_history);if((d.tx_count||0)>lastTx){lastTx=d.tx_count||0;el('pulse').classList.remove('flash');void el('pulse').getBoundingClientRect();el('pulse').classList.add('flash')}}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error(r.status);draw(await r.json())}catch(_){el('connection').textContent='WAITING';el('connection').style.color='#f2c879'}}refresh();setInterval(refresh,500);
</script></body></html>"""


def initial_live_status() -> dict[str, Any]:
    return {
        "schema_version": "riose.mvp3.live_dashboard/v1",
        "status": "STARTING",
        "provenance": "SIMULATED",
        "simulation_time_s": 0.0,
        "renode_time_s": 0.0,
        "clock_error_s": 0.0,
        "firmware_state": "BOOT",
        "radio_state": "INITIALIZING",
        "tx_count": 0,
        "anchor_accept_count": 0,
        "rssi_dbm": None,
        "modeled_charge_uah": None,
        "power_status": "FINAL_MVP2_MODEL_PENDING",
        "imu_history": [],
        "last_anchor_event": None,
    }


class LiveDashboard:
    """Serve one simulation's HTML and latest JSON over loopback only."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.html_path = output_dir / "live_dashboard.html"
        self.status_path = output_dir / "live_status.json"
        self.html_path.write_text(_HTML, encoding="utf-8")
        self._lock = threading.Lock()
        self._status = initial_live_status()
        self.status_path.write_text(json.dumps(self._status, indent=2) + "\n", encoding="utf-8")
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path in ("/", "/index.html"):
                    body, content_type = _HTML.encode("utf-8"), "text/html; charset=utf-8"
                elif self.path == "/api/status":
                    with dashboard._lock:
                        body = json.dumps(dashboard._status, separators=(",", ":")).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="riose-live-dashboard", daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/"

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._status.update(values)
            encoded = json.dumps(self._status, indent=2, sort_keys=True) + "\n"
            self.status_path.write_text(encoded, encoding="utf-8")

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


__all__ = ["LiveDashboard", "initial_live_status"]
