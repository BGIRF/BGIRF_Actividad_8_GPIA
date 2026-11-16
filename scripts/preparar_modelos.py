"""Registra en MLflow las versiones del modelo que usa el game day y la referencia de drift de v1.

* v1 (1.0.0) · el artefacto de la Fase 2 tal cual (macro-F1 0.852 en los 169 claims de prueba).
  Alias `champion` y `estable`.
* v2 (1.1.0) · reentrenamiento con un lote de etiquetas que trae un error sistemático de mapeo de
  catálogo: los claims SALUD de "beneficio genérico" y "función de nutrimento" quedaron como
  MARKETING. Su compuerta solo pedía exactitud global ≥ 0.80 en validación cruzada sobre ese
  mismo lote (0.845), así que el error no se vio. Es la versión que se despliega en INC-01.

Uso:  python scripts/preparar_modelos.py   (con el servidor de MLflow en marcha)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import joblib  # noqa: E402
import mlflow  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.metrics import accuracy_score, f1_score, recall_score  # noqa: E402
from sklearn.model_selection import StratifiedKFold, cross_val_predict  # noqa: E402

from automatizacion import registro_modelos as reg  # noqa: E402
from automatizacion.comun import (DIR_RESULTADOS, EXPERIMENTO_ENTRENAMIENTO, URI_MLFLOW,  # noqa: E402
                                  iniciar_servidor_mlflow)
from src.entrenar_modelo import construir_pipeline  # noqa: E402

SEMILLA = 42


def metricas(y_true, y_pred, prefijo: str) -> dict:
    return {f"{prefijo}_accuracy": round(accuracy_score(y_true, y_pred), 4),
            f"{prefijo}_f1_macro": round(f1_score(y_true, y_pred, average="macro"), 4),
            f"{prefijo}_recall_salud": round(recall_score(y_true, y_pred, labels=["SALUD"], average="macro"), 4)}


def main() -> None:
    iniciar_servidor_mlflow()
    mlflow.set_tracking_uri(URI_MLFLOW)
    mlflow.set_experiment(EXPERIMENTO_ENTRENAMIENTO)
    datos = pd.read_csv(RAIZ / "data" / "claims_etiquetado.csv")
    dev, prueba = datos[datos.particion == "dev"], datos[datos.particion == "test"]
    dir_ref = DIR_RESULTADOS / "referencias"

    # ── v1 · artefacto de la Fase 2 ─────────────────────────────────────────────
    v1 = joblib.load(RAIZ / "modelos" / "claimcheck_router_v1.joblib")
    with mlflow.start_run(run_name="v1 · 1.0.0 · línea base Fase 2") as run:
        mlflow.set_tags({"version_semantica": "1.0.0", "origen": "Fase 2 (modelos/claimcheck_router_v1.joblib)",
                         "compuerta": "aprobada · prueba dorada de 169 claims"})
        mlflow.log_params({"C": 1, "tau": 0.35, "n_entrenamiento": len(dev), "datos": "dev (Fase 2)"})
        m = metricas(prueba.categoria, v1.predict(prueba.texto_claim), "prueba_dorada")
        mlflow.log_metrics(m)
        version_v1 = reg.registrar_version(v1, "1.0.0", "Línea base de la Fase 2 (TF-IDF + regresión logística).",
                                           {"estado": "en_produccion", "run_entrenamiento": run.info.run_id})
        ref = reg.construir_referencia(v1, prueba.texto_claim.tolist(), "v1 · validación (169 claims)")
        ref.guardar(dir_ref / "referencia_v1.json")
        mlflow.log_artifact(str(dir_ref / "referencia_v1.json"), "referencia_drift")
    reg.mover_alias("champion", version_v1, "Despliegue inicial (Fase 2)")
    reg.mover_alias("estable", version_v1, "Validada en producción")
    print(f"v1 registrada como versión {version_v1}: {m}")

    # ── v2 · lote de etiquetas con error de mapeo ───────────────────────────────
    y_lote = dev.categoria.copy()
    cambiados = dev[dev.subtipo.isin(["beneficio_generico", "funcion_nutrimento"])].index
    y_lote.loc[cambiados] = "MARKETING"  # error de mapeo del catálogo del proveedor de etiquetado
    v2 = construir_pipeline(1)
    cv = StratifiedKFold(5, shuffle=True, random_state=SEMILLA)
    pred_cv = cross_val_predict(construir_pipeline(1), dev.texto_claim, y_lote, cv=cv)
    v2.fit(dev.texto_claim, y_lote)
    with mlflow.start_run(run_name="v2 · 1.1.0 · lote de etiquetas 2026-11") as run:
        mlflow.set_tags({"version_semantica": "1.1.0", "origen": "Reentrenamiento con lote externo de etiquetas",
                         "compuerta": "aprobada · exactitud CV ≥ 0.80 en el mismo lote (sin prueba dorada ni métricas por clase)"})
        mlflow.log_params({"C": 1, "tau": 0.35, "n_entrenamiento": len(dev),
                           "etiquetas_modificadas_por_el_lote": len(cambiados)})
        m2 = metricas(y_lote, pred_cv, "validacion_lote")
        mlflow.log_metrics(m2)
        version_v2 = reg.registrar_version(v2, "1.1.0", "Reentrenamiento con el lote de etiquetas 2026-11.",
                                           {"estado": "candidata", "run_entrenamiento": run.info.run_id})
    reg.mover_alias("challenger", version_v2, "Aprobada por la compuerta del lote")
    print(f"v2 registrada como versión {version_v2}: {m2} ({len(cambiados)} etiquetas SALUD→MARKETING en el lote)")


if __name__ == "__main__":
    main()
