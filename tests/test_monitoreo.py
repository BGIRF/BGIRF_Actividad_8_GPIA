"""Pruebas del monitoreo (Actividad 8): drift, SLI/burn rate, motor de alertas, gestor e instrumentación."""

import json
import random

import pytest

from app.config import Config
from app.main import crear_app
from monitoreo.alertas import GestorAlertas, MotorAlertas, MotorIngenuo
from monitoreo.colector import Colector
from monitoreo.drift import Referencia, jsd, psi_categorica
from monitoreo.sli import Minuto, SerieSLI

SLOS = {"disponibilidad": {"objetivo": 0.995}, "latencia": {"objetivo": 0.95, "umbral_ms": 300},
        "calidad_enrutamiento": {"objetivo": 0.80}}


def registros(n, rng, idioma="es", oov=0.1, largo=25):
    return [{"n_caracteres": largo + rng.randint(-8, 8), "n_tokens": 4 + rng.randint(-2, 2),
             "prop_digitos": 0.0, "prop_oov": max(0.0, oov + rng.uniform(-0.1, 0.1)), "idioma": idioma,
             "categoria": rng.choice(["AUSENCIA", "MARKETING", "NUTRIMENTAL", "SALUD"]),
             "confianza": rng.uniform(0.4, 0.8)} for _ in range(n)]


# ── Drift ───────────────────────────────────────────────────────────────────────
def test_psi_bajo_si_la_distribucion_no_cambia():
    rng = random.Random(1)
    ref = Referencia.desde_registros("ref", registros(400, rng))
    res = ref.comparar(registros(400, rng))
    assert res["psi_max_entradas"] < 0.10
    assert res["predicciones"]["jsd"] < 0.02


def test_psi_alto_con_un_segmento_nuevo():
    rng = random.Random(2)
    ref = Referencia.desde_registros("ref", registros(400, rng))
    nuevo = registros(200, rng) + registros(200, rng, idioma="en", oov=0.8, largo=45)
    res = ref.comparar(nuevo)
    assert res["psi_max_entradas"] >= 0.20
    assert res["por_caracteristica"]["idioma"]["chi2_p"] < 0.01


def test_jsd_y_psi_categoricos_acotados():
    niveles = ["a", "b"]
    assert jsd(["a"] * 50, ["a"] * 50, niveles) == pytest.approx(0, abs=1e-6)
    assert jsd(["a"] * 50, ["b"] * 50, niveles) == pytest.approx(1, abs=1e-3)
    assert psi_categorica(["a"] * 50 + ["b"] * 50, ["a"] * 90 + ["b"] * 10, niveles) > 0.2


def test_referencia_se_guarda_y_carga(tmp_path):
    ref = Referencia.desde_registros("ref", registros(100, random.Random(3)))
    ref.guardar(tmp_path / "r.json")
    assert Referencia.cargar(tmp_path / "r.json").cortes == ref.cortes


# ── SLI y burn rate ─────────────────────────────────────────────────────────────
def serie_con(lentas_por_min, n=20, minutos=20):
    s = SerieSLI(SLOS, 300)
    for t in range(minutos):
        s.agregar(Minuto(t=t, ts="", n_api=n, n_clasif=n, n_clasif_lentas=lentas_por_min,
                         latencias_clasif=[100] * (n - lentas_por_min) + [500] * lentas_por_min))
    return s


def test_burn_rate_latencia():
    assert serie_con(1).burn_rate("latencia", 15)[0] == pytest.approx((1 / 20) / 0.05)
    assert serie_con(12).burn_rate("latencia", 15)[0] == pytest.approx(12.0)


def test_presupuesto_restante_disminuye():
    assert serie_con(0).presupuesto_restante("latencia") == 1.0
    assert serie_con(10).presupuesto_restante("latencia") < 1.0


def test_calidad_solo_cuenta_la_version_en_servicio():
    """Etiquetas tardías de una versión revertida no deben disparar alertas de la versión restaurada."""
    s = SerieSLI(SLOS, 300)
    s.agregar(Minuto(t=0, ts="", n_retro_todas=30, n_retro_ok_todas=15,
                     retro_por_version={"1.0.0": [10, 9, 4, 4], "1.1.0": [20, 6, 8, 0]}))
    s.version_actual = "1.0.0"
    assert s.exactitud(1)[0] == pytest.approx(0.9)
    assert s.recall_salud(1) == (1.0, 4)
    assert s.eventos("calidad_enrutamiento", 1, todas_las_versiones=True) == (15, 30)
    assert s.exactitud_por_version(1)["1.1.0"]["recall_salud"] == 0.0


