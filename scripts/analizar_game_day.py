"""Análisis del game day: MTTD/MTTR por incidente, comparativa de ruido y figuras para los reportes.

Uso:  python scripts/analizar_game_day.py   (después de scripts/game_day.py)
Salidas: resultados/resumen_game_day.json y resultados/figuras/*.png
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

R = RAIZ / "resultados"
FIG = R / "figuras"
# Paleta de referencia (skill dataviz): categóricos en orden fijo + tinta neutra para umbrales.
AZUL, NARANJA, AQUA, AMARILLO, VIOLETA, ROJO = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#6250d6", "#e34948"
TINTA, TENUE, REJILLA, SOMBRA = "#0b0b0b", "#52514e", "#e6e5e0", "#efeeea"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": "#b9b8b2",
                     "axes.labelcolor": TENUE, "xtick.color": TENUE, "ytick.color": TENUE,
                     "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.color": REJILLA, "grid.linewidth": 0.8, "axes.titleweight": "bold",
                     "axes.titlesize": 10, "axes.titlecolor": TINTA, "legend.frameon": False})


def hhmm(t: float) -> str:
    m = 540 + int(round(t))
    return f"{m // 60:02d}:{m % 60:02d}"


def cargar():
    gd = json.loads((R / "game_day.json").read_text(encoding="utf-8"))
    ind = pd.read_csv(R / "indicadores_por_minuto.csv")
    alertas = [json.loads(x) for x in (R / "alertas.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    return gd, ind, alertas


def resumir(gd: dict, ind: pd.DataFrame, alertas: list[dict]) -> dict:
    fallas = sorted(gd["escenario"], key=lambda e: e["t"])
    incs = [i for i in gd["incidentes"] if i["severidad"] != "INFO"]
    rbs = {r["id"]: r for r in gd["runbooks"]}
    notifs = gd["notificaciones_inteligentes"]
    resumen = []
    for k, f in enumerate(fallas):
        fin = fallas[k + 1]["t"] if k + 1 < len(fallas) else gd["duracion_min"]
        inc = next((i for i in incs if f["t"] <= i["abierto_t"] < fin), None)
        if inc is None:
            resumen.append({"incidente_escenario": f["incidente"], "falla": f["falla"], "detectado": False})
            continue
        ejec = [rbs[r] for r in inc["runbooks"] if r in rbs]
        restaurado = min((r["restaurado_t"] for r in ejec if r.get("restaurado_t") is not None), default=None)
        primera_alerta = min(inc["alertas"].values())
        avisos = [n for n in notifs if n["incidente"] == inc["id"]]
        resumen.append({
            "incidente_escenario": f["incidente"], "incidente": inc["id"], "falla": f["falla"],
            "falla_hora": hhmm(f["t"]), "severidad": inc["severidad"], "alertas": inc["alertas"],
            "primera_alerta_hora": hhmm(primera_alerta), "notificado_hora": hhmm(inc["notificado_t"]),
            "runbooks": inc["runbooks"], "restaurado_hora": hhmm(restaurado) if restaurado is not None else None,
            "cerrado_hora": hhmm(inc["resuelto_t"]) if inc["resuelto_t"] is not None else None,
            "mttd_min": primera_alerta - f["t"],
            "ttr_desde_deteccion_min": (restaurado - primera_alerta) if restaurado is not None else None,
            "ttr_desde_falla_min": (restaurado - f["t"]) if restaurado is not None else None,
            "notificaciones": [{"hora": n["ts"][11:16], "tipo": n["tipo"], "severidad": n["severidad"],
                                "titulo": n["titulo"]} for n in avisos],
            # Ventana del incidente: de la falla al cierre (o a la restauración, si es posterior) + 15 min.
            "ventana": [f["t"], max(restaurado or 0, inc["resuelto_t"] or fin) + 15],
            "fin_restauracion": restaurado,
            "causa_probable": inc["causa_probable"]})
    # Ruido: disparos fuera de las ventanas de incidente (falla → restauración + 15 min).
    ventanas = [r["ventana"] for r in resumen if r.get("ventana")]
    dentro = lambda t: any(a <= t <= b for a, b in ventanas)  # noqa: E731
    ingenuos = gd["disparos_ingenuos"]
    por_regla = Counter(n for _, n in ingenuos)
    ruido_ing = sum(not dentro(t) for t, _ in ingenuos)
    ruido_int = sum(not dentro(n["t"]) for n in notifs)
    primer_ingenuo = {}
    for r in resumen:
        if r.get("ventana"):
            ts = [t for t, _ in ingenuos if r["ventana"][0] <= t <= r["ventana"][1]]
            primer_ingenuo[r["incidente_escenario"]] = (min(ts) - r["ventana"][0]) if ts else None
    ultimo = ind.iloc[-1]
    return {
        "incidentes": resumen,
        "ruido": {"notificaciones_inteligentes": len(notifs),
                  "notificaciones_por_tipo": dict(Counter(n["tipo"] for n in notifs)),
                  "inteligentes_fuera_de_incidente": ruido_int,
                  "disparos_ingenuos": len(ingenuos), "ingenuos_por_regla": dict(por_regla.most_common()),
                  "ingenuos_fuera_de_incidente": ruido_ing,
                  "reduccion_pct": round(100 * (1 - len(notifs) / max(len(ingenuos), 1)), 1),
                  "precision_ingenuo": round(1 - ruido_ing / max(len(ingenuos), 1), 3),
                  "precision_inteligente": round(1 - ruido_int / max(len(notifs), 1), 3),
                  "minutos_a_primer_disparo_ingenuo": primer_ingenuo,
                  "alertas_inhibidas_minuto": len(gd["inhibidas"])},
        "presupuesto_restante": {s: round(float(ultimo[f"presupuesto_restante_{s}"]), 4)
                                 for s in ("disponibilidad", "latencia", "calidad_enrutamiento")},
        "umbrales_dinamicos": gd["umbrales_dinamicos"],
        "errores_cliente": gd["errores_cliente"], "segundos_reales": gd["segundos_reales"],
    }


def sombrear(ax, resumen):
    for r in resumen["incidentes"]:
        if r.get("ventana"):
            ax.axvspan(r["ventana"][0], r["fin_restauracion"] or r["ventana"][1] - 15, color=SOMBRA, zorder=0)


def eje_tiempo(ax, x0=0, x1=480, paso=60):
    ax.set_xlim(x0, x1)
    ticks = list(range(x0 - x0 % paso, x1 + 1, paso))
    ax.set_xticks(ticks)
    ax.set_xticklabels([hhmm(t) for t in ticks])


def figura_linea_tiempo(ind: pd.DataFrame, resumen: dict, gd: dict) -> None:
    fig, axs = plt.subplots(4, 1, figsize=(10, 9.2), sharex=True)
    t = ind["t"]
    ax = axs[0]
    ax.plot(t, ind["p95_ms_1m"], color=AZUL, lw=1, alpha=.55, label="p95 1 min")
    ax.plot(t, ind["p95_ms_15m"], color=AZUL, lw=2, label="p95 15 min")
    ax.axhline(300, color=TINTA, ls="--", lw=1)
    ax.text(3, 330, "SLO 300 ms", color=TENUE, fontsize=8)
    ax.set_yscale("log")
    ax.set_ylabel("ms (escala log)")
    ax.set_title("Latencia de /api/v1/clasificar")
    ax = axs[1]
    ax.plot(t, ind["exactitud_30m"], color=AQUA, lw=2, label="exactitud 30 min (versión en servicio)")
    ax.plot(t, ind["recall_salud_30m"], color=VIOLETA, lw=2, label="recall SALUD 30 min")
    ax.axhline(.80, color=TINTA, ls="--", lw=1)
    ax.axhline(.70, color=TENUE, ls=":", lw=1)
    ax.set_ylim(0, 1.02)
    ax.set_title("Calidad confirmada por el revisor (SLO 0.80 · guardarraíl SALUD 0.70)")
    ax = axs[2]
    ax.plot(t, ind["psi_max_entradas_15m"], color=NARANJA, lw=2, label="PSI máx. entradas (15 min)")
    ax.axhline(.20, color=TINTA, ls="--", lw=1)
    ax.set_title("Data drift de entradas (umbral crítico 0.20)")
    ax.set_ylabel("PSI")
    ax = axs[3]
    ax.plot(t, ind["jsd_predicciones_15m"], color=AMARILLO, lw=2, label="JSD predicciones (15 min)")
    ax.axhline(.04, color=TINTA, ls="--", lw=1)
    ax.set_title("Cambio en la mezcla de predicciones, sin etiquetas (umbral 0.04)")
    ax.set_ylabel("JSD")
    for k, ax in enumerate(axs):
        sombrear(ax, resumen)
        ax.legend(loc="lower left" if k == 1 else "upper right", fontsize=8, ncol=2,
                  facecolor="white", framealpha=0.9, frameon=True, edgecolor="white")
    for f in gd["escenario"]:
        for ax in axs:
            ax.axvline(f["t"], color=ROJO, lw=1)
        axs[0].text(f["t"] + 3, 12, f["incidente"], color=ROJO, fontsize=8, fontweight="bold")
    eje_tiempo(axs[-1])
    fig.text(0.01, 0.005, "Línea roja: falla inyectada · zona gris: incidente (falla → servicio restaurado)",
             color=TENUE, fontsize=8)
    fig.tight_layout(rect=(0, 0.015, 1, 1))
    fig.savefig(FIG / "fig_linea_tiempo_game_day.png", dpi=180)
    plt.close(fig)


def figura_ruido(resumen: dict) -> None:
    r = resumen["ruido"]
    reglas = list(r["ingenuos_por_regla"].items())[::-1]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.6), gridspec_kw={"width_ratios": [1.5, 1.1]})
    a1.barh([n for n, _ in reglas], [v for _, v in reglas], color=NARANJA, height=.6)
    for i, (_, v) in enumerate(reglas):
        a1.text(v + 2, i, str(v), va="center", color=TINTA, fontsize=8)
    a1.set_title("Alertado ingenuo: disparos por regla en 8 h")
    a1.grid(axis="y", visible=False)
    etiquetas = ["Ingenuo\n(umbral estático)", "Inteligente\n(SLO + gestor)"]
    totales = [r["disparos_ingenuos"], r["notificaciones_inteligentes"]]
    fuera = [r["ingenuos_fuera_de_incidente"], r["inteligentes_fuera_de_incidente"]]
    a2.bar(etiquetas, [a - b for a, b in zip(totales, fuera)], color=AZUL, width=.55, label="durante un incidente")
    a2.bar(etiquetas, fuera, bottom=[a - b for a, b in zip(totales, fuera)], color="#b9b8b2", width=.55,
           label="fuera de incidente (ruido)")
    for i, v in enumerate(totales):
        a2.text(i, v + max(totales) * 0.02, str(v), ha="center", color=TINTA, fontweight="bold")
    a2.set_title(f"Notificaciones: −{r['reduccion_pct']:.0f} %")
    a2.legend(fontsize=7.5, loc="upper right", bbox_to_anchor=(1.0, 0.92))
    a2.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(FIG / "fig_ruido_alertas.png", dpi=180)
    plt.close(fig)


def figura_incidente(ind: pd.DataFrame, r: dict, paneles: list[tuple], nombre: str, gd: dict) -> None:
    a, b = r["ventana"][0] - 15, r["ventana"][1] + 10
    sub = ind[(ind.t >= a) & (ind.t <= b)]
    fig, axs = plt.subplots(1, len(paneles), figsize=(10, 2.7))
    for ax, (titulo, series, umbral, ylim) in zip(axs, paneles):
        for col, color, etiqueta in series:
            ax.plot(sub.t, sub[col], color=color, lw=2, label=etiqueta)
        if umbral is not None:
            ax.axhline(umbral, color=TINTA, ls="--", lw=1)
        if ylim:
            ax.set_ylim(*ylim)
        ax.axvline(r["ventana"][0], color=ROJO, lw=1)
        for rb in gd["runbooks"]:
            if rb["incidente"] == r["incidente"]:
                for p in rb["pasos"]:
                    if p["fase"] == "Acción" and a <= p["t"] <= b:
                        ax.axvline(p["t"], color=AQUA, lw=1, ls="-.")
        ax.set_title(titulo, fontsize=9)
        eje_tiempo(ax, a, b, 15 if b - a <= 120 else 30)
        ax.tick_params(axis="x", labelrotation=0, labelsize=7.5)
        if len(series) > 1:
            ax.legend(fontsize=7, loc="best")
    fig.text(0.01, 0.01, "Roja: falla inyectada · verde punteada: acción del runbook", color=TENUE, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(FIG / nombre, dpi=180)
    plt.close(fig)


def main() -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    gd, ind, alertas = cargar()
    resumen = resumir(gd, ind, alertas)
    (R / "resumen_game_day.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    figura_linea_tiempo(ind, resumen, gd)
    figura_ruido(resumen)
    incs = {r["incidente_escenario"]: r for r in resumen["incidentes"] if r.get("ventana")}
    if "INC-01" in incs:
        figura_incidente(ind, incs["INC-01"], [
            ("JSD de predicciones (sin etiquetas)", [("jsd_predicciones_15m", AMARILLO, "JSD")], .04, None),
            ("Proporción de SALUD propuesta", [("mezcla_SALUD", VIOLETA, "SALUD")], None, (0, .4)),
            ("Recall SALUD (versión en servicio)", [("recall_salud_30m", VIOLETA, "")], .70, (0, 1.02))],
            "fig_inc01.png", gd)
    if "INC-02" in incs:
        figura_incidente(ind, incs["INC-02"], [
            ("p95 /clasificar 1 min (ms)", [("p95_ms_1m", AZUL, "")], 300, None),
            ("Burn rate de latencia (3 min)", [("burn_latencia_3m", AZUL, "")], 6, None),
            ("Réplicas en servicio", [("replicas", AQUA, "")], None, (0, 4))], "fig_inc02.png", gd)
    if "INC-03" in incs:
        figura_incidente(ind, incs["INC-03"], [
            ("PSI máx. de entradas", [("psi_max_entradas_15m", NARANJA, "")], .20, None),
            ("Claims detectados como inglés", [("prop_idioma_en_15m", AZUL, "")], None, (0, 1)),
            ("Exactitud confirmada 30 min", [("exactitud_30m", AQUA, "")], .80, (0, 1.02))], "fig_inc03.png", gd)
    print(json.dumps(resumen["incidentes"], ensure_ascii=False, indent=1, default=str)[:4000])
    print(json.dumps(resumen["ruido"], ensure_ascii=False, indent=1))
    print(resumen["presupuesto_restante"])


if __name__ == "__main__":
    main()
