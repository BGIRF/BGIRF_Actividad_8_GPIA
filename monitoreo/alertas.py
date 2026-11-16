"""Motor de alertas inteligentes y gestor de incidentes (Tema 20).

Dos capas, como Prometheus + Alertmanager:

1. **MotorAlertas** evalúa las reglas de config/alertas.yaml cada minuto: burn rate multi-ventana
   sobre los SLO, guardarraíles, umbrales con periodo de confirmación (`para_min`) y umbrales
   dinámicos aprendidos en la primera hora (media + kσ).
2. **GestorAlertas** convierte alertas en incidentes: inhibición (síntoma sobre causa),
   agrupación y correlación, deduplicación, escalamiento de severidad, recordatorios, cierre y
   disparo de runbooks automáticos con control de frecuencia.

`MotorIngenuo` replica un alertado de umbrales estáticos por métrica (sin ventanas ni
agrupación) en modo sombra, para medir cuánto ruido elimina el diseño inteligente.
"""

from __future__ import annotations

import operator
import statistics
from dataclasses import dataclass, field
from typing import Callable

OPERADORES = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
ORDEN_SEVERIDAD = {"P1": 1, "P2": 2, "P3": 3, "INFO": 4}


@dataclass
class EstadoRegla:
    pendiente_desde: int | None = None
    activa: bool = False
    activa_desde: int | None = None
    valor: float | None = None
    severidad: str = ""
    contexto: dict = field(default_factory=dict)


@dataclass
class Alerta:
    nombre: str
    grupo: str
    severidad: str
    valor: float | None
    desde: int
    resumen: str
    runbook: str
    automatizacion: str
    contexto: dict


class MotorAlertas:
    def __init__(self, reglas: list[dict], minutos_aprendizaje: int = 60):
        self.reglas = {r["nombre"]: r for r in reglas}
        self.estado = {n: EstadoRegla() for n in self.reglas}
        self.minutos_aprendizaje = minutos_aprendizaje
        self.historial_dinamico: dict[str, list[float]] = {n: [] for n, r in self.reglas.items()
                                                           if r["tipo"] == "umbral_dinamico"}
        self.umbrales_aprendidos: dict[str, float] = {}

    def _umbral_dinamico(self, nombre: str, regla: dict, t: int, valor: float | None) -> float | None:
        if t < self.minutos_aprendizaje:
            if valor is not None:
                self.historial_dinamico[nombre].append(valor)
            return None
        if nombre not in self.umbrales_aprendidos:
            h = self.historial_dinamico[nombre]
            media, sigma = statistics.fmean(h), statistics.pstdev(h)
            self.umbrales_aprendidos[nombre] = max(regla.get("minimo_absoluto", 0), media + regla["sigmas"] * sigma)
        return self.umbrales_aprendidos[nombre]

    def _condicion(self, nombre: str, regla: dict, t: int, ind: dict, serie) -> tuple[bool, float | None, dict]:
        tipo = regla["tipo"]
        if tipo == "burn_rate":
            burns = [serie.burn_rate(regla["slo"], w) for w in regla["ventanas_min"]]
            larga, n_larga = burns[0]
            if n_larga < regla.get("minimo_eventos", 1):
                return False, larga, {}
            cumple = all(b >= regla["umbral"] for b, _ in burns)
            malos, validos = serie.eventos(regla["slo"], regla["ventanas_min"][0])
            ctx = {"burn_corta": round(burns[-1][0], 3), "eventos": int(validos),
                   "valor_pct": 100 * malos / validos if validos else 0, "p95": ind.get("p95_ms_15m") or 0}
            return cumple, larga, ctx
        if tipo == "guardarrail":
            valor, n = ind.get(regla["indicador"]), ind.get(regla["indicador"] + "_n", 0)
            if valor is None or n < regla.get("minimo_eventos", 1):
                return False, valor, {}
            return OPERADORES[regla["operador"]](valor, regla["umbral"]), valor, {"eventos": n}
        valor = ind.get(regla["indicador"])
        if tipo == "umbral_dinamico":
            umbral = self._umbral_dinamico(nombre, regla, t, valor)
            if umbral is None or valor is None:
                return False, valor, {}
            return valor > umbral, valor, {"umbral": umbral, "umbral_pct": 100 * umbral,
                                           "valor_pct": 100 * valor}
        if valor is None:
            return False, None, {}
        ctx = {"detalle": ind.get(regla["indicador"] + "_detalle", ""), "valor_pct": 100 * valor}
        return OPERADORES[regla["operador"]](valor, regla["umbral"]), valor, ctx

    def evaluar(self, t: int, ind: dict, serie, despliegue_reciente: bool) -> list[Alerta]:
        activas = []
        for nombre, regla in self.reglas.items():
            est = self.estado[nombre]
            cumple, valor, ctx = self._condicion(nombre, regla, t, ind, serie)
            est.valor = valor
            if cumple:
                if est.pendiente_desde is None:
                    est.pendiente_desde = t
                if t - est.pendiente_desde + 1 >= regla.get("para_min", 1) and not est.activa:
                    est.activa, est.activa_desde = True, t
            else:
                est.pendiente_desde, est.activa, est.activa_desde = None, False, None
            if est.activa:
                sev = regla["severidad"]
                if regla.get("escalar_si_despliegue_reciente_min") and despliegue_reciente and sev == "P3":
                    sev = "P2"
                est.severidad, est.contexto = sev, ctx
                datos = {"valor": valor if valor is not None else 0, **{k: v for k, v in ctx.items()}}
                try:
                    resumen = regla["resumen"].format(**datos)
                except (KeyError, ValueError):
                    resumen = regla["resumen"]
                activas.append(Alerta(nombre, regla["grupo"], sev, valor, est.activa_desde, resumen,
                                      regla.get("runbook", ""), regla.get("automatizacion", "manual"), ctx))
        return activas


