# Guía de construcción del harness DevOps

Esta guía registra lo que ya funciona y explica cómo se relaciona con el diseño. Leer [el alcance](../01-alcance-y-escenarios.md) para ver los hitos futuros. Los ejemplos usan datos ficticios y no requieren API keys.

Para distinguir lo implementado del diseño objetivo, consultá la [matriz de cierre de etapa 2](status.md), con hitos, escenarios y pendientes.

La guía se divide ahora en **55 capítulos**, uno por paso, dentro de `docs/guide/`. El contenido anterior se conserva en su orden de construcción; la [matriz de estado](status.md) distingue lo actual de los pasos históricos.

**Último paso:** [M7g · Del ticket de Jira Cloud al build de Jenkins](guide/58-m7g-jira-cloud-a-ci-desde-el-chat.md).

## Índice

Los encabezados conservan las anclas de la guía anterior para mantener sus enlaces. Abrí cada capítulo para leer el paso a paso.

## M0 y su límite

[1. Leer el capítulo](guide/01-m0-y-su-limite.md).

## Entorno sin dependencias externas

[2. Leer el capítulo](guide/02-entorno-sin-dependencias-externas.md).

## Contratos de dominio

[3. Leer el capítulo](guide/03-contratos-de-dominio.md).

## Puertos y adaptadores falsos

[4. Leer el capítulo](guide/04-puertos-y-adaptadores-falsos.md).

## Store y control de versión

[5. Leer el capítulo](guide/05-store-y-control-de-version.md).

## Ciclo de ejecución propio

[6. Leer el capítulo](guide/06-ciclo-de-ejecucion-propio.md).

## Demostración permitida y denegada

[7. Leer el capítulo](guide/07-demostracion-permitida-y-denegada.md).

## Contratos aislados de aprobación y recuperación

[8. Leer el capítulo](guide/08-contratos-aislados-de-aprobacion-y-recuperacion.md).

## Pruebas y límites pendientes

[9. Leer el capítulo](guide/09-pruebas-y-limites-pendientes.md).

## Lo que sigue en M1

[10. Leer el capítulo](guide/10-lo-que-sigue-en-m1.md).

## M1 guardar y recuperar un run con SQLite

[11. Leer el capítulo](guide/11-m1-guardar-y-recuperar-un-run-con-sqlite.md).

## M1 deadline global y tiempo activo persistido

[12. Leer el capítulo](guide/12-m1-deadline-global-y-tiempo-activo-persistido.md).

## M2 primera herramienta por MCP

[13. Leer el capítulo](guide/13-m2-primera-herramienta-por-mcp.md).

## M2 GitHub por MCP y recuperación entre herramientas

[14. Leer el capítulo](guide/14-m2-github-por-mcp-y-recuperacion-entre-herramientas.md).

## M2 Jenkins en Go y comparación de revisiones

[15. Leer el capítulo](guide/15-m2-jenkins-en-go-y-comparacion-de-revisiones.md).

## M2 informe parcial cuando falta Jenkins

[16. Leer el capítulo](guide/16-m2-informe-parcial-cuando-falta-jenkins.md).

## M2 S04 contenido malicioso y cierre del hito

[17. Leer el capítulo](guide/17-m2-s04-contenido-malicioso-y-cierre-del-hito.md).

## M3a API local sobre el mismo core

[18. Leer el capítulo](guide/18-m3a-api-local-sobre-el-mismo-core.md).

## M3b primer frontend del harness

[19. Leer el capítulo](guide/19-m3b-primer-frontend-del-harness.md).

## M3c PostgreSQL y productor transaccional

[20. Leer el capítulo](guide/20-m3c-postgresql-y-productor-transaccional.md).

## M3c worker independiente leases y recuperación

[21. Leer el capítulo](guide/21-m3c-worker-independiente-leases-y-recuperacion.md).

## M3c API y worker en Docker Compose

[22. Leer el capítulo](guide/22-m3c-api-y-worker-en-docker-compose.md).

## M3c Jira por Streamable HTTP e identidad de servicio

[23. Leer el capítulo](guide/23-m3c-jira-por-streamable-http-e-identidad-de-servicio.md).

## M3c TLS y emisor de identidad separado

[24. Leer el capítulo](guide/24-m3c-tls-y-emisor-de-identidad-separado.md).

## M3c renovación TLS y rotación de firma recuperables

[25. Leer el capítulo](guide/25-m3c-renovacion-tls-y-rotacion-de-firma-recuperables.md).

## M3c renovación programada y rotación en vivo

[26. Leer el capítulo](guide/26-m3c-renovacion-programada-y-rotacion-en-vivo.md).

## M3c OpenTelemetry y Jaeger

[27. Leer el capítulo](guide/27-m3c-opentelemetry-y-jaeger.md).

## M3c Prometheus y Grafana

