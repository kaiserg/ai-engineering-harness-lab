# Etapa 2 — Alcance y escenarios del harness DevOps

> **Estado actualizado:** el recorrido de lectura usa modelo y backends DevOps simulados. PostgreSQL/API/workers, Jira MCP Streamable HTTP con identidad de servicio/TLS, rotación y observabilidad funcionan en local. M4a–M4h incluyen propuestas/aprobaciones, ledger, despacho MCP, frontend, cierre revisado, worker de escrituras, trazas/métricas y ocho evaluaciones deterministas. M3d1–M3d5 conectan identidad local, lecturas, aprobaciones, envío por servicio, recuperación y cierre manual autenticados en un schema separado. El cierre previo a M5 incorpora cuatro evaluaciones autenticadas y un checkpoint local. M5a inicia inventario PostgreSQL y Chroma local con versiones, retirada y búsqueda léxica con citas/ACL; M5b añade cola/worker de ingestión con recuperación y activación atómica bajo lease/fencing; M5c conecta documentos y jobs a API/UI local autenticada con carga idempotente; M5d conecta la búsqueda al core guionado con presupuesto, citas y recuperación; M5e reúne los flujos en una consola autenticada común; M5f añade embeddings semánticos locales fijados y evaluación comparativa; LLM de generación real y evaluación ampliada siguen pendientes; login web/OIDC, aislamiento operativo completo y proveedores/LLM reales de M7 siguen pendientes. Ver [la matriz de cierre](docs/status.md) y [la guía](docs/guide.md) para evidencia y límites.

**Estado:** diseño objetivo; el avance implementado se resume arriba. Esta etapa aprende a construir y operar un harness con requisitos empresariales. Los sistemas, identidades y datos de los ejercicios serán ficticios; el diseño no describe la infraestructura de una empresa concreta.

**Orden de lectura:** este alcance → [arquitectura](02-arquitectura.md) → [modelo de amenazas](03-modelo-de-amenazas.md) → [contratos y estados](04-contratos-y-estados.md).

## 1. Objetivo y criterio de aprendizaje

Construir un asistente de operaciones que reúna evidencia de Jira, GitHub, Jenkins y procedimientos internos, proponga una acción y la ejecute solamente cuando las reglas del sistema lo permitan. Debemos poder reconstruir qué ocurrió, quién autorizó la acción y cómo recuperar un resultado incierto.

El objetivo es comprender y mantener el core: decisiones del LLM, ejecución de herramientas, estado persistente, autorización, concurrencia y fallos. Una interfaz completa llegará después de un recorrido verificable de extremo a extremo.

El laboratorio anterior se conserva como referencia pequeña. Esta carpeta será un proyecto independiente; no trasladamos sus claves, bases, informes ni instantáneas Git. El diseño inicial constaba de cuatro documentos; el repositorio ya contiene aplicación, pruebas y documentación incremental. No conectamos todavía proveedores DevOps reales.

## 2. Escenario principal

> Revisá DEV-123, relacioná su PR, consultá Jenkins y explicá si hay evidencia suficiente para avanzar. Si faltan validaciones, proponé una acción.

Ejemplo de datos sintéticos: DEV-123 se relaciona con el PR 42 de `demo/atlas`; su revisión actual es `revision-A`; Jenkins tiene un build asociado a esa revisión. Un build verde de otra revisión no sirve como evidencia para la revisión actual.

El sistema devuelve hechos con referencias, datos faltantes y una recomendación. La recomendación del modelo no equivale a autorización ni a una decisión automática de despliegue. Una regla determinista compara revisiones y antigüedad de evidencia; el LLM explica el resultado sin poder cambiar esa regla.

La primera acción con efectos será solicitar una ejecución de pruebas en un job de Jenkins previamente permitido, sobre una revisión concreta. No se implementarán merge, borrado, despliegue productivo ni ejecución arbitraria de comandos en la primera versión.

## 3. Actores y permisos iniciales

| Actor | Capacidad prevista | Límite |
| --- | --- | --- |
| Lector | Consultar tickets, PR, builds y documentos autorizados. | No crea acciones externas. |
| Operador | Consultar y proponer una ejecución de pruebas. | No puede aprobar su propia propuesta. |
| Aprobador | Aprobar o rechazar una propuesta de su workspace. | No amplía permisos del solicitante ni de la integración. |
| Auditor | Consultar eventos permitidos y resultados de ejecución. | No recibe automáticamente documentos o secretos. |
| Administrador de conocimiento | Cargar, retirar e indexar documentos de su workspace. | No administra credenciales ni autoriza acciones DevOps. |

La identidad, workspace y roles se obtienen de autenticación validada y asignaciones del servidor. Escribir «soy administrador» no cambia capacidades. Probaremos dos workspaces ficticios para detectar cruces de datos; esto no sustituye una revisión de aislamiento de producción.

## 4. Escenarios y criterios de aceptación

