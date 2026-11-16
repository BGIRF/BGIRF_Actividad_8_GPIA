"""Detección de data drift y de cambios en las predicciones (Tema 19, "Detección de data drift").

Compara la ventana reciente de producción contra una referencia (el conjunto de validación del
modelo en servicio) con cuatro métodos:

* PSI  · Population Stability Index, por cubetas de cuantiles de la referencia.
         < 0.10 estable · 0.10–0.20 advertencia · ≥ 0.20 cambio significativo.
* KS   · estadístico de Kolmogorov-Smirnov (máxima distancia entre CDF) para continuas.
* JSD  · divergencia de Jensen-Shannon (base 2, acotada en [0, 1]) para la mezcla de categorías.
* χ²   · prueba de independencia para variables categóricas (idioma).

Las características de entrada vienen de los logs JSON de la API (app/observabilidad.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import stats
from scipy.spatial.distance import jensenshannon

EPS = 1e-4
CONTINUAS = ["n_caracteres", "n_tokens", "prop_digitos", "prop_oov", "confianza"]
CATEGORICAS = {"idioma": ["es", "en", "indeterminado"],
               "categoria": ["AUSENCIA", "MARKETING", "NUTRIMENTAL", "SALUD"]}


def _proporciones(conteos: np.ndarray) -> np.ndarray:
    p = conteos.astype(float) + EPS
    return p / p.sum()


def psi_desde_proporciones(ref: np.ndarray, act: np.ndarray) -> float:
    return float(np.sum((act - ref) * np.log(act / ref)))


def cortes_cuantiles(valores: np.ndarray, cubetas: int = 10) -> np.ndarray:
    """Cortes internos por cuantiles; se eliminan duplicados (variables discretas como n_tokens)."""
    q = np.quantile(valores, np.linspace(0, 1, cubetas + 1)[1:-1])
    return np.unique(q)


def psi_continua(ref: np.ndarray, act: np.ndarray, cortes: np.ndarray) -> float:
    r = np.bincount(np.searchsorted(cortes, ref, side="right"), minlength=len(cortes) + 1)
    a = np.bincount(np.searchsorted(cortes, act, side="right"), minlength=len(cortes) + 1)
    return psi_desde_proporciones(_proporciones(r), _proporciones(a))


def psi_categorica(ref: list[str], act: list[str], niveles: list[str]) -> float:
    r = np.array([sum(v == n for v in ref) for n in niveles])
    a = np.array([sum(v == n for v in act) for n in niveles])
    return psi_desde_proporciones(_proporciones(r), _proporciones(a))


def jsd(ref: list[str], act: list[str], niveles: list[str]) -> float:
    r = _proporciones(np.array([sum(v == n for v in ref) for n in niveles]))
    a = _proporciones(np.array([sum(v == n for v in act) for n in niveles]))
    return float(jensenshannon(r, a, base=2) ** 2)


def chi2_pvalor(ref: list[str], act: list[str], niveles: list[str]) -> float:
    tabla = np.array([[sum(v == n for v in ref) for n in niveles],
                      [sum(v == n for v in act) for n in niveles]])
    tabla = tabla[:, tabla.sum(axis=0) > 0]
    if tabla.shape[1] < 2:
        return 1.0
    return float(stats.chi2_contingency(tabla)[1])


@dataclass
class Referencia:
    """Distribución de referencia: valores por característica y cortes de cuantiles fijos."""

    nombre: str
    valores: dict[str, list] = field(default_factory=dict)
    cortes: dict[str, list[float]] = field(default_factory=dict)
    cubetas: int = 10

    @classmethod
    def desde_registros(cls, nombre: str, registros: list[dict], cubetas: int = 10) -> "Referencia":
        ref = cls(nombre=nombre, cubetas=cubetas)
        for c in CONTINUAS:
            v = [r[c] for r in registros if r.get(c) is not None]
            ref.valores[c] = v
            ref.cortes[c] = cortes_cuantiles(np.array(v, dtype=float), cubetas).tolist()
        for c in CATEGORICAS:
            ref.valores[c] = [r[c] for r in registros]
        return ref

    def guardar(self, ruta: Path) -> None:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps({"nombre": self.nombre, "cubetas": self.cubetas, "valores": self.valores,
                                    "cortes": self.cortes}, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def cargar(cls, ruta: Path) -> "Referencia":
        d = json.loads(Path(ruta).read_text(encoding="utf-8"))
        return cls(nombre=d["nombre"], valores=d["valores"], cortes=d["cortes"], cubetas=d["cubetas"])

    def comparar(self, registros: list[dict]) -> dict:
        """Métricas de drift de una ventana de producción contra la referencia."""
        res: dict = {"n": len(registros), "por_caracteristica": {}}
        for c in CONTINUAS:
            act = np.array([r[c] for r in registros if r.get(c) is not None], dtype=float)
            ref = np.array(self.valores[c], dtype=float)
            if len(act) == 0:
                continue
            res["por_caracteristica"][c] = {
                "psi": round(psi_continua(ref, act, np.array(self.cortes[c])), 4),
                "ks": round(float(stats.ks_2samp(ref, act).statistic), 4),
                "media_ref": round(float(ref.mean()), 4), "media_act": round(float(act.mean()), 4)}
        idioma_act = [r["idioma"] for r in registros]
        res["por_caracteristica"]["idioma"] = {
            "psi": round(psi_categorica(self.valores["idioma"], idioma_act, CATEGORICAS["idioma"]), 4),
            "chi2_p": chi2_pvalor(self.valores["idioma"], idioma_act, CATEGORICAS["idioma"]),
            "prop_en_ref": round(float(np.mean([v == "en" for v in self.valores["idioma"]])), 4),
            "prop_en_act": round(float(np.mean([v == "en" for v in idioma_act])), 4)}
        cat_act = [r["categoria"] for r in registros]
        res["predicciones"] = {
            "jsd": round(jsd(self.valores["categoria"], cat_act, CATEGORICAS["categoria"]), 4),
            "mezcla_ref": {n: round(float(np.mean([v == n for v in self.valores["categoria"]])), 3)
                           for n in CATEGORICAS["categoria"]},
            "mezcla_act": {n: round(float(np.mean([v == n for v in cat_act])), 3) for n in CATEGORICAS["categoria"]}}
        entradas = {k: v["psi"] for k, v in res["por_caracteristica"].items() if k != "confianza"}
        res["psi_max_entradas"] = max(entradas.values())
        res["caracteristica_psi_max"] = max(entradas, key=entradas.get)
        res["prop_caracteristicas_con_drift"] = round(float(np.mean([v >= 0.2 for v in entradas.values()])), 3)
        return res
