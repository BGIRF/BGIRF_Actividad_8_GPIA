# ClaimCheck IA · Actividad 8 — Monitorización proactiva y respuesta a incidentes

**Gestión de Proyectos de Inteligencia Artificial · Maestría en IA (Tecmilenio)**
Alumno: Brandon Gilberto Imanol Retana Fuentes (AL07175444)

Continúa la Fase 2 ([BGIRF_Proyecto_Fase2_GPIA](https://github.com/BGIRF/BGIRF_Proyecto_Fase2_GPIA)): la API de
preclasificación de claims de etiquetado (FastAPI + TF-IDF/regresión logística) ahora se observa con métricas,
logs y trazas, se vigila contra SLO con alertas inteligentes y responde a incidentes con runbooks automatizados.
Todo se validó en un *game day* de 8 h simuladas con tres incidentes reales sobre el servicio.

## Entregables

| # | Entregable | Archivo |
|---|---|---|
| 1 | Documento técnico (indicadores, alertas, tablero, runbooks, drift, respuesta automatizada) | [`docs/BGIRF_Actividad_8_Documento_Tecnico.pdf`](docs/BGIRF_Actividad_8_Documento_Tecnico.pdf) |
| 2 | Evidencia de implementación (MLflow, métricas, alertas, tablero) | [`docs/BGIRF_Actividad_8_Evidencia_implementacion.pdf`](docs/BGIRF_Actividad_8_Evidencia_implementacion.pdf) y [`.pptx`](docs/BGIRF_Actividad_8_Evidencia_implementacion.pptx) |
| 3 | Simulación de incidentes (registro, runbooks ejecutados, resultados) | [`docs/BGIRF_Actividad_8_Simulacion_incidentes.pdf`](docs/BGIRF_Actividad_8_Simulacion_incidentes.pdf) |
| 4 | Código y configuración | este repositorio (`app/`, `monitoreo/`, `automatizacion/`, `config/`, `scripts/`) |

## Estructura

```
app/                 API v1.1: observabilidad.py (métricas, logs JSON, trazas), caos.py (fallas y controles), main.py
monitoreo/           sli.py (SLI, burn rate, presupuesto) · drift.py (PSI, KS, JSD, χ²) · alertas.py (motor + gestor)
                     colector.py · monitor.py (ciclo por minuto) · tablero.py (tablero en tiempo real)
automatizacion/      runbooks.py (RB-01..RB-03 ejecutables) · reentrenamiento.py (compuerta) · registro_modelos.py
                     (MLflow Model Registry y alias) · orquestador.py (réplicas y escalamiento)
config/              slos.yaml · alertas.yaml · prometheus/ (reglas, pruebas de reglas y Alertmanager equivalentes)
runbooks/            RB-01 rollback · RB-02 latencia · RB-03 data drift · RB-04 disponibilidad
scripts/             game_day.py · analizar_game_day.py · capturas_evidencia.py · preparar_modelos.py
                     generar_segmento_exportacion.py
docs/                entregables finales (PDF y PPTX) y diagrama de arquitectura (docs/diagramas/)
resultados/          indicadores por minuto, alertas, notificaciones, bitácoras de runbooks, figuras y capturas
logs/                logs JSON de las réplicas durante el game day (evidencia de los tres pilares)
mlflow_store.zip     base de MLflow del game day (SQLite + artefactos)
tests/               84 pruebas de la Fase 2 + 20 de monitoreo e instrumentación
```

## Cómo reproducir

```bash
pip install -r requirements.txt
python -m pytest -q                         # 104 pruebas
python scripts/game_day.py --reiniciar      # ~20 min: levanta MLflow, registra v1/v2, corre 8 h simuladas
python scripts/analizar_game_day.py         # MTTD/TTR, comparativa de ruido y figuras
python scripts/capturas_evidencia.py        # capturas de MLflow y de la API (requiere Chrome)
mlflow ui --backend-store-uri sqlite:///mlflow/mlflow.db   # o el servidor que deja game_day.py en :5000
```

Durante el game day el tablero operativo está en `http://127.0.0.1:8090` y MLflow en `http://127.0.0.1:5000`.

**Ver el MLflow del game day sin volver a correrlo.** La base de MLflow (runs, métricas por minuto, runbooks, trazas y
Model Registry) viene comprimida en `mlflow_store.zip`:

```bash
unzip mlflow_store.zip            # crea mlflow/mlflow.db y mlflow/artefactos/
mlflow server --backend-store-uri sqlite:///mlflow/mlflow.db --artifacts-destination mlflow/artefactos --port 5000
```

Validar la configuración portable de Prometheus/Alertmanager:

```bash
cd config/prometheus
promtool check rules reglas_claimcheck.yml && promtool test rules pruebas_reglas.yml
amtool check-config alertmanager.yml
```

## Variables nuevas de la API (además de las de la Fase 2)

| Variable | Uso |
|---|---|
| `MODELO_URI` | `models:/claimcheck_router@champion` para cargar del Model Registry; vacío = artefacto local de la Fase 2 |
| `LOG_JSON_PATH` / `ETIQUETAS_PATH` | Log de observabilidad (sin texto del claim) y almacén de etiquetas del revisor |
| `TRAZAS_MLFLOW`, `MLFLOW_TRACE_SAMPLING_RATIO` | Trazas en MLflow Tracing y su muestreo |
| `ADMIN_TOKEN` | Habilita `/admin/modelo/recargar` y `/admin/caos`; vacío = deshabilitados (404) |
| `HILOS_TRABAJO`, `COSTO_CPU_MS_POR_CLAIM` | Emulan una instancia pequeña en el game day |
| `RELOJ_SIMULADO` | Acepta la cabecera `X-Reloj-Simulado` del generador de tráfico |

## Limitaciones

Reloj simulado (8 h en ~20 min), CPU limitada emulada y aprobaciones humanas simuladas (4 y 10 min). Las réplicas
son procesos locales y las notificaciones se escriben en `resultados/notificaciones.jsonl` (en producción: Render o
Cloud Run y webhooks de PagerDuty/Slack). El segmento en inglés es sintético. Organismo certificador y datos ficticios.

Licencia MIT.