# ── Motor de alertas ────────────────────────────────────────────────────────────
REGLA_PSI = {"nombre": "Drift", "grupo": "datos", "severidad": "P2", "tipo": "umbral",
             "indicador": "psi", "operador": ">=", "umbral": 0.2, "para_min": 3,
             "runbook": "runbooks/RB-03_x.md", "automatizacion": "manual", "resumen": "PSI {valor:.2f}"}


def test_regla_con_periodo_de_confirmacion():
    motor, s = MotorAlertas([REGLA_PSI]), SerieSLI(SLOS, 300)
    valores = [0.3, 0.3, 0.3, 0.05]
    activas = [motor.evaluar(t, {"psi": v}, s, False) for t, v in enumerate(valores)]
    assert [len(a) for a in activas] == [0, 0, 1, 0]  # dispara al tercer minuto y se resuelve


def test_burn_rate_multiventana_requiere_ambas_ventanas():
    regla = {"nombre": "Lat", "grupo": "rendimiento", "severidad": "P2", "tipo": "burn_rate", "slo": "latencia",
             "ventanas_min": [15, 3], "umbral": 6, "resumen": "x", "runbook": "r", "automatizacion": "manual"}
    motor = MotorAlertas([regla])
    assert motor.evaluar(0, {}, serie_con(12), False)          # 60 % lentas en ambas ventanas
    s = serie_con(12, minutos=15)
    for t in range(15, 18):
        s.agregar(Minuto(t=t, ts="", n_api=20, n_clasif=20, latencias_clasif=[100] * 20))
    assert not motor.evaluar(18, {}, s, False)                 # la ventana corta ya se recuperó


def test_umbral_dinamico_aprende_la_linea_base():
    regla = {"nombre": "Traf", "grupo": "rendimiento", "severidad": "INFO", "tipo": "umbral_dinamico",
             "indicador": "x", "sigmas": 4, "minimo_absoluto": 0, "para_min": 1, "resumen": "x"}
    motor, s = MotorAlertas([regla], minutos_aprendizaje=10), SerieSLI(SLOS, 300)
    for t in range(10):
        motor.evaluar(t, {"x": 20 + (t % 3)}, s, False)
    assert not motor.evaluar(10, {"x": 23}, s, False)
    assert motor.evaluar(11, {"x": 200}, s, False)


def test_gestor_agrupa_inhibe_y_lanza_un_solo_runbook():
    cfg = {"gestor": {"espera_grupo_min": 2, "repetir_cada_min": 60, "resolver_tras_min": 5,
                      "max_acciones_automaticas_por_6h": 1},
           "severidades": {s: {"canales": [s]} for s in ("P1", "P2", "P3", "INFO")},
           "inhibiciones": [{"origen": "Lat", "destino": ["CPU"]}], "correlaciones": []}
    avisos, lanzados = [], []
    gestor = GestorAlertas(cfg, avisos.append, lambda rb, inc, a: lanzados.append(rb) or f"{rb}#1", lambda i: False)
    from monitoreo.alertas import Alerta
    lat = Alerta("Lat", "rendimiento", "P2", 8.0, 0, "lat", "runbooks/RB-02_x.md", "semiautomatica", {})
    cpu = Alerta("CPU", "rendimiento", "P3", 95.0, 0, "cpu", "runbooks/RB-02_x.md", "manual", {})
    for t in range(4):
        visibles = gestor.procesar(t, f"2026-11-07T10:0{t}:00", [lat, cpu])
        assert [a.nombre for a in visibles] == ["Lat"]          # CPU inhibida por el síntoma
    for t in range(4, 12):
        gestor.procesar(t, f"2026-11-07T10:{t:02d}:00", [])
    assert len(gestor.incidentes) == 1 and lanzados == ["RB-02"]
    assert [a["tipo"] for a in avisos] == ["abierto", "resuelto"]  # deduplicación: un aviso por estado


