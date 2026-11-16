"""Tablero operativo en tiempo real (Tema 19, "Tablero de control operativo").

Servidor ligero que publica el estado del monitor en /api/estado; la página se actualiza sola
cada 2 s. Sigue los principios del tema: información crítica arriba, colores semafóricos,
métricas agrupadas por contexto (servicio, modelo, datos, alertas) y solo indicadores accionables.
"""

from __future__ import annotations

import threading
from typing import Callable

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>ClaimCheck IA · Tablero operativo</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--fondo:#0f1720;--panel:#17212c;--borde:#26323f;--texto:#e6edf3;--tenue:#8b9bab;--verde:#2fbf71;
--ambar:#f2b134;--rojo:#ef5350;--azul:#4ea1ff;--morado:#b48cff;--cian:#3fd0c9}
*{box-sizing:border-box}body{margin:0;background:var(--fondo);color:var(--texto);
font:13px/1.35 "Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{display:flex;align-items:center;gap:18px;padding:12px 18px;border-bottom:1px solid var(--borde);background:#121b25}
header h1{font-size:17px;margin:0;font-weight:600}header .sub{color:var(--tenue)}
.pill{padding:3px 10px;border-radius:999px;font-weight:600;font-size:12px;border:1px solid var(--borde)}
.ok{background:rgba(47,191,113,.15);color:var(--verde);border-color:rgba(47,191,113,.4)}
.warn{background:rgba(242,177,52,.15);color:var(--ambar);border-color:rgba(242,177,52,.4)}
.crit{background:rgba(239,83,80,.16);color:var(--rojo);border-color:rgba(239,83,80,.45)}
.der{margin-left:auto;display:flex;gap:10px;align-items:center}
main{padding:14px 18px;display:grid;grid-template-columns:1fr 360px;gap:14px}
.tiles{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:12px}
.tile{background:var(--panel);border:1px solid var(--borde);border-left:5px solid var(--verde);border-radius:8px;padding:10px 12px}
.tile.warn{border-left-color:var(--ambar)}.tile.crit{border-left-color:var(--rojo)}
.tile .t{color:var(--tenue);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.tile .v{font-size:24px;font-weight:700;margin:2px 0}.tile .d{color:var(--tenue);font-size:11.5px}
.bar{height:5px;background:#26323f;border-radius:3px;margin-top:6px;overflow:hidden}.bar i{display:block;height:100%;background:var(--verde)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.panel{background:var(--panel);border:1px solid var(--borde);border-radius:8px;padding:10px 12px}
.panel h3{margin:0 0 6px;font-size:12.5px;font-weight:600;display:flex;justify-content:space-between}
.panel h3 span{color:var(--tenue);font-weight:400}
svg{width:100%;height:150px;display:block}.ley{display:flex;gap:10px;color:var(--tenue);font-size:11px;flex-wrap:wrap}
.ley b{display:inline-block;width:10px;height:3px;margin-right:4px;vertical-align:middle}
table{width:100%;border-collapse:collapse}td,th{padding:5px 4px;border-bottom:1px solid var(--borde);text-align:left;vertical-align:top}
th{color:var(--tenue);font-weight:500;font-size:11px}.sev{font-weight:700;font-size:11px;padding:1px 6px;border-radius:4px}
.P1{background:var(--rojo);color:#fff}.P2{background:#e67e22;color:#fff}.P3{background:var(--ambar);color:#222}.INFO{background:#33475b;color:#cfe}
.lado{display:flex;flex-direction:column;gap:10px}.vacio{color:var(--tenue);font-style:italic;padding:6px 0}
.feed div{padding:5px 0;border-bottom:1px solid var(--borde)}.feed small{color:var(--tenue)}
.mezcla{display:flex;height:16px;border-radius:4px;overflow:hidden;margin:4px 0}.mezcla i{display:block}
</style></head><body>
<header><h1>ClaimCheck IA · Tablero operativo</h1><span class="sub">CertiBajío · claimcheck-api · actualización cada 2 s</span>
<div class="der"><span class="sub" id="hora">—</span><span class="pill" id="modelo">modelo —</span>
<span class="pill" id="replicas">réplicas —</span><span class="pill ok" id="estado">—</span></div></header>
<main><section>
<div class="tiles" id="tiles"></div>
<div class="grid">
 <div class="panel"><h3>Latencia /clasificar <span>SLO: 95 % &lt; 300 ms</span></h3><svg id="g_lat"></svg>
  <div class="ley"><span><b style="background:var(--azul)"></b>p95 1 min</span><span><b style="background:var(--morado)"></b>p95 15 min</span><span><b style="background:var(--rojo)"></b>300 ms</span></div></div>
 <div class="panel"><h3>Tráfico y CPU <span>golden signals</span></h3><svg id="g_traf"></svg>
  <div class="ley"><span><b style="background:var(--cian)"></b>claims/min</span><span><b style="background:var(--ambar)"></b>CPU máx. %</span></div></div>
 <div class="panel"><h3>Calidad confirmada por el revisor <span>ventana 30 min</span></h3><svg id="g_cal"></svg>
  <div class="ley"><span><b style="background:var(--verde)"></b>exactitud</span><span><b style="background:var(--morado)"></b>recall SALUD</span><span><b style="background:var(--rojo)"></b>SLO 0.80 / mín. 0.70</span></div></div>
 <div class="panel"><h3>Data drift y predicciones <span>vs referencia, 15 min</span></h3><svg id="g_drift"></svg>
  <div class="ley"><span><b style="background:var(--rojo)"></b>PSI máx. entradas</span><span><b style="background:var(--cian)"></b>JSD predicciones ×5</span><span><b style="background:var(--ambar)"></b>umbral 0.20</span></div></div>
 <div class="panel"><h3>Burn rate del presupuesto de error <span>1× = agota en 28 días</span></h3><svg id="g_burn"></svg>
  <div class="ley"><span><b style="background:var(--azul)"></b>latencia (15 min)</span><span><b style="background:var(--verde)"></b>calidad (30 min)</span><span><b style="background:var(--ambar)"></b>1×</span></div></div>
 <div class="panel"><h3>Señales del modelo sin etiquetas <span>15 min</span></h3><svg id="g_conf"></svg>
  <div class="ley"><span><b style="background:var(--verde)"></b>confianza media</span><span><b style="background:var(--ambar)"></b>% a confirmar</span><span><b style="background:var(--rojo)"></b>prop. OOV</span></div></div>
</div></section>
<aside class="lado">
 <div class="panel"><h3>Alertas activas <span id="n_al"></span></h3><div id="alertas"></div></div>
 <div class="panel"><h3>Incidentes <span>hoy</span></h3><div id="incidentes"></div></div>
 <div class="panel"><h3>#claimcheck-incidentes <span>notificaciones</span></h3><div class="feed" id="feed"></div></div>
 <div class="panel"><h3>Mezcla de predicciones <span>ref. vs actual</span></h3><div id="mezcla"></div>
  <div class="ley"><span><b style="background:#4ea1ff"></b>AUSENCIA</span><span><b style="background:#f2b134"></b>MARKETING</span><span><b style="background:#2fbf71"></b>NUTRIMENTAL</span><span><b style="background:#ef5350"></b>SALUD</span></div></div>
 <div class="panel"><h3>Eventos operativos</h3><div class="feed" id="eventos"></div></div>
</aside></main>
<script>
const C={azul:'#4ea1ff',morado:'#b48cff',rojo:'#ef5350',verde:'#2fbf71',ambar:'#f2b134',cian:'#3fd0c9'};
const hh=t=>{const m=540+t;return String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0')};
function grafica(id,lineas,opt){const s=document.getElementById(id),W=s.clientWidth||400,H=150,pl=34,pb=18,pt=6;
 let xs=[],ys=[];lineas.forEach(l=>l.d.forEach(([x,y])=>{if(y!=null){xs.push(x);ys.push(y)}}));
 const xmax=Math.max(opt.xmax||0,...xs,60),xmin=0;let ymax=opt.ymax!=null?Math.max(opt.ymax,...ys.map(v=>v*1.08)):Math.max(...ys,1)*1.1;
 if(opt.ytope)ymax=Math.min(ymax,opt.ytope);const ymin=opt.ymin||0;
 const X=x=>pl+(x-xmin)/(xmax-xmin)*(W-pl-6),Y=y=>pt+(1-(Math.min(y,ymax)-ymin)/(ymax-ymin))*(H-pt-pb);
 let g='';for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4,y=Y(v);g+=`<line x1="${pl}" x2="${W-6}" y1="${y}" y2="${y}" stroke="#26323f"/><text x="${pl-4}" y="${y+3}" fill="#8b9bab" font-size="9.5" text-anchor="end">${opt.fmt?opt.fmt(v):(v>=100?v.toFixed(0):v.toFixed(2))}</text>`}
 for(let t=0;t<=xmax;t+=60){g+=`<text x="${X(t)}" y="${H-4}" fill="#8b9bab" font-size="9.5" text-anchor="middle">${hh(t)}</text>`}
 (opt.marcas||[]).forEach(m=>{g+=`<line x1="${X(m.t)}" x2="${X(m.t)}" y1="${pt}" y2="${H-pb}" stroke="#5c6b7a" stroke-dasharray="2,3"/>`});
 (opt.umbrales||[]).forEach(u=>{g+=`<line x1="${pl}" x2="${W-6}" y1="${Y(u.v)}" y2="${Y(u.v)}" stroke="${u.c}" stroke-dasharray="5,4" stroke-width="1.2"/>`});
 lineas.forEach(l=>{let p='',on=false;l.d.forEach(([x,y])=>{if(y==null){on=false;return}p+=(on?'L':'M')+X(x).toFixed(1)+','+Y(l.f?l.f(y):y).toFixed(1)+' ';on=true});g+=`<path d="${p}" fill="none" stroke="${l.c}" stroke-width="${l.w||1.6}"/>`});
 s.setAttribute('viewBox',`0 0 ${W} ${H}`);s.innerHTML=g}
function clase(v,warn,crit,menor){if(v==null)return'';return menor?(v<crit?'crit':v<warn?'warn':'ok'):(v>=crit?'crit':v>=warn?'warn':'ok')}
function tile(t,v,d,c,pres){return`<div class="tile ${c}"><div class="t">${t}</div><div class="v">${v}</div><div class="d">${d}</div>${pres!=null?`<div class="bar"><i style="width:${Math.max(0,pres*100).toFixed(1)}%;background:${pres<.5?C.rojo:pres<.8?C.ambar:C.verde}"></i></div><div class="d">presupuesto restante ${(pres*100).toFixed(1)} %</div>`:''}</div>`}
const f=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
async function ciclo(){let e;try{e=await (await fetch('/api/estado')).json()}catch(x){return}
 const u=e.ultimo||{},xmax=e.t||0;document.getElementById('hora').textContent='Hora simulada '+(e.ahora||'').slice(11,16)+' · 7 nov 2026';
 document.getElementById('modelo').textContent='modelo '+(u.version_modelo||'—');document.getElementById('replicas').textContent=(u.replicas||0)+' réplica(s)';
 const peor=(e.activas||[]).reduce((a,x)=>Math.min(a,{P1:1,P2:2,P3:3,INFO:4}[x.severidad]),9);
 const est=document.getElementById('estado');est.className='pill '+(peor<=1?'crit':peor<=3?'warn':'ok');est.textContent=peor<=1?'Crítico':peor<=2?'Degradado':peor<=3?'Advertencia':'Operando normal';
 const cl=clase(u.p95_ms_15m,250,300),cc=clase(u.exactitud_30m,.82,.80,true),cs=clase(u.recall_salud_30m,.75,.70,true),cd=clase(u.psi_max_entradas_15m,.10,.20),ca=clase(u.burn_disponibilidad_60m,1,14.4);
 document.getElementById('tiles').innerHTML=
  tile('Disponibilidad (60 min)',u.sli_disponibilidad_60m==null?'—':(u.sli_disponibilidad_60m*100).toFixed(2)+' %','SLO 99.5 % · burn '+f(u.burn_disponibilidad_60m)+'×',ca,u.presupuesto_restante_disponibilidad)+
  tile('Latencia p95 (15 min)',f(u.p95_ms_15m,0)+' ms','SLO 95 % < 300 ms · burn '+f(u.burn_latencia_15m)+'×',cl,u.presupuesto_restante_latencia)+
  tile('Calidad (30 min)',f(u.exactitud_30m),'SLO ≥ 0.80 confirmada · burn '+f(u.burn_calidad_enrutamiento_30m)+'×',cc,u.presupuesto_restante_calidad_enrutamiento)+
  tile('Recall SALUD (30 min)',f(u.recall_salud_30m),'guardarraíl ≥ 0.70 · n = '+(u.recall_salud_30m_n||0),cs)+
  tile('Data drift (PSI máx.)',f(u.psi_max_entradas_15m),(u.psi_max_entradas_15m_detalle||'—')+' · JSD pred. '+f(u.jsd_predicciones_15m,3),cd);
 const S=e.series,mar=(e.eventos||[]).map(x=>({t:x.t}));
 grafica('g_lat',[{d:S.p95_ms_1m,c:C.azul,w:1.1},{d:S.p95_ms_15m,c:C.morado,w:2}],{umbrales:[{v:300,c:C.rojo}],ymax:400,xmax,marcas:mar,fmt:v=>v.toFixed(0)});
 grafica('g_traf',[{d:S.claims_por_min,c:C.cian},{d:S.cpu_max_pct,c:C.ambar}],{xmax,marcas:mar,fmt:v=>v.toFixed(0)});
 grafica('g_cal',[{d:S.exactitud_30m,c:C.verde,w:2},{d:S.recall_salud_30m,c:C.morado,w:2}],{umbrales:[{v:.8,c:C.rojo},{v:.7,c:'#a33'}],ymin:0,ymax:1,ytope:1,xmax,marcas:mar});
 grafica('g_drift',[{d:S.psi_max_entradas_15m,c:C.rojo,w:2},{d:S.jsd_predicciones_15m,c:C.cian,f:v=>v*5}],{umbrales:[{v:.2,c:C.ambar}],ymax:.5,xmax,marcas:mar});
 grafica('g_burn',[{d:S.burn_latencia_15m,c:C.azul,w:2},{d:S.burn_calidad_enrutamiento_30m,c:C.verde,w:2}],{umbrales:[{v:1,c:C.ambar}],ymax:3,xmax,marcas:mar});
 grafica('g_conf',[{d:S.confianza_media_15m,c:C.verde,w:2},{d:S.pct_a_confirmar_15m,c:C.ambar,w:2},{d:S.prop_oov_media_15m,c:C.rojo,w:2}],{ymin:0,ymax:1,ytope:1,xmax,marcas:mar});
 const al=e.activas||[];document.getElementById('n_al').textContent=al.length?al.length+' activa(s)':'';
 document.getElementById('alertas').innerHTML=al.length?'<table><tr><th>Sev.</th><th>Alerta</th><th>Desde</th></tr>'+al.map(a=>`<tr><td><span class="sev ${a.severidad}">${a.severidad}</span></td><td><b>${a.nombre}</b><br><small style="color:#8b9bab">${a.resumen}<br>runbook: ${a.runbook}</small></td><td>${a.desde}</td></tr>`).join('')+'</table>':'<div class="vacio">Sin alertas activas: todos los SLO dentro de objetivo.</div>';
 const inc=e.incidentes||[];document.getElementById('incidentes').innerHTML=inc.length?'<table><tr><th>ID</th><th>Sev.</th><th>Estado</th><th>Abierto</th><th>Cerrado</th></tr>'+inc.map(i=>`<tr><td>${i.id}</td><td><span class="sev ${i.severidad}">${i.severidad}</span></td><td>${i.estado}${i.runbooks.length?'<br><small style="color:#8b9bab">'+i.runbooks.join(', ')+'</small>':''}</td><td>${i.abierto}</td><td>${i.resuelto||'—'}</td></tr>`).join('')+'</table>':'<div class="vacio">Sin incidentes.</div>';
 document.getElementById('feed').innerHTML=(e.notificaciones||[]).map(n=>`<div><span class="sev ${n.severidad}">${n.severidad}</span> <small>${n.hora} · ${n.tipo}</small><br>${n.titulo}</div>`).join('')||'<div class="vacio">Sin notificaciones.</div>';
 document.getElementById('eventos').innerHTML=(e.eventos||[]).map(x=>`<div><small>${x.hora}</small> ${x.texto}</div>`).join('')||'<div class="vacio">—</div>';
 const m=e.mezcla||{},col={AUSENCIA:'#4ea1ff',MARKETING:'#f2b134',NUTRIMENTAL:'#2fbf71',SALUD:'#ef5350'};
 const barra=o=>'<div class="mezcla">'+Object.entries(o||{}).map(([k,v])=>`<i style="width:${v*100}%;background:${col[k]}" title="${k} ${(v*100).toFixed(0)}%"></i>`).join('')+'</div>';
 document.getElementById('mezcla').innerHTML='<small style="color:#8b9bab">Referencia ('+e.referencia+')</small>'+barra(m.mezcla_ref)+'<small style="color:#8b9bab">Producción, últimos 15 min</small>'+barra(m.mezcla_act);}
ciclo();setInterval(ciclo,2000);
</script></body></html>"""


def iniciar_tablero(estado: Callable[[], dict], puerto: int = 8090) -> threading.Thread:
    app = FastAPI(title="ClaimCheck IA · tablero")

    @app.get("/", response_class=HTMLResponse)
    def inicio():
        return HTML

    @app.get("/api/estado")
    def api_estado():
        return JSONResponse(estado())

    servidor = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=puerto, log_level="warning"))
    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()
    return hilo
