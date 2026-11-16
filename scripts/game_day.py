"""Game day de ClaimCheck IA: 8 horas de operación simulada con tres incidentes (Tema 20).

Reloj simulado: cada minuto de operación (09:00–17:00) es un ciclo del script. En cada ciclo:
1. el generador envía el tráfico de ese minuto a las réplicas reales de la API (HTTP);
2. los "revisores" confirman los claims de 5 a 15 minutos antes (POST /retroalimentacion);
3. el monitor lee los logs, calcula SLI, drift y alertas, y el gestor dispara runbooks;
4. los runbooks en curso avanzan; las métricas van a MLflow y al tablero en tiempo real.

Escenario (el monitor no lo conoce; solo ve la telemetría):
* 10:00  INC-01  el equipo de ML despliega v2 (lote de etiquetas con error de mapeo).
* 12:00  INC-02  un cliente sube 45 min de lotes masivos sobre una instancia con CPU limitada.
* 14:00  INC-03  empiezan a llegar etiquetas de exportación en inglés (0 → 50 % en 20 min).

Uso:  python scripts/game_day.py --reiniciar      (≈ 20 min; deja MLflow en http://127.0.0.1:5000)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import mlflow  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
from mlflow import MlflowClient  # noqa: E402
from mlflow.entities import Metric  # noqa: E402

from automatizacion import registro_modelos as reg  # noqa: E402
from automatizacion.comun import (DIR_LOGS, DIR_MLFLOW, DIR_RESULTADOS, EXPERIMENTO_MONITOREO, URI_MLFLOW,  # noqa: E402
                                  iniciar_servidor_mlflow, leer_yaml)
from automatizacion.orquestador import Orquestador  # noqa: E402
from automatizacion.runbooks import MotorRunbooks  # noqa: E402
from monitoreo.drift import Referencia  # noqa: E402
from monitoreo.monitor import Monitor  # noqa: E402
from monitoreo.tablero import iniciar_tablero  # noqa: E402

INICIO = datetime(2026, 11, 7, 9, 0, tzinfo=timezone(timedelta(hours=-6)))
DURACION_MIN = 480
SEMILLA = 2026
TOKEN = "token-game-day"
COSTO_CPU_MS = 6.0                 # instancia con CPU limitada (emulada)
TRAFICO_BASE = 20                  # solicitudes /clasificar por minuto (media Poisson)
ESCENARIO = {"INC-01": 60, "INC-02": 180, "INC-03": 300}
VENTANA_S = 2.0                    # segundos reales por minuto simulado (compresión 30×)
PICO = {"inicio": 180, "fin": 225, "lotes_por_min": 20, "claims_por_lote": 50}
DRIFT = {"inicio": 300, "rampa_min": 20, "prop_max": 0.5}
APROBACION_MIN = {"RB-02": 4, "RB-03": 10}   # tiempo de respuesta simulado del on-call
CAPTURAS_TABLERO = {55: "01_normal_0955", 72: "02_inc01_deteccion_1012", 100: "03_inc01_recuperado_1040",
                    193: "04_inc02_latencia_1213", 215: "05_inc02_mitigado_1235", 328: "06_inc03_drift_1428",
                    400: "07_inc03_recuperado_1540", 479: "08_cierre_1659"}
METRICAS_MLFLOW = [
    "p95_ms_1m", "p95_ms_15m", "p50_ms_15m", "p95_lote_ms_15m", "solicitudes_por_min", "claims_por_min", "n_5xx_1m",
    "n_429_1m", "cpu_max_pct", "cpu_total_pct", "mem_max_mb", "replicas", "exactitud_30m", "recall_salud_30m",
    "confianza_media_15m", "pct_a_confirmar_15m", "psi_max_entradas_15m", "jsd_predicciones_15m",
    "prop_idioma_en_15m", "prop_oov_media_15m", "psi_n_caracteres", "psi_n_tokens", "psi_prop_oov", "psi_idioma",
    "ks_prop_oov", "burn_disponibilidad_60m", "burn_latencia_15m", "burn_latencia_3m",
    "burn_calidad_enrutamiento_30m", "burn_calidad_enrutamiento_5m", "presupuesto_restante_disponibilidad",
    "presupuesto_restante_latencia", "presupuesto_restante_calidad_enrutamiento", "alertas_activas",
    "alertas_inhibidas", "alertas_ingenuas", "mezcla_SALUD", "mezcla_MARKETING"]


def ts(t: int, s: int = 0) -> str:
    return (INICIO + timedelta(minutes=t, seconds=s)).isoformat(timespec="seconds")


class Generador:
    """Clientes del portal (tráfico) y revisores técnicos (confirmaciones con retraso)."""

    def __init__(self, orquestador: Orquestador):
        self.o = orquestador
        self.rng = random.Random(SEMILLA)
        es = pd.read_csv(RAIZ / "data" / "claims_etiquetado.csv")
        en = pd.read_csv(RAIZ / "data" / "claims_exportacion_en.csv")
        self.es = es[es.particion == "test"][["texto_claim", "categoria"]].values.tolist()
        self.en = en[en.particion == "trafico"][["texto_claim", "categoria"]].values.tolist()
        self.pendientes: list[dict] = []
        self.errores_cliente = 0
        self.local = threading.local()
        self.candado = threading.Lock()

    def _sesion(self) -> requests.Session:
        if not hasattr(self.local, "s"):
            self.local.s = requests.Session()
        return self.local.s

    def prop_ingles(self, t: int) -> float:
        if t < DRIFT["inicio"]:
            return 0.0
        return DRIFT["prop_max"] * min(1.0, (t - DRIFT["inicio"] + 1) / DRIFT["rampa_min"])

    def _claims(self, t: int, n: int) -> list[list[str]]:
        p = self.prop_ingles(t)
        return [self.rng.choice(self.en if self.rng.random() < p else self.es) for _ in range(n)]

    def _enviar(self, t: int, tipo: str, claims: list) -> None:
        rid = uuid.uuid4().hex[:12]
        cab = {"X-Request-ID": rid, "X-Reloj-Simulado": ts(t, self.rng.randint(0, 59))}
        url = self.o.siguiente_url()
        textos = [c[0] for c in claims]
        try:
            if tipo == "clasificar":
                r = self._sesion().post(f"{url}/api/v1/clasificar", json={"texto": textos[0]}, headers=cab, timeout=30)
                propuestas = [r.json()["categoria"]] if r.status_code == 200 else []
            elif tipo == "lote":
                r = self._sesion().post(f"{url}/api/v1/clasificar/lote", json={"claims": textos}, headers=cab, timeout=60)
                propuestas = [x["categoria"] for x in r.json()["resultados"]] if r.status_code == 200 else []
            else:
                r = self._sesion().post(f"{url}/api/v1/analizar-etiqueta", json={"producto": "Producto", "claims": textos},
                                        headers=cab, timeout=30)
                propuestas = [x["categoria"] for x in r.json()["resultados"]] if r.status_code == 200 else []
        except requests.RequestException:
            with self.candado:
                self.errores_cliente += 1
            return
        version = r.headers.get("X-Modelo-Version")
        with self.candado:
            for i, ((texto, real), prop) in enumerate(zip(claims, propuestas)):
                if self.rng.random() < 0.9:  # 90 % de los claims se confirman en la ventana
                    self.pendientes.append({"t": t + self.rng.randint(5, 15), "request_id": rid, "posicion": i,
                                            "texto": texto, "categoria_propuesta": prop, "categoria_confirmada": real,
                                            "version_modelo": version})

    def minuto(self, t: int, pool: ThreadPoolExecutor, pool_masivo: ThreadPoolExecutor) -> None:
        """Usuarios del portal (2 en paralelo) y, durante el pico, un cliente masivo (8 hilos)."""
        tareas = [("clasificar", self._claims(t, 1)) for _ in range(self.rng.choice([18, 19, 20, 20, 21, 22]))]
        if t % 5 == 0:
            tareas.append(("lote", self._claims(t, 8)))
        if t % 10 == 3:
            tareas.append(("etiqueta", self._claims(t, 4)))
        self.rng.shuffle(tareas)
        # Llegadas repartidas en la ventana del minuto (VENTANA_S segundos reales por minuto simulado):
        # durante el pico, los usuarios del portal compiten con un cliente masivo que no deja de enviar lotes.
        eventos = [(i / len(tareas), "i", x) for i, x in enumerate(tareas)]
        if PICO["inicio"] <= t < PICO["fin"]:
            eventos += [((j + 0.5) / PICO["lotes_por_min"], "m", ("lote", self._claims(t, PICO["claims_por_lote"])))
                        for j in range(PICO["lotes_por_min"])]
        futuros, inicio = [], time.perf_counter()
        for offset, quien, x in sorted(eventos, key=lambda e: e[0]):
            espera = inicio + offset * VENTANA_S - time.perf_counter()
            if espera > 0:
                time.sleep(espera)
            futuros.append((pool if quien == "i" else pool_masivo).submit(self._enviar, t, *x))
        for f in futuros:
            f.result()
        with self.candado:
            vencidas = [p for p in self.pendientes if p["t"] <= t]
            self.pendientes = [p for p in self.pendientes if p["t"] > t]

        def confirmar(p):
            cab = {"X-Reloj-Simulado": ts(t, self.rng.randint(0, 59))}
            try:
                self._sesion().post(f"{self.o.siguiente_url()}/api/v1/retroalimentacion", headers=cab, timeout=30,
                                    json={k: p[k] for k in ("request_id", "posicion", "texto", "categoria_propuesta",
                                                            "categoria_confirmada", "version_modelo")})
            except requests.RequestException:
                pass
        list(pool.map(confirmar, vencidas))


def preparar(reiniciar: bool) -> None:
    if reiniciar:
        os.system("pkill -f 'mlflow server' >/dev/null 2>&1; pkill -f 'uvicorn app.main' >/dev/null 2>&1")
        time.sleep(2)
        for d in (DIR_MLFLOW, DIR_LOGS, DIR_RESULTADOS, RAIZ / "data" / "etiquetas_revisor"):
            shutil.rmtree(d, ignore_errors=True)
    for d in (DIR_LOGS, DIR_RESULTADOS):
        d.mkdir(parents=True, exist_ok=True)
    iniciar_servidor_mlflow()
    mlflow.set_tracking_uri(URI_MLFLOW)
    if not (RAIZ / "data" / "claims_exportacion_en.csv").exists():
        os.system(f"{sys.executable} {RAIZ / 'scripts' / 'generar_segmento_exportacion.py'}")
    if reg.version_de_alias("champion") is None:
        os.system(f"{sys.executable} -W ignore {RAIZ / 'scripts' / 'preparar_modelos.py'}")


def main() -> None:
    args = argparse.ArgumentParser()
    args.add_argument("--reiniciar", action="store_true", help="Borra MLflow, logs y resultados antes de empezar")
    args.add_argument("--sin-capturas", action="store_true")
    args.add_argument("--minutos", type=int, default=DURACION_MIN, help="Duración (prueba corta)")
    a = args.parse_args()
    preparar(a.reiniciar)
    cfg_slos, cfg_alertas = leer_yaml("slos.yaml"), leer_yaml("alertas.yaml")
    cliente = MlflowClient()
    exp = mlflow.set_experiment(EXPERIMENTO_MONITOREO)
    run = cliente.create_run(exp.experiment_id, run_name="game-day · 2026-11-07 · 09:00–17:00",
                             tags={"tipo": "game_day", "servicio": "claimcheck-api",
                                   "mlflow.note.content": "Operación simulada de 8 h con 3 incidentes inyectados."})
    run_id = run.info.run_id
    cliente.log_batch(run_id, params=[mlflow.entities.Param(k, str(v)) for k, v in {
        "inicio": INICIO.isoformat(), "duracion_min": DURACION_MIN, "trafico_base_por_min": TRAFICO_BASE,
        "costo_cpu_ms_por_claim": COSTO_CPU_MS, "pico_lotes": json.dumps(PICO), "drift": json.dumps(DRIFT),
        "slo_disponibilidad": cfg_slos["slos"]["disponibilidad"]["objetivo"],
        "slo_latencia": f"{cfg_slos['slos']['latencia']['objetivo']} < {cfg_slos['slos']['latencia']['umbral_ms']} ms",
        "slo_calidad": cfg_slos["slos"]["calidad_enrutamiento"]["objetivo"],
        "muestreo_trazas": 0.1, "semilla": SEMILLA}.items()])
    for nombre in ("slos.yaml", "alertas.yaml"):
        cliente.log_artifact(run_id, str(RAIZ / "config" / nombre), "configuracion")

    o = Orquestador(TOKEN, costo_cpu_ms_por_claim=COSTO_CPU_MS, muestreo_trazas=0.1, hilos_trabajo=4)
    o.reloj = ts(0)
    o.escalar(1)
    referencia = Referencia.cargar(DIR_RESULTADOS / "referencias" / "referencia_v1.json")

    def registrar(t: int, ind: dict) -> None:
        ms = int((INICIO + timedelta(minutes=t)).timestamp() * 1000)
        metricas = [Metric(k, float(ind[k]), ms, t) for k in METRICAS_MLFLOW if isinstance(ind.get(k), (int, float))]
        cliente.log_batch(run_id, metrics=metricas)

    motor_rb: dict = {}
    monitor = Monitor(cfg_slos, cfg_alertas, DIR_LOGS, DIR_RESULTADOS, referencia, INICIO,
                      lanzar_runbook=lambda rb, inc, al: motor_rb["m"].lanzar(rb, inc, al),
                      runbook_activo=lambda i: motor_rb["m"].activo(i), infra=o.metricas_infra,
                      registrar_metricas=registrar)
    motor_rb["m"] = MotorRunbooks(o, monitor, ts, lambda rb, accion: APROBACION_MIN[rb], run_id,
                                  exp.experiment_id, DIR_RESULTADOS, RAIZ / "data" / "etiquetas_revisor", cliente)
    iniciar_tablero(monitor.estado_tablero, 8090)
    navegador = None
    if not a.sin_capturas:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        navegador = pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        pagina = navegador.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1.5)
        (DIR_RESULTADOS / "capturas").mkdir(exist_ok=True)

    gen = Generador(o)
    eventos_escenario = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as pool, ThreadPoolExecutor(max_workers=8) as pool_masivo:
        for t in range(a.minutos):
            o.reloj = ts(t)
            motor_rb["m"].t = t
            if t == ESCENARIO["INC-01"]:
                reg.mover_alias("champion", reg.version_de_alias("challenger"), "Despliegue planificado de v2 (1.1.0)")
                o.recargar_modelo()
                eventos_escenario.append({"t": t, "incidente": "INC-01", "falla": "Despliegue de v2 (1.1.0) con "
                                          "etiquetas SALUD mal mapeadas a MARKETING"})
            if t == ESCENARIO["INC-02"]:
                eventos_escenario.append({"t": t, "incidente": "INC-02", "falla": f"Pico de lotes: "
                                          f"{PICO['lotes_por_min']} lotes × {PICO['claims_por_lote']} claims/min durante "
                                          f"{PICO['fin'] - PICO['inicio']} min (CPU emulada {COSTO_CPU_MS} ms/claim)"})
            if t == ESCENARIO["INC-03"]:
                eventos_escenario.append({"t": t, "incidente": "INC-03", "falla": "Nuevo segmento: claims en inglés "
                                          f"(0 → {DRIFT['prop_max']:.0%} del tráfico en {DRIFT['rampa_min']} min)"})
            gen.minuto(t, pool, pool_masivo)
            ind = monitor.tick(t)
            motor_rb["m"].avanzar(t)
            if t % 30 == 0:
                print(f"{ts(t)[11:16]}  p95={ind.get('p95_ms_15m') or 0:6.0f} ms  exact30={ind.get('exactitud_30m') or 0:.3f}"
                      f"  recSALUD={ind.get('recall_salud_30m') or 0:.2f}  PSI={ind.get('psi_max_entradas_15m') or 0:.2f}"
                      f"  JSD={ind.get('jsd_predicciones_15m') or 0:.3f}  CPU={ind['cpu_max_pct']:.0f}%  réplicas={ind['replicas']}"
                      f"  alertas={ind['alertas_activas']}  ({time.time() - t0:.0f} s)", flush=True)
            if navegador and t in CAPTURAS_TABLERO:
                pagina.goto("http://127.0.0.1:8090/")
                pagina.wait_for_timeout(1800)
                pagina.screenshot(path=str(DIR_RESULTADOS / "capturas" / f"tablero_{CAPTURAS_TABLERO[t]}.png"),
                                  full_page=True)

    # ── Cierre del game day ─────────────────────────────────────────────────────
    time.sleep(3)
    pd.DataFrame([{k: v for k, v in f.items() if not isinstance(v, dict)} for f in monitor.filas]).to_csv(
        DIR_RESULTADOS / "indicadores_por_minuto.csv", index=False)
    incidentes = [{"id": i.id, "grupo": i.grupo, "severidad": i.severidad, "abierto_t": i.abierto_t,
                   "notificado_t": i.notificado_t, "resuelto_t": i.resuelto_t, "estado": i.estado,
                   "alertas": i.alertas, "runbooks": i.runbooks, "causa_probable": i.causa_probable}
                  for i in monitor.gestor.incidentes]
    ejecuciones = [{k: v for k, v in e.__dict__.items() if k != "gen"} for e in motor_rb["m"].ejecuciones.values()]
    salida = {"inicio": INICIO.isoformat(), "duracion_min": DURACION_MIN, "escenario": eventos_escenario,
              "incidentes": incidentes, "runbooks": ejecuciones,
              "notificaciones_inteligentes": monitor.notificador.enviadas,
              "disparos_ingenuos": monitor.ingenuo.disparos, "inhibidas": monitor.gestor.inhibidas_t,
              "umbrales_dinamicos": monitor.motor.umbrales_aprendidos, "despliegues": monitor.despliegues,
              "referencias": monitor.historial_referencias, "errores_cliente": gen.errores_cliente,
              "segundos_reales": round(time.time() - t0, 1), "run_id_mlflow": run_id}
    (DIR_RESULTADOS / "game_day.json").write_text(json.dumps(salida, ensure_ascii=False, indent=2, default=str),
                                                 encoding="utf-8")
    for ruta in ["game_day.json", "indicadores_por_minuto.csv", "alertas.jsonl", "notificaciones.jsonl"]:
        cliente.log_artifact(run_id, str(DIR_RESULTADOS / ruta), "resultados")
    for ruta in sorted((DIR_RESULTADOS / "capturas").glob("tablero_*.png")):
        cliente.log_artifact(run_id, str(ruta), "tablero")
    for ruta in sorted((DIR_RESULTADOS / "referencias").glob("*.json")):
        cliente.log_artifact(run_id, str(ruta), "referencias_drift")
    cliente.set_terminated(run_id, "FINISHED")
    if navegador:
        navegador.close()
        pw.stop()
    o.detener_todo()
    print(f"\nGame day terminado en {time.time() - t0:.0f} s · {len(incidentes)} incidentes · "
          f"{len(monitor.notificador.enviadas)} notificaciones · {len(monitor.ingenuo.disparos)} disparos ingenuos")


if __name__ == "__main__":
    main()