| ID | Situación | Resultado verificable |
| --- | --- | --- |
| S01 | Ticket, PR y build de la misma revisión disponibles. | Informe con referencias y revisión verificada; ningún efecto externo. |
| S02 | Jenkins no responde y Jira/GitHub sí. | Resultado parcial con la ausencia explícita; no afirma que CI pasó. |
| S03 | Jenkins está verde para una revisión anterior. | Evidencia marcada como obsoleta; no declara lista la revisión actual. |
| S04 | README, ticket o resultado RAG contiene instrucciones maliciosas. | Se trata como dato; no modifica políticas, credenciales ni herramientas. |
| S05 | Operador solicita pruebas para un job autorizado. | Propuesta persistida con argumentos y evidencia; espera aprobación. |
| S06 | Cambia la revisión o los argumentos después de aprobar. | La aprobación anterior deja de servir; se requiere otra propuesta/aprobación. |
| S07 | El proceso cae tras enviar la acción, sin resultado local. | Operación UNKNOWN; recuperación segura según las capacidades del proveedor. |
| S08 | Dos workers intentan ejecutar la misma operación. | Solo uno reclama la operación; un reintento remoto depende del contrato de idempotencia. |
| S09 | Usuario intenta acceder a otro workspace o consultar su trace_id. | Rechazo antes de devolver contenido; evento de auditoría sin datos ajenos. |
| S10 | Se carga un documento o falla su reindexación. | Trabajo observable; la versión activa anterior continúa hasta activar una nueva completa. |
| S11 | Se retira un documento mientras se reindexa. | Deja de ser recuperable inmediatamente; el job viejo no lo reactiva. |
| S12 | El LLM repite herramientas, excede presupuesto o el usuario cancela. | Se detienen nuevos pasos; no se confunde cancelación con reversión de un efecto remoto. |

Cada ID tendrá al menos una prueba automatizada. Los ataques adversariales también se ejecutarán sin LLM, forzando llamadas a los límites de autorización: una negativa redactada por el modelo no demuestra enforcement.

## 5. Secuencia de construcción

Los hitos son orden de trabajo, no fechas prometidas. No agregamos una dependencia sin una función y una comprobación que la justifiquen.

| Hito | Construimos | Evidencia para cerrar |
| --- | --- | --- |
| M0 — Contratos | Tipos, transiciones, repositorios de estado y LLM/herramientas falsos. | Una ejecución guionada y sus errores se reproducen sin API. |
| M1 — Core propio | Ciclo acotado en Python, validación, políticas, persistencia local y cancelación. | Consultar una herramienta falsa y recuperar el estado tras reiniciar. |
| M2 — MCP y Go | Clientes MCP, Jira/GitHub en Python y Jenkins en Go, con backends simulados. | S01–S04; contrato Python↔Go y fallos parciales comprobados. |
| M3 — Operación persistente | PostgreSQL, API, worker, identidad de prueba controlada y permisos por recurso. | Reinicio de workers, S08–S09 y separación de workspaces. |
| M4 — Acciones aprobadas | Propuesta inmutable, aprobación vinculada, ledger y reconciliación. | S05–S07; efecto incierto nunca reintentado ciegamente. |
| M5 — Conocimiento | Embeddings, Chroma como servicio, carga, retirada y reindexación versionada. | S10–S11; citas autorizadas y recuperación etiquetada. |
| M6 — Operación observable | Compose, métricas, trazas distribuidas, dashboards y runbooks. | Diagnosticar un fallo y reconstruir una ejecución sin secretos. |
| M7 — Endurecimiento | Integraciones reales de sandbox, OIDC, pruebas de carga y revisión de amenazas. | Evals y evidencias operativas antes de conectar escrituras reales. |

**Orden ajustado tras cerrar M2 simulado:** M3a y M3b ya implementan API local y frontend para escenarios, runs, evidencia y recuperación sobre el core/SQLite. M3c ya incorpora PostgreSQL, trabajo durable y worker con leases/fencing; Jira ya usa MCP Streamable HTTP con identidad de servicio y TLS. La identidad de usuarios y el aislamiento operativo completo siguen pendientes. Es una secuencia de incrementos: la API y pantalla locales no cumplen por sí solas los criterios distribuidos de M3.

Las trazas y pruebas empiezan en M1; M6 añade exportación y operación de varios servicios. Docker Compose se introduce cuando aparecen procesos separados, con perfiles para no levantar toda la plataforma en cada ejercicio. Kubernetes se estudia después, si hay un objetivo comprobable de despliegue o escalado.

## 6. Qué dejamos para después

Interfaz de administración completa, múltiples proveedores de LLM, LangGraph, autoscaling, Kubernetes, alta disponibilidad y conectores a todos los productos DevOps. No se necesitan para demostrar el primer recorrido.

LangGraph será una comparación posterior: tomaremos un flujo ya probado y evaluaremos qué simplifica en estado, pausas y recuperación. No ejecutaremos dos loops de agentes anidados sin un motivo y un contrato explícitos.

## 7. Evaluaciones y operación

Separar cuatro clases de evidencia: pruebas del core sin LLM; contratos e integración Python/Go; evals de calidad con API; ejercicios operativos de fallos y recuperación. Un test saltado no cuenta como aprobado y un caso adversarial aprobado no demuestra seguridad universal.

Mediremos éxito por escenario, uso correcto de herramientas, referencias válidas, recuperación relevante, latencia, tokens, errores y acciones inciertas. Los umbrales de calidad y rendimiento se fijarán con una primera línea base; no inventamos un SLA de producción antes de medir.

## 8. Primer paso práctico

Implementar solamente M0: un `Run`, una llamada `jira_get_issue`, un resultado y una denegación; todo con fixtures. Antes de añadir un LLM real, debemos poder explicar cada transición, qué se guarda y qué pasa si el proceso se interrumpe. La primera sesión terminará con ese recorrido y pruebas, no con una plataforma de pantallas vacías.