@dataclass
class Incidente:
    id: str
    grupo: str
    abierto_t: int
    severidad: str
    alertas: dict = field(default_factory=dict)       # nombre → primer minuto activa
    notificado_t: int | None = None
    ultimo_aviso_t: int | None = None
    ultima_alerta_t: int | None = None
    estado: str = "abierto"
    resuelto_t: int | None = None
    causa_probable: str | None = None
    runbooks: list = field(default_factory=list)       # ids de ejecución
    grupos: set = field(default_factory=set)


class GestorAlertas:
    def __init__(self, cfg: dict, notificar: Callable[[dict], None],
                 lanzar_runbook: Callable[[str, "Incidente", Alerta], str | None],
                 runbook_activo: Callable[[str], bool]):
        self.cfg = cfg
        self.gestor = cfg["gestor"]
        self.notificar = notificar
        self.lanzar_runbook = lanzar_runbook
        self.runbook_activo = runbook_activo
        self.inhibiciones = cfg.get("inhibiciones", [])
        self.correlaciones = cfg.get("correlaciones", [])
        self.incidentes: list[Incidente] = []
        self.acciones_automaticas: dict[str, list[int]] = {}
        self.inhibidas_t: list[tuple[int, str]] = []

    # ── Inhibición ──────────────────────────────────────────────────────────────
    def inhibir(self, t: int, activas: list[Alerta]) -> list[Alerta]:
        nombres = {a.nombre for a in activas}
        silenciadas = {d for regla in self.inhibiciones if regla["origen"] in nombres for d in regla["destino"]}
        for a in activas:
            if a.nombre in silenciadas:
                self.inhibidas_t.append((t, a.nombre))
        return [a for a in activas if a.nombre not in silenciadas]

    def _incidente_abierto(self, grupo: str) -> Incidente | None:
        for inc in reversed(self.incidentes):
            if inc.estado != "resuelto" and grupo in inc.grupos:
                return inc
        return None

    def _grupo_destino(self, alerta: Alerta, nombres_activas: set) -> tuple[str, str | None]:
        for c in self.correlaciones:
            if c["si_activo"] in nombres_activas and alerta.grupo in c["agrupar"]:
                return "datos", c.get("causa_probable")
        return alerta.grupo, None

    def _aviso(self, t: int, ts: str, inc: Incidente, tipo: str, activas: list[Alerta]) -> None:
        sev = self.cfg["severidades"][inc.severidad]
        propias = [a for a in activas if a.nombre in inc.alertas]
        self.notificar({
            "t": t, "ts": ts, "tipo": tipo, "incidente": inc.id, "severidad": inc.severidad,
            "canales": sev["canales"], "grupo": inc.grupo, "causa_probable": inc.causa_probable,
            "alertas": [{"nombre": a.nombre, "severidad": a.severidad, "resumen": a.resumen,
                         "runbook": a.runbook} for a in propias],
            "titulo": f"[{inc.severidad}] {inc.id} · {', '.join(sorted(a.nombre for a in propias)) or inc.grupo}",
        })
        inc.ultimo_aviso_t = t

    # ── Ciclo por minuto ────────────────────────────────────────────────────────
    def procesar(self, t: int, ts: str, activas_crudas: list[Alerta]) -> list[Alerta]:
        activas = self.inhibir(t, activas_crudas)
        nombres = {a.nombre for a in activas}
        for a in activas:
            if a.severidad == "INFO":
                continue  # las informativas solo van al tablero; no abren incidentes
            grupo, causa = self._grupo_destino(a, nombres)
            inc = self._incidente_abierto(grupo) or self._incidente_abierto(a.grupo)
            if inc is None:
                inc = Incidente(id=f"INC-{len(self.incidentes) + 1:02d}", grupo=grupo, abierto_t=t,
                                severidad=a.severidad, grupos={grupo, a.grupo})
                self.incidentes.append(inc)
            inc.grupos |= {grupo, a.grupo}
            if causa:
                inc.causa_probable = causa
            inc.alertas.setdefault(a.nombre, t)
            inc.ultima_alerta_t = t
            if ORDEN_SEVERIDAD[a.severidad] < ORDEN_SEVERIDAD[inc.severidad]:
                inc.severidad = a.severidad
                if inc.notificado_t is not None:
                    self._aviso(t, ts, inc, "escalado", activas)
            self._quizas_automatizar(t, inc, a)

        for inc in self.incidentes:
            if inc.estado == "resuelto":
                continue
            if inc.notificado_t is None and (t - inc.abierto_t >= self.gestor["espera_grupo_min"]
                                             or inc.severidad == "P1"):
                if inc.severidad != "INFO":
                    self._aviso(t, ts, inc, "abierto", activas)
                inc.notificado_t = t
            elif inc.notificado_t is not None and inc.ultimo_aviso_t is not None and \
                    t - inc.ultimo_aviso_t >= self.gestor["repetir_cada_min"] and inc.ultima_alerta_t == t \
                    and inc.severidad != "INFO":
                self._aviso(t, ts, inc, "recordatorio", activas)
            runbooks_en_curso = any(self.runbook_activo(r) for r in inc.runbooks)
            if inc.ultima_alerta_t is not None and t - inc.ultima_alerta_t >= self.gestor["resolver_tras_min"] \
                    and not runbooks_en_curso:
                inc.estado, inc.resuelto_t = "resuelto", t
                if inc.severidad != "INFO":
                    self._aviso(t, ts, inc, "resuelto", [])
            elif runbooks_en_curso:
                inc.estado = "mitigando"
        return activas

    def _quizas_automatizar(self, t: int, inc: Incidente, a: Alerta) -> None:
        if a.automatizacion == "manual" or not a.runbook:
            return
        if ORDEN_SEVERIDAD[a.severidad] > 2:  # solo P1 y P2 disparan acciones
            return
        rb = a.runbook.split("/")[-1].split("_")[0]  # RB-01, RB-02…
        if any(r.startswith(rb) for r in inc.runbooks):
            return  # deduplicación: un runbook por incidente
        recientes = [x for x in self.acciones_automaticas.get(rb, []) if t - x < 360]
        if len(recientes) >= self.gestor["max_acciones_automaticas_por_6h"]:
            return
        ejecucion = self.lanzar_runbook(rb, inc, a)
        if ejecucion:
            inc.runbooks.append(ejecucion)
            self.acciones_automaticas.setdefault(rb, []).append(t)


class MotorIngenuo:
    """Una notificación por regla y por minuto en que se cumple la condición (sin agrupar)."""

    def __init__(self, reglas: list[dict]):
        self.reglas = reglas
        self.disparos: list[tuple[int, str]] = []

    def evaluar(self, t: int, ind: dict) -> int:
        n = 0
        for r in self.reglas:
            v = ind.get(r["indicador"])
            if v is not None and OPERADORES[r["operador"]](v, r["umbral"]):
                self.disparos.append((t, r["nombre"]))
                n += 1
        return n
