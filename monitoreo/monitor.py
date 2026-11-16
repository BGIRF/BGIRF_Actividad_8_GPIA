"""Ciclo de monitorización proactiva (Tema 19, figura 3).

Cada minuto: recolección → cálculo de SLI → evaluación contra SLO → alertas inteligentes →
acción correctiva (vía runbooks). El estado se publica en el tablero operativo y en MLflow.

El monitor no conoce el escenario del game day: solo ve la telemetría que emite la API.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from monitoreo.alertas import GestorAlertas, MotorAlertas, MotorIngenuo
from monitoreo.colector import Colector
from monitoreo.drift import Referencia
from monitoreo.sli import Minuto, SerieSLI


class Notificador:
    """Canal de notificación simulado (Slack/pager/ticket): JSONL + consola + feed del tablero."""

    def __init__(self, ruta: Path, eco: bool = True):
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text("", encoding="utf-8")
        self.enviadas: list[dict] = []
        self.eco = eco

    def __call__(self, aviso: dict) -> None:
        self.enviadas.append(aviso)
        with open(self.ruta, "a", encoding="utf-8") as f:
            f.write(json.dumps(aviso, ensure_ascii=False) + "\n")
        if self.eco:
            hora = aviso["ts"][11:16]
            print(f"  📣 {hora} {aviso['tipo'].upper():12s} {aviso['titulo']}  → {', '.join(aviso['canales'])}")


class Monitor:
    def __init__(self, cfg_slos: dict, cfg_alertas: dict, dir_logs: Path, dir_resultados: Path,
                 referencia: Referencia, inicio: datetime,
                 lanzar_runbook: Callable, runbook_activo: Callable,
                 infra: Callable[[], dict], registrar_metricas: Callable[[int, dict], None] | None = None):
        self.cfg_slos = cfg_slos
        self.slos = cfg_slos["slos"]
        self.cfg_drift = cfg_slos["indicadores_drift"]
        self.colector = Colector(dir_logs)
        self.serie = SerieSLI(self.slos, self.slos["latencia"]["umbral_ms"])
        self.referencia = referencia
        self.historial_referencias = [(0, referencia.nombre)]
        self.inicio = inicio
        self.dir = Path(dir_resultados)
        self.notificador = Notificador(self.dir / "notificaciones.jsonl")
        self.motor = MotorAlertas(cfg_alertas["reglas"])
        self.gestor = GestorAlertas(cfg_alertas, self.notificador, lanzar_runbook, runbook_activo)
        self.ingenuo = MotorIngenuo(cfg_alertas["alertado_ingenuo"])
        self.infra = infra
        self.registrar_metricas = registrar_metricas
        self.claims = deque()            # (t, registro) de los últimos 60 min para drift
        self.despliegues: list[dict] = []
        self.eventos_operativos: list[dict] = []
        self.filas: list[dict] = []      # una fila de indicadores por minuto (CSV final)
        self.activas: list = []
        self.ultimo_drift: dict = {}
        self.ruta_alertas = self.dir / "alertas.jsonl"
        self.ruta_alertas.write_text("", encoding="utf-8")
        self._activas_previas: set = set()
        self.version_en_servicio: str | None = None

    def ts(self, t: int, segundos: int = 0) -> str:
        return (self.inicio + timedelta(minutes=t, seconds=segundos)).isoformat(timespec="seconds")

    def minuto_de(self, ts: str) -> int:
        return int((datetime.fromisoformat(ts) - self.inicio).total_seconds() // 60)

    def actualizar_referencia(self, t: int, referencia: Referencia) -> None:
        self.referencia = referencia
        self.historial_referencias.append((t, referencia.nombre))

    def _minuto_evento(self, e: dict, t: int) -> int:
        try:
            return min(t, max(0, self.minuto_de(e["ts"])))
        except (KeyError, ValueError):
            return t

    def despliegue_reciente(self, t: int, minutos: int = 120) -> bool:
        return any(t - d["t"] <= minutos for d in self.despliegues)

    # ── Agregación del minuto ───────────────────────────────────────────────────
    def _agregar(self, t: int, eventos: list[dict]) -> Minuto:
        m = Minuto(t=t, ts=self.ts(t))
        for e in eventos:
            tipo = e.get("evento")
            if tipo == "solicitud":
                if str(e.get("request_id", "")).startswith("smoke-"):
                    continue  # tráfico sintético de validación: no cuenta en los SLI
                m.n_api += 1
                c = e["codigo"]
                m.n_5xx += c >= 500
                m.n_4xx += 400 <= c < 500
                m.n_429 += c == 429
                if e["endpoint"] == "/api/v1/clasificar" and c < 500:
                    m.n_clasif += 1
                    m.n_clasif_lentas += e["latencia_ms"] >= self.slos["latencia"]["umbral_ms"]
                    m.latencias_clasif.append(e["latencia_ms"])
                elif e["endpoint"] == "/api/v1/clasificar/lote" and c == 200:
                    m.latencias_lote.append(e["latencia_ms"])
                m.versiones[e["modelo_version"]] = m.versiones.get(e["modelo_version"], 0) + 1
                for cl in e.get("claims", []):
                    m.n_claims += 1
                    m.n_a_confirmar += cl["a_confirmar"]
                    m.suma_confianza += cl["confianza"]
                    self.claims.append((t, cl))
            elif tipo == "retroalimentacion":
                # Cada confirmación se atribuye a la versión que hizo la predicción: las alertas de
                # calidad miran solo la versión en servicio; el presupuesto de error, todas.
                ok = bool(e["coincide"])
                v = e.get("version_modelo") or "?"
                m.n_retro_todas += 1
                m.n_retro_ok_todas += ok
                acc = m.retro_por_version.setdefault(v, [0, 0, 0, 0])
                acc[0] += 1
                acc[1] += ok
                if e["categoria_confirmada"] == "SALUD":
                    acc[2] += 1
                    acc[3] += e["categoria_propuesta"] == "SALUD"
            elif tipo == "arranque":
                self.version_en_servicio = self.version_en_servicio or e["modelo_version"]
            elif tipo == "modelo_recargado":
                self.version_en_servicio = e["version_nueva"]
                te = self._minuto_evento(e, t)
                if e["version_anterior"] != e["version_nueva"] and not any(
                        d["version_nueva"] == e["version_nueva"] and abs(d["t"] - te) <= 1 for d in self.despliegues):
                    self.despliegues.append({"t": te, "version_anterior": e["version_anterior"],
                                             "version_nueva": e["version_nueva"]})
                    self.eventos_operativos.append({"t": te, "tipo": "modelo", "texto":
                                                    f"Modelo {e['version_anterior']} → {e['version_nueva']}"})
            elif tipo == "caos_actualizado" and e.get("motivo") != "estado vigente al arrancar":
                te = self._minuto_evento(e, t)
                if not any(x["t"] == te and x["texto"] == e.get("motivo") for x in self.eventos_operativos):
                    self.eventos_operativos.append({"t": te, "tipo": "control", "texto": e.get("motivo", "")})
        infra = self.infra()
        m.cpu_max, m.cpu_total = infra["cpu_max_pct"], infra["cpu_total_pct"]
        m.mem_max_mb, m.replicas = infra["mem_max_mb"], infra["replicas"]
        while self.claims and self.claims[0][0] < t - 59:
            self.claims.popleft()
        return m

    def _drift(self, t: int, w: int, minimo: int) -> dict | None:
        registros = [c for (tt, c) in self.claims if tt > t - w]
        if len(registros) < minimo:
            return None
        return self.referencia.comparar(registros)

    def _indicadores(self, t: int, m: Minuto) -> dict:
        s = self.serie
        ind: dict = {"t": t, "ts": m.ts}
        ind["p95_ms_1m"] = s.percentil_latencia(95, 1)
        ind["p95_ms_15m"] = s.percentil_latencia(95, 15)
        ind["p50_ms_15m"] = s.percentil_latencia(50, 15)
        ind["p95_lote_ms_15m"] = s.percentil_latencia(95, 15, "latencias_lote")
        ind["n_5xx_1m"] = m.n_5xx
        ind["n_429_1m"] = m.n_429
        ind["solicitudes_por_min"] = m.n_api
        ind["claims_por_min"] = m.n_claims
        ind["cpu_max_pct"], ind["cpu_total_pct"] = m.cpu_max, m.cpu_total
        ind["mem_max_mb"], ind["replicas"] = m.mem_max_mb, m.replicas
        ind["exactitud_10m"], ind["exactitud_10m_n"] = s.exactitud(10)
        ind["exactitud_30m"], ind["exactitud_30m_n"] = s.exactitud(30)
        ind["recall_salud_30m"], ind["recall_salud_30m_n"] = s.recall_salud(30)
        ind["confianza_media_1m"] = s.confianza_media(1)
        ind["confianza_media_15m"] = s.confianza_media(15)
        ind["pct_a_confirmar_1m"] = s.pct_a_confirmar(1)
        ind["pct_a_confirmar_15m"] = s.pct_a_confirmar(15)
        for slo, ventanas in {"disponibilidad": (5, 60), "latencia": (3, 15), "calidad_enrutamiento": (5, 30)}.items():
            for w in ventanas:
                ind[f"burn_{slo}_{w}m"] = round(s.burn_rate(slo, w)[0], 4)
            ind[f"sli_{slo}_60m"] = s.sli(slo, 60)
            ind[f"presupuesto_restante_{slo}"] = round(s.presupuesto_restante(slo), 5)
        d15 = self._drift(t, self.cfg_drift["ventana_produccion_min"], self.cfg_drift["minimo_claims_ventana"])
        d5 = self._drift(t, 5, 80)
        if d15:
            self.ultimo_drift = d15
            ind["psi_max_entradas_15m"] = d15["psi_max_entradas"]
            ind["psi_max_entradas_15m_detalle"] = d15["caracteristica_psi_max"]
            ind["jsd_predicciones_15m"] = d15["predicciones"]["jsd"]
            ind["prop_caracteristicas_con_drift"] = d15["prop_caracteristicas_con_drift"]
            for c, v in d15["por_caracteristica"].items():
                ind[f"psi_{c}"] = v["psi"]
                if "ks" in v:
                    ind[f"ks_{c}"] = v["ks"]
            ind["prop_idioma_en_15m"] = d15["por_caracteristica"]["idioma"]["prop_en_act"]
            ind["prop_oov_media_15m"] = d15["por_caracteristica"]["prop_oov"]["media_act"]
            for cat, p in d15["predicciones"]["mezcla_act"].items():
                ind[f"mezcla_{cat}"] = p
        if d5:
            ind["psi_max_entradas_5m"] = d5["psi_max_entradas"]
        return ind

    # ── Ciclo principal ─────────────────────────────────────────────────────────
    def tick(self, t: int) -> dict:
        eventos = self.colector.leer_nuevos()
        m = self._agregar(t, eventos)
        self.serie.version_actual = self.version_en_servicio
        self.serie.agregar(m)
        ind = self._indicadores(t, m)
        crudas = self.motor.evaluar(t, ind, self.serie, self.despliegue_reciente(t))
        n_ingenuas = self.ingenuo.evaluar(t, ind)
        self.activas = self.gestor.procesar(t, ind["ts"], crudas)
        nombres = {a.nombre for a in crudas}
        with open(self.ruta_alertas, "a", encoding="utf-8") as f:
            for a in crudas:
                if a.nombre not in self._activas_previas:
                    f.write(json.dumps({"t": t, "ts": ind["ts"], "estado": "disparada", "alerta": a.nombre,
                                        "severidad": a.severidad, "grupo": a.grupo, "valor": a.valor,
                                        "resumen": a.resumen, "inhibida": a not in self.activas},
                                       ensure_ascii=False, default=str) + "\n")
            for n in self._activas_previas - nombres:
                f.write(json.dumps({"t": t, "ts": ind["ts"], "estado": "resuelta", "alerta": n},
                                   ensure_ascii=False) + "\n")
        self._activas_previas = nombres
        ind["alertas_activas"] = len(self.activas)
        ind["alertas_inhibidas"] = len(crudas) - len(self.activas)
        ind["alertas_ingenuas"] = n_ingenuas
        ind["version_modelo"] = max(m.versiones, key=m.versiones.get) if m.versiones else None
        ind["exactitud_por_version_60m"] = self.serie.exactitud_por_version(60)
        self.filas.append(ind)
        if self.registrar_metricas:
            self.registrar_metricas(t, ind)
        return ind

    def estado_tablero(self) -> dict:
        ultimo = self.filas[-1] if self.filas else {}
        claves = ["p95_ms_15m", "p95_ms_1m", "claims_por_min", "n_5xx_1m", "cpu_max_pct", "replicas",
                  "exactitud_30m", "recall_salud_30m", "jsd_predicciones_15m", "psi_max_entradas_15m",
                  "prop_oov_media_15m", "confianza_media_15m", "pct_a_confirmar_15m", "burn_latencia_15m",
                  "burn_calidad_enrutamiento_30m", "burn_disponibilidad_60m", "prop_idioma_en_15m"]
        series = {k: [[f["t"], f.get(k)] for f in self.filas] for k in claves}
        return {
            "ahora": ultimo.get("ts"), "t": ultimo.get("t"), "ultimo": {k: ultimo.get(k) for k in ultimo
                                                                        if not isinstance(ultimo.get(k), dict)},
            "slos": {k: {"objetivo": v["objetivo"]} for k, v in self.slos.items()},
            "series": series,
            "activas": [{"nombre": a.nombre, "severidad": a.severidad, "desde": self.ts(a.desde)[11:16],
                         "resumen": a.resumen, "runbook": a.runbook.split("/")[-1]} for a in self.activas],
            "incidentes": [{"id": i.id, "severidad": i.severidad, "estado": i.estado, "grupo": i.grupo,
                            "abierto": self.ts(i.abierto_t)[11:16],
                            "resuelto": self.ts(i.resuelto_t)[11:16] if i.resuelto_t is not None else None,
                            "runbooks": i.runbooks, "causa": i.causa_probable}
                           for i in self.gestor.incidentes if i.severidad != "INFO"],
            "notificaciones": [{"hora": n["ts"][11:16], "tipo": n["tipo"], "titulo": n["titulo"],
                                "severidad": n["severidad"]} for n in self.notificador.enviadas[-8:]][::-1],
            "eventos": [{"hora": self.ts(e["t"])[11:16], **e} for e in self.eventos_operativos[-6:]][::-1],
            "referencia": self.referencia.nombre,
            "ultimo_drift": self.ultimo_drift.get("por_caracteristica", {}),
            "mezcla": self.ultimo_drift.get("predicciones", {}),
        }
