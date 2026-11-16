"""Capturas de evidencia: interfaz de MLflow (experimentos, métricas, trazas, registro) y API instrumentada.

Requiere el servidor de MLflow en marcha (lo deja scripts/game_day.py) y Google Chrome.
Uso:  python scripts/capturas_evidencia.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import mlflow  # noqa: E402
import requests  # noqa: E402
from mlflow import MlflowClient  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from automatizacion.comun import (DIR_RESULTADOS, EXPERIMENTO_ENTRENAMIENTO, EXPERIMENTO_MONITOREO,  # noqa: E402
                                  URI_MLFLOW, iniciar_servidor_mlflow)
from automatizacion.orquestador import Orquestador  # noqa: E402

SALIDA = DIR_RESULTADOS / "capturas"
UI = URI_MLFLOW


def capturar(pagina, url: str, nombre: str, espera_ms: int = 3500, selector: str | None = None,
             alto: int | None = None, acciones=None, pagina_completa: bool = False) -> None:
    pagina.goto(url)
    pagina.wait_for_load_state("domcontentloaded")
    if selector:
        try:
            pagina.wait_for_selector(selector, timeout=15000)
        except Exception:  # noqa: BLE001
            pass
    pagina.wait_for_timeout(espera_ms)
    _cerrar_asistente(pagina)
    _clic_texto(pagina, "Got it")
    if acciones:
        acciones(pagina)
        pagina.wait_for_timeout(1500)
    opciones = {"path": str(SALIDA / f"{nombre}.png"), "full_page": pagina_completa}
    if alto:
        opciones["clip"] = {"x": 0, "y": 0, "width": pagina.viewport_size["width"], "height": alto}
    pagina.screenshot(**opciones)
    print("captura", nombre)


def recortar(ruta: Path, margen: int = 24) -> None:
    """Quita el espacio vacío inferior: última fila con contenido (desviación de color) + margen."""
    from PIL import Image, ImageStat
    im = Image.open(ruta).convert("RGB")
    w, h = im.size
    x0, x1 = int(w * 0.16), int(w * 0.97)
    ultima = 0
    for y in range(0, h - 30, 4):
        fila = im.crop((x0, y, x1, y + 4))
        if max(ImageStat.Stat(fila).stddev) > 6:
            ultima = y + 4
    if 0 < ultima < h - 60:
        im.crop((0, 0, w, min(h, ultima + margen))).save(ruta.with_name(ruta.stem + "_recorte.png"))


def recortar_todo() -> None:
    for ruta in sorted(SALIDA.glob("*.png")):
        if not ruta.stem.endswith("_recorte"):
            recortar(ruta)


def _clic_texto(pagina, texto: str) -> None:
    try:
        pagina.get_by_text(texto, exact=True).first.click(timeout=3000)
        pagina.wait_for_timeout(1500)
    except Exception:  # noqa: BLE001
        pass


def _cerrar_asistente(pagina) -> None:
    try:
        boton = pagina.get_by_label("Close").first
        if boton.is_visible(timeout=1500):
            boton.click(timeout=2000)
            pagina.wait_for_timeout(800)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    SALIDA.mkdir(parents=True, exist_ok=True)
    iniciar_servidor_mlflow()
    mlflow.set_tracking_uri(URI_MLFLOW)
    c = MlflowClient()
    exp_m = c.get_experiment_by_name(EXPERIMENTO_MONITOREO)
    exp_e = c.get_experiment_by_name(EXPERIMENTO_ENTRENAMIENTO)
    runs = c.search_runs([exp_m.experiment_id], order_by=["start_time ASC"])
    game_day = next(r for r in runs if r.data.tags.get("tipo") == "game_day")
    runbooks = [r for r in runs if r.data.tags.get("tipo") == "runbook"]
    gd = json.loads((DIR_RESULTADOS / "game_day.json").read_text(encoding="utf-8"))

    with sync_playwright() as pw:
        nav = pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        pagina = nav.new_page(viewport={"width": 1600, "height": 950}, device_scale_factor=1.5)
        e, rid = exp_m.experiment_id, game_day.info.run_id
        # Vista "Model training" (ML clásico) y sin el panel del asistente; el estado queda en el navegador.
        pagina.goto(f"{UI}/#/experiments")
        pagina.wait_for_timeout(4000)
        _clic_texto(pagina, "Model training")
        _cerrar_asistente(pagina)
        capturar(pagina, f"{UI}/#/experiments", "mlflow_01_experimentos", alto=420)

        def expandir(p):
            celda = p.get_by_text("game-day", exact=False).first
            caja = celda.bounding_box()
            if caja:
                p.mouse.click(caja["x"] - 26, caja["y"] + caja["height"] / 2)

        capturar(pagina, f"{UI}/#/experiments/{e}/runs", "mlflow_02_runs_monitoreo", acciones=expandir, alto=470)
        capturar(pagina, f"{UI}/#/experiments/{e}/runs/{rid}", "mlflow_03_run_game_day")
        for nombre, filtro in [("latencia", "p95"), ("burn", "burn"), ("drift", "psi"), ("calidad", "exactitud"),
                               ("operacion", "replicas")]:
            def buscar(p, filtro=filtro):
                caja = p.get_by_placeholder("Search metric charts").first
                caja.fill(filtro)
            capturar(pagina, f"{UI}/#/experiments/{e}/runs/{rid}/model-metrics",
                     f"mlflow_04_metricas_{nombre}", espera_ms=4000, acciones=buscar)

        def abrir(*rutas):
            def accion(p):
                for r in rutas:
                    p.get_by_text(r, exact=True).first.click()
                    p.wait_for_timeout(1500)
            return accion

        capturar(pagina, f"{UI}/#/experiments/{e}/runs/{rid}/artifacts", "mlflow_05_artefactos_game_day",
                 acciones=abrir("resultados", "notificaciones.jsonl"))
        for r in runbooks:
            rb = r.data.tags.get("runbook", "RB")
            capturar(pagina, f"{UI}/#/experiments/{e}/runs/{r.info.run_id}", f"mlflow_06_{rb}_run")
            md = f"{rb}_{r.data.tags.get('mlflow.runName', '')}"
            archivo = next((a.path.split("/")[-1] for a in c.list_artifacts(r.info.run_id, "bitacora")
                            if a.path.endswith(".md")), None)
            capturar(pagina, f"{UI}/#/experiments/{e}/runs/{r.info.run_id}/artifacts", f"mlflow_07_{rb}_bitacora",
                     acciones=abrir("bitacora", archivo) if archivo else None)

        def traza(p):
            filas = p.query_selector_all("[role=row]")
            if len(filas) > 4:
                filas[4].click()
                p.wait_for_timeout(2500)
                _clic_texto(p, "POST /api/v1/clasificar")
        capturar(pagina, f"{UI}/#/experiments/{e}/traces", "mlflow_08_trazas", espera_ms=5000)
        capturar(pagina, f"{UI}/#/experiments/{e}/traces", "mlflow_08_traza_detalle", espera_ms=5000, acciones=traza)
        capturar(pagina, f"{UI}/#/experiments/{exp_e.experiment_id}/runs", "mlflow_09_runs_entrenamiento", alto=470)
        pagina.set_viewport_size({"width": 1600, "height": 2000})
        capturar(pagina, f"{UI}/#/models/claimcheck_router", "mlflow_10_registro_modelo")
        pagina.set_viewport_size({"width": 1600, "height": 950})
        for v in ("1", "2", "3"):
            capturar(pagina, f"{UI}/#/models/claimcheck_router/versions/{v}", f"mlflow_11_version_{v}")
        nav.close()

    # API instrumentada: /metrics y documentación OpenAPI con los endpoints nuevos.
    o = Orquestador("token-capturas", puerto_base=8191, prefijo="captura", trazas=False)
    o.reloj = None
    o.escalar(1)
    url = o.siguiente_url()
    for texto in ["Sin gluten", "Gluten free", "El calcio ayuda a mantener huesos fuertes", "Receta tradicional"]:
        requests.post(f"{url}/api/v1/clasificar", json={"texto": texto})
    (SALIDA / "metrics_api.txt").write_text(requests.get(f"{url}/metrics").text, encoding="utf-8")
    # La página /docs de FastAPI carga Swagger UI desde un CDN; sin red externa se exporta la lista de endpoints.
    api = requests.get(f"{url}/openapi.json").json()
    endpoints = [[m.upper(), ruta, (op.get("tags") or [""])[0], op.get("summary", "")]
                 for ruta, ops in api["paths"].items() for m, op in ops.items()]
    (SALIDA / "endpoints.json").write_text(json.dumps(endpoints, ensure_ascii=False, indent=1), encoding="utf-8")
    o.detener_todo()
    recortar_todo()
    print("listo", time.strftime("%H:%M:%S"), gd["run_id_mlflow"])


if __name__ == "__main__":
    main()