[28. Leer el capítulo](guide/28-m3c-prometheus-y-grafana.md).

## M3c alertas e incidente investigable

[29. Leer el capítulo](guide/29-m3c-alertas-e-incidente-investigable.md).

## Evaluaciones deterministas del recorrido

[30. Leer el capítulo](guide/30-evaluaciones-deterministas-del-recorrido.md).

## M4a propuestas y aprobaciones persistidas

[31. Leer el capítulo](guide/31-m4a-propuestas-y-aprobaciones-persistidas.md).

## M4b ledger y despacho Jenkins simulado

[32. Leer el capítulo](guide/32-m4b-ledger-y-despacho-jenkins-simulado.md).

## M4c despacho y consulta del efecto por MCP

[33. Leer el capítulo](guide/33-m4c-despacho-y-consulta-del-efecto-por-mcp.md).

## M4d propuestas y operaciones desde API y frontend local

[34. Leer el capítulo](guide/34-m4d-propuestas-y-operaciones-desde-api-y-frontend-local.md).

## M4e resolución manual de UNKNOWN con evidencia del proveedor

[35. Leer el capítulo](guide/35-m4e-resolucion-manual-de-unknown-con-evidencia-del-proveedor.md).

## M4f worker independiente de escrituras

[36. Leer el capítulo](guide/36-m4f-worker-independiente-de-escrituras.md).

## M4g observabilidad del worker y del ledger de escrituras

[37. Leer el capítulo](guide/37-m4g-observabilidad-del-worker-y-del-ledger-de-escrituras.md).

## M4h evaluaciones deterministas de escrituras

[38. Leer el capítulo](guide/38-m4h-evaluaciones-deterministas-de-escrituras.md).

## M3d1 frontera de identidad de usuarios y workspace

[39. Leer el capítulo](guide/39-m3d1-frontera-de-identidad-de-usuarios-y-workspace.md).

## M3d2 lecturas autenticadas ownership y cola

[40. Leer el capítulo](guide/40-m3d2-lecturas-autenticadas-ownership-y-cola.md).

## M3d3 propuestas y aprobaciones con identidad autenticada

[41. Leer el capítulo](guide/41-m3d3-propuestas-y-aprobaciones-con-identidad-autenticada.md).

## M3d4 cola y worker de escrituras autenticadas

[42. Leer el capítulo](guide/42-m3d4-cola-y-worker-de-escrituras-autenticadas.md).

## M3d5 resolución manual autenticada de UNKNOWN

[43. Leer el capítulo](guide/43-m3d5-resolucion-manual-autenticada-de-unknown.md).

## Cierre autenticado evaluaciones y checkpoint antes de M5

[44. Leer el capítulo](guide/44-cierre-autenticado-evaluaciones-y-checkpoint-antes-de-m5.md).

## M5a Chroma como servicio e inventario de conocimiento

[45. Leer el capítulo](guide/45-m5a-chroma-como-servicio-e-inventario-de-conocimiento.md).

## M5b Tipo de RAG e ingestión durable

[46. Leer el capítulo](guide/46-m5b-tipo-de-rag-e-ingestion-durable.md).

## M5c API y frontend de documentos e ingestión

[47. Leer el capítulo](guide/47-m5c-api-y-frontend-de-documentos-e-ingestion.md).

## M5d RAG como herramienta del core

[48. Leer el capítulo](guide/48-m5d-rag-como-herramienta-del-core.md).

## M5e Consola autenticada unificada

[49. Leer el capítulo](guide/49-m5e-consola-autenticada-unificada.md).

## M5f Embeddings semánticos y evaluaciones de recuperación

[50. Leer el capítulo](guide/50-m5f-embeddings-semanticos-y-evaluaciones.md).

## M5g Preguntas sin respuesta y camino al chat

[51. Leer el capítulo](guide/51-m5g-preguntas-sin-respuesta.md).

## M7a LLM local de lectura, respuestas y abstención

[52. Leer el capítulo](guide/52-m7a-llm-local-de-lectura.md).

## M7b Chat e historial persistido

[53. Leer el capítulo](guide/53-m7b-chat-e-historial-persistido.md).

## M7c Jira desde el chat por MCP

[54. Leer el capítulo](guide/54-m7c-jira-desde-el-chat.md).

## M7d GitHub desde el chat por MCP

[55. Leer el capítulo](guide/55-m7d-github-desde-el-chat.md).

## M7e Investigación de CI desde el chat

[56. Leer el capítulo](guide/56-m7e-investigacion-ci-desde-el-chat.md).

## M7f PR real y Jenkins local

[57. Leer el capítulo](guide/57-m7f-pr-real-y-jenkins-local.md).

## M7g Jira Cloud, GitHub y Jenkins desde el chat

[58. Leer el capítulo](guide/58-m7g-jira-cloud-a-ci-desde-el-chat.md).
