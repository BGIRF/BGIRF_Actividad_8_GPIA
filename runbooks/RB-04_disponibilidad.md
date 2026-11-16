# RB-04 · Errores 5xx / réplica no saludable → reinicio y rollback de aplicación

| Campo | Valor |
|---|---|
| Identificación | RB-04 · `claimcheck-api` · componente: aplicación / contenedor |
| Alertas que lo disparan | `DisponibilidadSLOQuemaRapida` (P1, automática), `DisponibilidadSLOQuemaLenta` (P2, manual) |
| Automatización | **Automática** (documentado; no se simuló en el game day) |
| Responsable | On-call de plataforma |

## Descripción
La API responde con 5xx o una réplica deja de pasar `/health`.

## Impacto
Los revisores no reciben preclasificación; consumo acelerado del presupuesto de disponibilidad (SLO 99.5 %).

## Diagnóstico
1. ¿Errores en una réplica o en todas? (logs JSON por `replica`).
2. ¿Despliegue de la aplicación en la última hora? (imagen nueva en Render / Cloud Run).
3. Logs de arranque: fallo de integridad SHA-256 del modelo, memoria (> 450 MB) o dependencia.

## Pasos de resolución
1. Réplica individual no saludable → reiniciarla (la plataforma lo hace con el health check `/health`).
2. Errores en todas tras un despliegue → **rollback de aplicación** a la imagen anterior (Render: *Rollback* al deploy previo; Cloud Run: tráfico 100 % a la revisión anterior).
3. Sin despliegue → escalar al on-call con logs y trazas de las solicitudes fallidas.

## Validación
Tasa de 5xx de 5 min = 0 y burn rate de 60 min < 1×.

## Escalamiento
P1: respuesta ≤ 15 min. Si la causa es el modelo (integridad), coordinar con RB-01.
