# RB-01 · Degradación del modelo tras un despliegue → rollback automático

| Campo | Valor |
|---|---|
| Identificación | RB-01 · `claimcheck-api` · componente: modelo de enrutamiento |
| Alertas que lo disparan | `CambioDistribucionPredicciones` (P2 si hubo despliegue en 2 h), `RecallSaludBajo` (P1), `CalidadSLOQuemaRapida` (P1) |
| Automatización | **Automática** (`automatizacion/runbooks.py::_rb01_rollback`) |
| Responsable | On-call de ML (se le notifica; no necesita aprobar) |
| Versión del runbook | 1.1 · revisado en el game day del 7-nov-2026 |

## Descripción
Después de desplegar una versión nueva del modelo, la mezcla de categorías propuestas cambia o el
revisor empieza a corregir más claims de lo normal. Las entradas siguen estables.

## Impacto
Claims enrutados al checklist equivocado. El riesgo mayor es un claim de **SALUD** que llega a
CL-MKT y se revisa sin los criterios de los Arts. 10-14 del Reg. (CE) 1924/2006. Consume el
presupuesto de error de `calidad_enrutamiento` y aumenta el retrabajo de los revisores.

## Diagnóstico (automático)
1. ¿Hubo un cambio de versión del modelo en las últimas 2 h? (eventos `modelo_recargado`). Si **no** → no es una regresión del modelo: escalar a **RB-03**.
2. ¿Las entradas están estables? (PSI máximo de entradas < 0.20). Si **no** → el cambio viene de los datos: escalar a **RB-03**.
3. Comparar la mezcla de predicciones con la referencia (qué clase perdió y cuál ganó).
4. Guardarraíles: existe un alias `@estable` distinto de `@champion`; no hubo otro rollback en las últimas 6 h (control de frecuencia contra ciclos de reversión).

## Pasos de resolución (automáticos)
1. Mover el alias `@champion` a la versión `@estable` en el MLflow Model Registry.
2. Etiquetar la versión revertida con `estado=revertida`, el incidente y la alerta.
3. `POST /admin/modelo/recargar` en todas las réplicas (intercambio atómico; si la carga falla, la réplica conserva el modelo anterior).

## Validación
1. Inmediata: `/health` de todas las réplicas reporta la versión estable; prueba de humo con 20 claims dorados (5 por clase): exactitud ≥ 0.80, recall SALUD ≥ 0.70 y todas HTTP 200. El tráfico de humo (`X-Request-ID: smoke-*`) no cuenta en los SLI.
2. Diferida (20 min): JSD de predicciones < 0.04 y exactitud confirmada por el revisor de la versión restaurada ≥ 0.80.

## Escalamiento
- Falla cualquier diagnóstico o guardarraíl → on-call de ML (P1: respuesta ≤ 15 min).
- La prueba de humo falla después del rollback → incidente P1 de disponibilidad del modelo; el on-call decide entre `@estable` anterior o modo manual (todos los claims a confirmación).

## Cierre
Postmortem obligatorio: por qué la compuerta de validación aprobó la versión. La versión revertida
queda bloqueada hasta corregir la causa.
