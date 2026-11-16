"""Cálculo de SLI por minuto, ventanas móviles, burn rate y presupuesto de error.

* SLI = eventos buenos / eventos válidos en una ventana.
* Burn rate = (tasa de eventos malos en la ventana) / (presupuesto de error del SLO).
  Un burn rate de 1 agota exactamente el presupuesto al final del periodo de 28 días; 6 lo
  agota en ~4.7 días; 14.4 en ~2 días.
* Presupuesto restante = 1 − eventos malos acumulados / eventos malos permitidos en 28 días
  (estimados con el tráfico promedio observado).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

MINUTOS_PERIODO = 28 * 24 * 60


@dataclass
class Minuto:
    t: int
    ts: str
    n_api: int = 0
    n_5xx: int = 0
    n_4xx: int = 0
    n_429: int = 0
    n_clasif: int = 0
    n_clasif_lentas: int = 0
    latencias_clasif: list = field(default_factory=list)
    latencias_lote: list = field(default_factory=list)
    n_claims: int = 0
    n_a_confirmar: int = 0
    suma_confianza: float = 0.0
    n_retro_todas: int = 0    # confirmaciones de todas las versiones (presupuesto de error del SLO)
    n_retro_ok_todas: int = 0
    # versión → [confirmados, correctos, SALUD confirmados, SALUD correctos]
    retro_por_version: dict = field(default_factory=dict)
    cpu_max: float = 0.0
    cpu_total: float = 0.0
    mem_max_mb: float = 0.0
    replicas: int = 0
    versiones: dict = field(default_factory=dict)


class SerieSLI:
    def __init__(self, slos: dict, umbral_latencia_ms: float):
        self.minutos: list[Minuto] = []
        self.version_actual: str | None = None  # la fija el monitor con cada recarga de modelo
        self.slos = slos
        self.umbral_latencia_ms = umbral_latencia_ms

    def agregar(self, m: Minuto) -> None:
        self.minutos.append(m)

    def ventana(self, w: int) -> list[Minuto]:
        return self.minutos[-w:]

    def suma(self, campo: str, w: int) -> float:
        return float(sum(getattr(m, campo) for m in self.ventana(w)))

    def retro(self, w: int, version: str | None = None) -> list[float]:
        """[confirmados, correctos, SALUD confirmados, SALUD correctos] de una versión en la ventana."""
        version = version or self.version_actual
        tot = [0.0, 0.0, 0.0, 0.0]
        for m in self.ventana(w):
            for i, x in enumerate(m.retro_por_version.get(version, (0, 0, 0, 0))):
                tot[i] += x
        return tot

    # ── Eventos buenos / válidos por SLO ────────────────────────────────────────
    def eventos(self, slo: str, w: int, todas_las_versiones: bool = False) -> tuple[float, float]:
        """(malos, válidos) en la ventana de w minutos.

        Para calidad, las alertas usan solo las confirmaciones del modelo que está en servicio: las
        etiquetas que llegan tarde de una versión ya revertida van al postmortem, no al pager.
        El presupuesto de error sí cuenta todas las versiones (el impacto ocurrió).
        """
        if slo == "disponibilidad":
            return self.suma("n_5xx", w), self.suma("n_api", w)
        if slo == "latencia":
            return self.suma("n_clasif_lentas", w), self.suma("n_clasif", w)
        if slo == "calidad_enrutamiento":
            if todas_las_versiones:
                validos = self.suma("n_retro_todas", w)
                return validos - self.suma("n_retro_ok_todas", w), validos
            n, ok, _, _ = self.retro(w)
            return n - ok, n
        raise KeyError(slo)

    def presupuesto(self, slo: str) -> float:
        return 1 - self.slos[slo]["objetivo"]

    def burn_rate(self, slo: str, w: int) -> tuple[float, float]:
        malos, validos = self.eventos(slo, w)
        if validos == 0:
            return 0.0, 0.0
        return (malos / validos) / self.presupuesto(slo), validos

    def sli(self, slo: str, w: int) -> float | None:
        malos, validos = self.eventos(slo, w)
        return None if validos == 0 else 1 - malos / validos

    def presupuesto_restante(self, slo: str) -> float:
        malos, validos = self.eventos(slo, len(self.minutos), todas_las_versiones=True)
        if validos == 0:
            return 1.0
        permitidos = self.presupuesto(slo) * (validos / len(self.minutos)) * MINUTOS_PERIODO
        return max(0.0, 1 - malos / permitidos)

    # ── Indicadores derivados ───────────────────────────────────────────────────
    def percentil_latencia(self, q: float, w: int, campo: str = "latencias_clasif") -> float | None:
        valores = [x for m in self.ventana(w) for x in getattr(m, campo)]
        return float(np.percentile(valores, q)) if valores else None

    def recall_salud(self, w: int) -> tuple[float | None, float]:
        _, _, n, ok = self.retro(w)
        return (None if n == 0 else ok / n), n

    def exactitud(self, w: int) -> tuple[float | None, float]:
        n, ok, _, _ = self.retro(w)
        return (None if n == 0 else ok / n), n

    def exactitud_por_version(self, w: int) -> dict:
        versiones = {v for m in self.ventana(w) for v in m.retro_por_version}
        res = {}
        for v in sorted(versiones):
            n, ok, ns, oks = self.retro(w, v)
            if n:
                res[v] = {"n": int(n), "exactitud": round(ok / n, 4),
                          "recall_salud": round(oks / ns, 4) if ns else None}
        return res

    def pct_a_confirmar(self, w: int) -> float | None:
        n = self.suma("n_claims", w)
        return None if n == 0 else self.suma("n_a_confirmar", w) / n

    def confianza_media(self, w: int) -> float | None:
        n = self.suma("n_claims", w)
        return None if n == 0 else self.suma("suma_confianza", w) / n