def test_motor_ingenuo_notifica_cada_minuto():
    m = MotorIngenuo([{"nombre": "p95", "indicador": "p95", "operador": ">", "umbral": 300}])
    assert sum(m.evaluar(t, {"p95": 400}) for t in range(10)) == 10


# ── Colector ────────────────────────────────────────────────────────────────────
def test_colector_lee_solo_lineas_nuevas_y_completas(tmp_path):
    ruta = tmp_path / "api_r1.jsonl"
    ruta.write_text(json.dumps({"a": 1}) + "\n" + '{"b": 2', encoding="utf-8")
    c = Colector(tmp_path)
    assert c.leer_nuevos() == [{"a": 1}]
    with open(ruta, "a", encoding="utf-8") as f:
        f.write("}\n")
    assert c.leer_nuevos() == [{"b": 2}]


# ── Instrumentación de la API ───────────────────────────────────────────────────
@pytest.fixture()
def cliente_obs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("LOG_JSON_PATH", str(tmp_path / "api.jsonl"))
    monkeypatch.setenv("ETIQUETAS_PATH", str(tmp_path / "etiquetas.jsonl"))
    monkeypatch.setenv("ADMIN_TOKEN", "secreto")
    with TestClient(crear_app(Config())) as c:
        yield c, tmp_path


def test_log_json_con_caracteristicas_y_sin_texto(cliente_obs):
    c, d = cliente_obs
    r = c.post("/api/v1/clasificar", json={"texto": "Gluten free"})
    assert r.headers["X-Modelo-Version"] == "1.0.0"
    eventos = [json.loads(x) for x in (d / "api.jsonl").read_text().splitlines()]
    sol = [e for e in eventos if e["evento"] == "solicitud"][-1]
    assert sol["claims"][0]["idioma"] == "en" and sol["claims"][0]["prop_oov"] > 0
    assert "Gluten free" not in (d / "api.jsonl").read_text()


def test_retroalimentacion_va_al_almacen_de_etiquetas(cliente_obs):
    c, d = cliente_obs
    r = c.post("/api/v1/retroalimentacion", json={"request_id": "x", "texto": "Sin gluten",
                                                  "categoria_propuesta": "AUSENCIA", "categoria_confirmada": "AUSENCIA"})
    assert r.status_code == 202 and r.json()["coincide"]
    assert "Sin gluten" in (d / "etiquetas.jsonl").read_text()
    assert "Sin gluten" not in (d / "api.jsonl").read_text()


def test_metricas_prometheus(cliente_obs):
    c, _ = cliente_obs
    c.post("/api/v1/clasificar", json={"texto": "Sin gluten"})
    texto = c.get("/metrics").text
    assert 'claimcheck_predicciones_total{categoria="AUSENCIA",version_modelo="1.0.0"}' in texto
    assert "claimcheck_latencia_segundos_bucket" in texto


def test_admin_requiere_token(cliente_obs):
    c, _ = cliente_obs
    assert c.post("/admin/caos", json={}).status_code == 403
    assert c.post("/admin/caos", json={}, headers={"X-Admin-Token": "otro"}).status_code == 403


def test_limite_de_lotes_responde_429(cliente_obs):
    c, _ = cliente_obs
    h = {"X-Admin-Token": "secreto"}
    assert c.post("/admin/caos", json={"max_lotes_concurrentes": 1}, headers=h).status_code == 200
    c.app.state.caos.entrar_lote()  # ocupa el único lugar disponible
    r = c.post("/api/v1/clasificar/lote", json={"claims": ["Sin gluten"]})
    assert r.status_code == 429 and r.headers["Retry-After"] == "5"
    c.app.state.caos.salir_lote()
    assert c.post("/api/v1/clasificar/lote", json={"claims": ["Sin gluten"]}).status_code == 200


def test_falla_inyectada_devuelve_503(cliente_obs):
    c, _ = cliente_obs
    c.post("/admin/caos", json={"tasa_error": 1.0}, headers={"X-Admin-Token": "secreto"})
    assert c.post("/api/v1/clasificar", json={"texto": "Sin gluten"}).status_code == 503
    c.post("/admin/caos", json={"tasa_error": 0.0}, headers={"X-Admin-Token": "secreto"})


def test_admin_deshabilitado_sin_token():
    from fastapi.testclient import TestClient
    with TestClient(crear_app(Config())) as c:
        assert c.post("/admin/modelo/recargar").status_code == 404
