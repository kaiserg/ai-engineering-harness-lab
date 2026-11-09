# Estado y cierre del alcance de la etapa 2

Revisión del 9 de noviembre de 2026, contrastando los cuatro documentos de diseño con código, pruebas y la guía. El primer checkpoint público es `0a46494`. Esta revisión no agrega funciones ni convierte pruebas históricas en ejecuciones nuevas.

## Hitos originales

| Hito | Estado | Evidencia disponible | Qué falta para el objetivo original |
| --- | --- | --- | --- |
| M0 — Contratos | Completo para el laboratorio | Tipos, estados, puertos, fixtures y pruebas reproducibles sin API de modelos. | Mantener contratos al agregar escrituras. |
| M1 — Core propio | Completo para lecturas | Validación, políticas, SQLite, límites, deadline, tiempo activo, cancelación y recuperación; guía 11–12 y pruebas del core/store/timing. | Las escrituras tienen otro contrato de recuperación, todavía pendiente en M4. |
| M2 — MCP y Go | Completo con proveedores/modelo simulados | Jira/GitHub en Python, Jenkins en Go, comparación de revisión, informe parcial y ataques forzados; guía 13–17. | El subconjunto RAG de S04 se comprobará en M5; los proveedores reales se reservan a M7. |
| M3 — Operación persistente | Parcial | API/frontend, PostgreSQL, admisión transaccional, workers, leases/fencing, aislamiento de estado probado y Jira HTTP con identidad de servicio/TLS; guía 18–26. | S08 completo para efectos externos; S09 completo incluyendo acceso autorizado a trazas. La UI aún usa identidad de desarrollo y Jaeger local no aplica ACL por workspace. |
| M4 — Acciones aprobadas | Pendiente; contratos puros implementados | `Proposal` vincula payload/revisión/expiración y rechaza autoaprobación; `Operation` modela UNKNOWN y estrategias de recuperación; pruebas en `tests/test_contracts.py`. | Persistencia de propuestas/aprobaciones/ledger y recorrido ejecutable S05–S07. Todavía no hay herramienta de escritura ni reconciliación conectada al worker. |
| M5 — Conocimiento | Pendiente en este repositorio | Diseño de ingestión, versiones, retirada y aislamiento. El RAG del mini laboratorio anterior no implementa este hito. | Chroma como servicio, carga/retirada, ingestión versionada, citas/ACL y S10–S11. |
| M6 — Operación observable | Implementado para el entorno local; adelantado respecto de M4/M5 | Compose, OpenTelemetry exportado directamente a Jaeger, Prometheus/Grafana, alertas y runbook de incidente; guía 27–29. | Instrumentar escrituras/ingestión cuando existan; controles de acceso operativo y retención propios de un entorno compartido. El Collector separado del diagrama objetivo todavía no está desplegado. |
| M7 — Endurecimiento | Pendiente | Base de seguridad de servicio, pruebas adversariales deterministas y ejercicios locales. | Adaptador LLM real, conectores sandbox reales, identidad de usuarios/OIDC, evals con API, carga y revisión final de amenazas. No hay todavía evidencia de operación productiva. |

«Completo» se refiere al alcance didáctico indicado en cada fila. El harness actual toma decisiones guionadas y consulta backends ficticios. Docker, MCP, PostgreSQL, TLS y los monitores sí ejecutan sus implementaciones reales.

## Escenarios originales

| Escenario | Estado y límite de la evidencia |
| --- | --- |
| S01 — Evidencia coincidente | Implementado con fixtures y referencias. |
| S02 — Jenkins no disponible | Implementado: informe parcial, sin afirmar éxito de CI. |
| S03 — Revisión desactualizada | Implementado: comparación determinista de revisiones. |
| S04 — Instrucciones maliciosas | Cubierto en el recorrido MCP con decisiones adversariales forzadas; falta el vector RAG y evaluar un LLM real. |
| S05 — Propuesta de ejecutar pruebas | Contrato puro; falta persistencia y recorrido de propuesta/aprobación. |
| S06 — Cambios tras aprobación | Hash/revisión/expiración probados en contrato; falta comprobar precondiciones al despachar la acción persistida. |
| S07 — Efecto externo incierto | Estado UNKNOWN y elección de estrategia probados en contrato; falta ejercicio de proveedor con efecto confirmado remotamente y resultado local perdido. |
| S08 — Dos workers | Claims, leases y fencing probados para jobs de lectura; no equivale a deduplicación de una escritura en Jenkins. |
| S09 — Otro workspace o trace ID | Stores, historial y recuperación tienen scope probado; UI de desarrollo y Jaeger local no completan autenticación/autorización por workspace. |
| S10 — Ingestión/reindexación | Pendiente. |
| S11 — Retirada durante reindexación | Pendiente. |
| S12 — Límites y cancelación | Implementado para el recorrido de lecturas; ampliar la semántica cuando haya efectos externos. Cancelar no revierte un efecto remoto. |

## Orden de cierre propuesto

1. **Línea base de evaluaciones deterministas:** expresar S01–S04 y recuperación como dataset/criterios/reporte, reutilizando fixtures. Es el siguiente incremento pequeño; no necesita API key. Las pruebas existentes siguen verificando implementación y contratos.
2. **M4 por incrementos:** persistir propuesta inmutable y aprobación separada; después ledger/despacho de una acción Jenkins simulada; después UNKNOWN y recuperación. Mantener aislado el proveedor falso antes de cualquier escritura real.
3. **M5:** agregar conocimiento con versiones y ACL; cerrar S10–S11 y ampliar S04/evals con entradas recuperadas.
4. **Cerrar M3/S09 y preparar M7:** autenticación de usuarios y acceso operativo autorizado. Incorporar LLM real en lectura, ampliar evals y llevarlas a CI. Después conectar proveedores sandbox de lectura; las escrituras reales requieren primero la evidencia de M4 y sus contratos de idempotencia/consulta.
5. **Cierre verificable:** cada escenario con su prueba/evaluación, reporte de resultados y límites; revisar carga y amenazas antes de declarar terminada la etapa.

CI/evals, configuración de la app y experiencia de administración se incorporan cuando sostengan estos criterios. LangGraph, múltiples LLMs, Kubernetes, autoscaling y alta disponibilidad permanecen posteriores al alcance base según el apartado 6 del diseño. No son condiciones nuevas para cerrar el laboratorio.

## Evidencia de validación disponible

El último incremento reportó 184 pruebas locales, 6 comprobaciones Compose de métricas/alertas y 7 escenarios de promtool aprobados; además se ejecutó el incidente real con recuperación y verificación de las trazas. Las 43 pruebas PostgreSQL y otras suites Compose/Go tienen evidencia de incrementos anteriores: no se volvieron a ejecutar como parte de esta revisión documental. Ver la guía para comandos y condiciones de ejecución.

## Publicación segura del checkpoint

Se revisaron los 110 archivos exactos del primer commit y las tres capturas. Gitleaks 8.30.1 no encontró secretos; una comparación adicional no halló coincidencias con secretos locales, rutas personales ni correo personal. `.env*`, runtime, bases locales, certificados, claves y entornos/cachés quedan excluidos. El commit usa correo GitHub noreply. Esta comprobación describe ese checkpoint; cada publicación futura debe revisar sus archivos nuevos.
