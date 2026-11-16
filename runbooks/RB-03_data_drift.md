# RB-03 · Data drift → diagnóstico, reentrenamiento y promoción con compuerta

| Campo | Valor |
|---|---|
| Identificación | RB-03 · `claimcheck-api` · componente: datos de entrada y modelo |
| Alertas que lo disparan | `DataDriftEntradasCritico` (P2); `DataDriftEntradasAdvertencia` y `ConfirmacionesRevisorAltas` (P3, manuales) |
| Automatización | **Semiautomática** (`automatizacion/runbooks.py::_rb03_drift`) |
| Responsables | On-call de ML + responsable de calidad (aprueban la promoción) |
| Versión del runbook | 1.1 · revisado en el game day del 7-nov-2026 |

## Descripción
Las características de entrada (longitud, tokens fuera de vocabulario, idioma) se alejan de la
referencia (PSI ≥ 0.20). El modelo no falla con errores visibles: baja su confianza y enruta peor.

## Impacto
Más claims a confirmación manual (carga del revisor) y, cuando llegan las etiquetas, caída de la
calidad de enrutamiento. Puede afectar el SLA de 2 días hábiles.

## Diagnóstico (automático)
1. Integridad: rechazos 422 en 30 min (descarta errores de la fuente o del esquema).
2. PSI y KS por característica contra la referencia; χ² del idioma.
3. Segmento que cambió (p. ej., proporción de claims en inglés).
4. Impacto sin etiquetas: confianza media, % a confirmación, JSD de predicciones.
5. Descartar regresión del modelo (sin despliegues en 2 h; si lo hubo → RB-01).

## Pasos de resolución
1. **Mitigación inmediata (automática):** los claims con confianza < τ ya van a confirmación obligatoria; avisar al equipo de revisión.
2. **Esperar etiquetas** del segmento nuevo confirmadas por revisores (mínimo 80; máximo 90 min).
3. **Reentrenar (automático):** `dev` de la Fase 2 + etiquetas del revisor, **excluyendo los conjuntos dorados**; mismo pipeline y C.
4. **Compuerta (automática), champion vs challenger en conjuntos dorados:** macro-F1 ES sin regresión (−0.02), recall SALUD ES sin regresión (−0.02), exactitud del segmento nuevo ≥ 0.80, recall SALUD del segmento nuevo ≥ 0.70. Si pasa → alias `@challenger`.
5. **Aprobación humana** de la promoción → mover `@champion` y recargar réplicas. La versión anterior queda como `@estable` (destino de rollback).
6. **Actualizar la referencia de drift** con los conjuntos dorados en la mezcla de idiomas observada.

## Validación
Prueba de humo en ambos segmentos (exactitud ≥ 0.80) y, a los 20 min, exactitud confirmada de la versión nueva ≥ 0.80 y PSI contra la nueva referencia < 0.20. Entonces la versión pasa a `@estable`.

## Escalamiento
Compuerta rechazada o etiquetas insuficientes en 90 min → on-call de ML (P2). Mientras tanto, el segmento nuevo se revisa a mano.

## Cierre
Postmortem: por qué el segmento nuevo no se anticipó (alta de clientes de exportación). Documentar el nuevo alcance en la model card.
