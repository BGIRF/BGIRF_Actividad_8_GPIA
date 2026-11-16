# RB-02 · Latencia alta por saturación → escalar y proteger el tráfico interactivo

| Campo | Valor |
|---|---|
| Identificación | RB-02 · `claimcheck-api` · componente: cómputo / réplicas |
| Alertas que lo disparan | `LatenciaSLOQuemaRapida` (P2); `CPUSaturacion` (P3, se inhibe si ya hay alerta de latencia) |
| Automatización | **Semiautomática** (`automatizacion/runbooks.py::_rb02_latencia`) |
| Responsable | On-call de plataforma (aprueba el load shedding) |
| Versión del runbook | 1.1 · revisado en el game day del 7-nov-2026 |

## Descripción
El p95 de `/api/v1/clasificar` supera 300 ms de forma sostenida. Suele coincidir con un pico de
solicitudes de lote que acaparan los hilos de trabajo de la réplica.

## Impacto
El revisor espera la preclasificación al capturar una etiqueta en el portal. Consume el
presupuesto de error de `latencia` (SLO 95 % < 300 ms).

## Diagnóstico (automático)
1. p95 por endpoint (`/clasificar` vs `/clasificar/lote`), CPU por réplica, claims/min y número de réplicas.
2. ¿Hubo despliegue en la última hora? Si sí, considerar rollback de la aplicación antes que escalar.
3. En la traza de una solicitud lenta (MLflow Tracing), ¿el tiempo está en `inferencia_modelo` (cómputo) o fuera de los spans (cola de hilos)?

## Pasos de resolución
1. **Automático:** escalar horizontalmente a 3 réplicas (máximo 4).
2. Esperar 3 min y medir el p95 de 1 min.
3. Si sigue ≥ 300 ms → **con aprobación del on-call:** limitar a 2 lotes concurrentes por réplica (`POST /admin/caos {"max_lotes_concurrentes": 2}`). Los lotes excedentes reciben HTTP 429 con `Retry-After`; `/clasificar` queda protegido.
4. **Automático:** cuando el tráfico vuelve a la línea base 30 min seguidos, reducir a 1 réplica. El límite de lotes se conserva como control permanente.

## Validación
p95 de 1 min < 300 ms y burn rate de latencia (3 min) < 1× durante 5 min. Los 429 no cuentan contra el SLO de disponibilidad (rechazo controlado).

## Escalamiento
Si con 4 réplicas y límite de lotes el p95 sigue alto → on-call de plataforma (P2: respuesta ≤ 60 min) y aviso al cliente masivo para reprogramar la carga.

## Cierre
Registrar el cliente y el patrón de carga; evaluar una cola asíncrona para lotes (desacoplar del tráfico interactivo).
