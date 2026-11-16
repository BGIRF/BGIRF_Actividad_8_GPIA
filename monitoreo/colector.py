"""Colector de telemetría: lee de forma incremental los logs JSON de todas las réplicas.

Equivale al agente de recolección (Fluent Bit, OpenTelemetry Collector) de la arquitectura de
observabilidad del Tema 19: aplicación → instrumentación → recolección → almacenamiento/análisis.
"""

from __future__ import annotations

import json
from pathlib import Path


class Colector:
    def __init__(self, dir_logs: Path, patron: str = "api_*.jsonl"):
        self.dir_logs = Path(dir_logs)
        self.patron = patron
        self.posiciones: dict[Path, int] = {}
        self.lineas_invalidas = 0

    def leer_nuevos(self) -> list[dict]:
        eventos = []
        for ruta in sorted(self.dir_logs.glob(self.patron)):
            pos = self.posiciones.get(ruta, 0)
            with open(ruta, "rb") as f:
                f.seek(pos)
                bloque = f.read()
            # Solo se consumen líneas completas; una línea a medio escribir se lee en el próximo ciclo.
            fin = bloque.rfind(b"\n") + 1
            self.posiciones[ruta] = pos + fin
            for linea in bloque[:fin].decode("utf-8").splitlines():
                if not linea.strip():
                    continue
                try:
                    eventos.append(json.loads(linea))
                except json.JSONDecodeError:
                    self.lineas_invalidas += 1
        return eventos
