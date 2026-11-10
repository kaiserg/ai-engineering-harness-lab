# Estado y cierre del alcance de la etapa 2

Revisión del 9 de noviembre de 2026, contrastando los cuatro documentos de diseño con código, pruebas y la guía. El primer checkpoint público es `0a46494`. La matriz incorpora los incrementos posteriores; cada apartado de validación distingue su evidencia de las ejecuciones anteriores.

## Hitos originales

| Hito | Estado | Evidencia disponible | Qué falta para el objetivo original |
| --- | --- | --- | --- |
| M0 — Contratos | Completo para el laboratorio | Tipos, estados, puertos, fixtures y pruebas reproducibles sin API de modelos. | Mantener contratos al agregar escrituras. |
| M1 — Core propio | Completo para lecturas | Validación, políticas, SQLite, límites, deadline, tiempo activo, cancelación y recuperación; guía 11–12 y pruebas del core/store/timing. | Las escrituras tienen otro contrato, implementado en M4 para el simulador; quedan pendientes contratos de proveedores reales. |
| M2 — MCP y Go | Completo con proveedores/modelo simulados | Jira/GitHub en Python, Jenkins en Go, comparación de revisión, informe parcial y ataques forzados; guía 13–17. | El subconjunto RAG de S04 se comprobará en M5; los proveedores reales se reservan a M7. |
| M3 — Operación persistente | Parcial | API/frontend, PostgreSQL, admisión transaccional, workers, leases/fencing, aislamiento de estado probado y Jira HTTP con identidad de servicio/TLS; guía 18–26. | S08 completo para efectos externos; S09 completo incluyendo acceso autorizado a trazas. La UI aún usa identidad de desarrollo y Jaeger local no aplica ACL por workspace. |
| M4 — Acciones aprobadas | Parcial: flujo visual local, ledger y efecto simulado por MCP (M4a–M4h) | `ProposalStore` persiste intención/evidencia/decisión en PostgreSQL, con idempotencia y CLI de fixtures; guía 31 y pruebas PostgreSQL. `Operations` agrega consumo único, ledger/eventos y despacho a un simulador con commit independiente; UNKNOWN se consulta sin reenvío (guía 32). M4c agrega cliente/servidor Python MCP stdio sin cambiar el ledger (guía 33); M4d expone el recorrido desde API/frontend local opt-in (guía 34); M4e agrega cierre del ID y revisión manual vinculada a evidencia (guía 35); M4g observa escrituras y M4h evalúa ocho escenarios (guías 37–38). | Autenticación de usuarios, despliegue del proveedor y resolución/evaluación con otros contratos de proveedor. No hay escrituras DevOps reales; el worker local de M4f conserva cola, heartbeat y fencing (guía 36). |
| M5 — Conocimiento | Pendiente en este repositorio | Diseño de ingestión, versiones, retirada y aislamiento. El RAG del mini laboratorio anterior no implementa este hito. | Chroma como servicio, carga/retirada, ingestión versionada, citas/ACL y S10–S11. |
| M6 — Operación observable | Implementado para el entorno local; adelantado respecto de M4/M5 | Compose, OpenTelemetry exportado directamente a Jaeger, Prometheus/Grafana, alertas y runbook de incidente; guía 27–29. | Escrituras instrumentadas en M4g; falta instrumentar ingestión y completar controles de acceso operativo y retención propios de un entorno compartido. El Collector separado del diagrama objetivo todavía no está desplegado. |
| M7 — Endurecimiento | Pendiente | Base de seguridad de servicio, pruebas adversariales deterministas y ejercicios locales. | Adaptador LLM real, conectores sandbox reales, identidad de usuarios/OIDC, evals con API, carga y revisión final de amenazas. No hay todavía evidencia de operación productiva. |

«Completo» se refiere al alcance didáctico indicado en cada fila. El harness actual toma decisiones guionadas y consulta backends ficticios. Docker, MCP, PostgreSQL, TLS y los monitores sí ejecutan sus implementaciones reales.

## Escenarios originales

| Escenario | Estado y límite de la evidencia |
| --- | --- |
| S01 — Evidencia coincidente | Implementado con fixtures y referencias. |
| S02 — Jenkins no disponible | Implementado: informe parcial, sin afirmar éxito de CI. |
| S03 — Revisión desactualizada | Implementado: comparación determinista de revisiones. |
| S04 — Instrucciones maliciosas | Cubierto en el recorrido MCP con decisiones adversariales forzadas; falta el vector RAG y evaluar un LLM real. |
| S05 — Propuesta de ejecutar pruebas | Propuesta/aprobación/rechazo y consumo único persistidos; despacho simulado por CLI. El proveedor de escritura ya usa MCP stdio Python; ya tiene API/frontend de demostración local; M4f agrega el worker de escrituras; falta autenticación real y despliegue completo. |
| S06 — Cambios tras aprobación | Revalidación de revisión, vigencia y permisos probada al consumir y al aceptar en el simulador. No equivale a un contrato atómico de Jenkins real. |
| S07 — Efecto externo incierto | Ejercicio de efecto confirmado en SQLite independiente y resultado local perdido: consulta por operation ID resuelve sin duplicar; ausencia mantiene UNKNOWN. M4e resuelve el caso sin efecto mediante cierre durable y otro actor; faltan otros contratos de proveedor. |
| S08 — Dos workers | Claims/leases/fencing de lectura y carrera de despacho de escritura simulada comprobados; el proveedor falso deduplica por ID. No demuestra deduplicación de Jenkins real. |
| S09 — Otro workspace o trace ID | Stores, historial y recuperación tienen scope probado; UI de desarrollo y Jaeger local no completan autenticación/autorización por workspace. |
| S10 — Ingestión/reindexación | Pendiente. |
| S11 — Retirada durante reindexación | Pendiente. |
| S12 — Límites y cancelación | Implementado para el recorrido de lecturas; ampliar la semántica cuando haya efectos externos. Cancelar no revierte un efecto remoto. |

## Orden de cierre propuesto

1. **Línea base de evaluaciones deterministas implementada:** nueve casos versionados cubren el subconjunto MCP de S01–S04 y dos recuperaciones con SQLite temporal. `scripts/evaluate.py` genera reporte por criterio y termina con error si hay fallos. No usa LLM real ni completa M4/M5/M7. Ver guía sección 30.
2. **M4 por incrementos:** propuesta inmutable y aprobación separada ya persistidas; ledger/despacho simulado y consulta de UNKNOWN implementados; transporte MCP conectado; API/frontend local conectado; cierre manual revisado del simulador implementado; worker local de escrituras conectado; observabilidad y ocho evals de escritura disponibles; continuar con identidad real y otros contratos de proveedor. Mantener aislado el proveedor falso antes de cualquier escritura real.
3. **M5:** agregar conocimiento con versiones y ACL; cerrar S10–S11 y ampliar S04/evals con entradas recuperadas.
4. **Cerrar M3/S09 y preparar M7:** autenticación de usuarios y acceso operativo autorizado. Incorporar LLM real en lectura, ampliar evals y llevarlas a CI. Después conectar proveedores sandbox de lectura; las escrituras reales requieren primero la evidencia de M4 y sus contratos de idempotencia/consulta.
5. **Cierre verificable:** cada escenario con su prueba/evaluación, reporte de resultados y límites; revisar carga y amenazas antes de declarar terminada la etapa.

CI/evals, configuración de la app y experiencia de administración se incorporan cuando sostengan estos criterios. LangGraph, múltiples LLMs, Kubernetes, autoscaling y alta disponibilidad permanecen posteriores al alcance base según el apartado 6 del diseño. No son condiciones nuevas para cerrar el laboratorio.

## Evidencia de validación disponible

La validación de cierre previo a M5 aprobó **235 pruebas locales, 148 PostgreSQL y 21/21 evaluaciones**: nueve de lectura, ocho de escritura y cuatro autenticadas. M4g verificó además siete comprobaciones Compose de métricas/alertas y siete casos virtuales de promtool; esas suites no se repitieron en este cierre. Los apartados siguientes conservan la evidencia histórica por incremento, sin presentarla como una ejecución nueva.

## Publicación segura del checkpoint

Se revisaron los 110 archivos exactos del primer commit y las tres capturas. Gitleaks 8.30.1 no encontró secretos; una comparación adicional no halló coincidencias con secretos locales, rutas personales ni correo personal. `.env*`, runtime, bases locales, certificados, claves y entornos/cachés quedan excluidos. El commit usa correo GitHub noreply. Esta comprobación describe ese checkpoint; cada publicación futura debe revisar sus archivos nuevos.

### Validación de M4c

Pasaron 196 pruebas locales, 71 PostgreSQL y las 9 evaluaciones deterministas de lectura. El transporte MCP de escritura agrega seis pruebas de frontera y cinco de integración, incluida caída real del proceso servidor después del commit. La CLI recuperó una operación manteniendo un solo efecto simulado. No se modificaron ni repitieron las suites Compose/rotación: los servicios y ese canal no cambiaron en este incremento.

### Validación de M4d

196 pruebas locales, 76 PostgreSQL y 9 evaluaciones de lectura aprobadas. El API opt-in y el frontend local recorrieron creación, aprobación, consumo, despacho MCP e interrupción/consulta del mismo efecto, conservando una solicitud tras reiniciar la API. Usuarios reales, worker de escrituras y resolución manual siguen pendientes.

M4e agrega revisión manual persistida con dos actores de prueba y cierre durable del ID en el proveedor. El cierre sin recibo impide aceptar un envío tardío antes de permitir NOT_APPLIED. Esto cubre el caso local de ausencia con fencing explícito; no completa contratos de Jenkins real ni autenticación de usuarios.

### Validación de M4f

198 pruebas locales, 100 PostgreSQL y 9 evaluaciones de lectura aprobadas. Catorce pruebas nuevas cubren cola, worker independiente, fencing, heartbeat, cuota, intentos, SIGTERM y muerte real antes/después del despacho; las rutas HTTP solo encolan en este modo. La prueba SIGTERM se reforzó y pasó reteniendo un lock hasta que el hijo estuviera RUNNING. El recorrido visual terminó con dos reclamos y un solo efecto, sin alterar los cuatro pasos de modelo y tres herramientas de la lectura.

### Validación de M4g

Observabilidad del canal de escritura implementada: spans del ledger/worker/cliente/servidor MCP, links persistidos y privados, dos exporters loopback y seis paneles adicionales en Grafana. El recorrido real after-send mostró request y lookup en trazas enlazadas, dos reclamos y un único efecto, manteniendo 4/3 llamadas de lectura. Se conservan pendientes evals de escritura, identidad real y contratos de proveedores reales; la observación local no completa S09 empresarial. Ver guía 37 para activación, pruebas y límites.

Validación M4g: **204 pruebas locales, 102 PostgreSQL, siete comprobaciones Compose y 9/9 evaluaciones de lectura aprobadas**. Las comprobaciones Compose incluyen reglas/alertas y siete casos virtuales de promtool. La verificación real confirmó el vínculo padre/hijo cliente-servidor MCP y los links entre reclamos. No quedaron trabajos PENDING/RUNNING ni operaciones DISPATCHING/UNKNOWN en el schema principal. Gitleaks no encontró secretos en los archivos publicables nuevos o modificados.

### Validación de M4h

Línea base de escrituras con expectativas versionadas en `evals/write_cases.json` y ejecutor `scripts/evaluate_writes.py`: ocho casos de seguridad/recuperación con PostgreSQL temporal y proveedor MCP real ficticio. Se detectó y eliminó una segunda consulta innecesaria en el mismo reclamo cuando UNKNOWN no tenía recibo; se conserva la consulta inmediata después de un despacho incierto. No se cambiaron aprobaciones, leases, fencing ni políticas. El grader falla ante envíos/efectos extra, ausencia de revisión, cambio de evidencia y errores del entorno. Ver guía 38 para alcance y comandos.

Validación M4h: **213 pruebas locales, 104 PostgreSQL, 8/8 evaluaciones de escritura y 9/9 de lectura aprobadas**. Las integraciones incluyen muerte real del servidor MCP tras el commit, revisión manual, rechazo de envío tardío y eliminación del schema temporal. No quedaron schemas `harness_eval_*`, trabajos PENDING/RUNNING ni operaciones DISPATCHING/UNKNOWN en la app. El worker local se reinició con la corrección; ambos exporters continuaron disponibles. Las suites Compose/rotación no se repitieron: su configuración no cambió. Gitleaks no encontró secretos en los archivos publicables revisados.

## Checkpoint M4h antes de identidad de usuarios

Se publica el avance acumulado desde `db6938d`: evaluaciones de lectura y escritura, propuestas/aprobaciones, ledger, simulador por MCP, frontend, cierre manual, worker de escrituras y observabilidad. La documentación describe el estado local hasta M4h y mantiene explícitos los pendientes de identidad/aislamiento, RAG y proveedores reales.

La evidencia funcional es la validación M4h indicada arriba; esta revisión documental no vuelve a ejecutar ni atribuye resultados nuevos a esas suites. Antes de publicar se revisaron los 151 archivos del árbol completo preparado para el commit y las tres capturas nuevas con datos ficticios. Gitleaks no encontró secretos; tampoco se encontraron bases/claves, rutas personales ni enlaces locales rotos en Markdown. `.env*`, bases, reportes/runtime, claves, certificados y cachés quedan excluidos. El autor usa correo GitHub noreply. No se incluyen bases de prueba: se generan con las migraciones/fixtures y los comandos de reproducción de la guía.

El próximo incremento es identidad de usuarios y autorización por workspace, conservando los contratos de aprobación, recuperación y acceso a evidencia. La identidad de servicio MCP/TLS existente se mantiene separada de la identidad del usuario.

### M3d1 frontera de identidad verificada

Se agrega un directorio PostgreSQL con cuentas, pertenencias y credenciales locales revocables, emitidas por CLI administrativa. Un endpoint separado en localhost:8004 resuelve identidad/roles/workspace desde el servidor. No modifica versiones del core/propuestas ni workflows existentes. Las consolas 8002/8003 y los workers conservan fixtures; identidad real en rutas de negocio, login web/OIDC y autorización completa de operación siguen pendientes. La guía 39 explica reproducción, seguridad y el siguiente paso de integración.

Validación M3d1: **216 pruebas locales, 111 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Se verificó HTTP real con tres identidades, rechazo cross-workspace, cambios de roles y revocación; todas las credenciales del ejercicio quedaron revocadas. No quedaron schemas de pruebas de identidad, trabajos de escritura pendientes/activos ni operaciones inciertas. Se conservan los monitores/servicios anteriores y no se repitieron suites Compose/rotación. Gitleaks no encontró secretos en los archivos nuevos/modificados publicables; runtime y credenciales siguen excluidos.

### M3d2 API y cola de lecturas con identidad verificada

La API opt-in 8005 verifica la credencial en cada petición y usa ownership actor/workspace para listar, consultar y recuperar. Runs/jobs se separan en `harness_users_lab`, protegido de los modos reservados de fixtures. Workers configurados por actor/workspace resuelven permisos actuales al reclamar y antes del I/O; no almacenan credenciales del cliente en jobs. Conserva evidencia/límites en recuperación y rechaza permisos retirados. El recorrido de lectura autenticado no conecta todavía la aprobación M4 ni login web/OIDC. Ver guía 40 para ejecución, alcance y resultados.

Validación M3d2: **219 pruebas locales, 119 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Incluye ocho integraciones nuevas de lectura autenticada y tres tests de frontera/cliente/configuración; las comprobaciones específicas del schema reservado también pasaron tras añadir el guard. La prueba HTTP real conservó evidencia Jira y conteos 4/3, rechazó acceso cruzado y bloqueó I/O con permisos retirados. No quedaron jobs de lectura activos en ninguno de los dos schemas, schemas de pruebas de esta etapa ni credenciales activas de la verificación. La suite Compose/rotación no se repitió. Gitleaks no encontró secretos; los archivos de credenciales y reportes siguen ignorados.

### M3d3 propuestas y aprobaciones autenticadas

La API de usuarios conecta ProposalStore/Operations al directorio actual: el operador propone, otro actor approver revisa/decide y el dueño prepara la operación. Se conservan hash, versión, plazo, consumo único y actor de decisión; la autoaprobación con ambos roles y el acceso de otro actor/workspace se rechazan. Los grants se revalidan antes de decisión/consumo. La fase termina en PREPARED sin jobs/efectos; la ejecución autenticada, su autoridad de recuperación y el login web/OIDC quedan pendientes. Guía 41.

Validación M3d3: **222 pruebas locales, 126 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Siete integraciones nuevas y tres pruebas de frontera/cliente verifican dos credenciales, alcance, permisos actuales, idempotencia y consumo único. La prueba HTTP real dejó una operación PREPARED, cero jobs/envíos y la lectura 4/3 intacta. No quedaron sesiones activas de prueba, schemas temporales de esta etapa ni operaciones inciertas en ambos modos; la intención preparada permanece como evidencia sin despacho. Las suites Compose/rotación no se repitieron. Gitleaks no encontró secretos en los archivos publicables nuevos/modificados; runtime sigue excluido.

### M3d4 cola y servicio de escrituras autenticadas

El dueño autenticado puede admitir una operación en la cola con 202; el proceso `harness.user_write_worker` atiende solo su partición. Antes del primer I/O obtiene autoridad actual de solicitante/aprobador y conserva controles de aprobación, fuente, revisión, corte remoto y fencing. La lectura/confirmación del ledger usa autoridad de servicio con alcance fijo, de modo que una retirada posterior no impide recuperar un recibo ni autoriza otro request. Eventos auditados como servicio. La ausencia conserva UNKNOWN/NEEDS_REVIEW; falta conectar el cierre/revisión manual autenticados. Guía 42.

Validación M3d4: **225 pruebas locales, 136 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Diez integraciones nuevas cubren recuperación tras deshabilitar ambos actores, muerte real del servicio después del efecto, propietario obsoleto, control final y aparcado sin recibo. La prueba HTTP real terminó SUCCEEDED/DONE con dos reclamos y un solo request, con los grants retirados durante la recuperación y la lectura 4/3 intacta. Jaeger confirmó únicamente jenkins_find_request en el segundo reclamo. La suite completa se repitió con éxito tras reforzar dos pruebas antiguas de lease corto de lectura. Se restauraron los grants y se revocaron las credenciales de la verificación. Sin operaciones inciertas ni jobs de escritura activos en ambos schemas. Las suites Compose/rotación no se repitieron. Gitleaks no encontró secretos en los archivos publicables; los reportes permanecen en runtime ignorado.

### M3d5 resolución manual autenticada

Cierre MCP durable preparado por el dueño y confirmado por otro actor con permisos actuales. La capacidad de revisión no puede enviar/reconciliar solicitudes. Un guard exige job aparcado, conserva versiones y confirma resolución/ledger/job en una transacción, incrementando fencing. Se conserva la posibilidad de finalizar evidencia ya preparada tras deshabilitar al solicitante. Una ausencia sin cierre no prueba NOT_APPLIED. El flujo local autenticado queda conectado hasta revisión manual; sigue la regresión/evals del conjunto y checkpoint antes de M5. Guía 43.

Validación M3d5: **228 pruebas locales, 146 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Diez integraciones nuevas y tres pruebas de frontera/cliente cubren cierre, revisor actual, aislamiento, rollback atómico, concurrencia y reclamos agotados. La suite completa final pasó tras hacer explícitos los aplazamientos permitidos de una prueba de recuperación anterior, sin modificar el ejecutor de producción. La verificación HTTP terminó CONFIRMED/NOT_APPLIED/DONE con cero solicitudes aceptadas, cierre que rechazó un intento tardío, un incremento de fencing y auditoría del revisor; el solicitante estaba deshabilitado al confirmar. Se restauraron grants y revocaron las sesiones del ejercicio. No quedaron operaciones inciertas ni jobs de escritura activos en los schemas principales. Compose/rotación no se repitieron. Gitleaks no encontró secretos en los archivos publicables; runtime y credenciales permanecen ignorados.

### Cierre autenticado previo a M5

Cuatro evaluaciones versionadas en `evals/authenticated_cases.json` atraviesan la API con credenciales temporales, recuperación de lecturas, revisión por otro actor, cola/servicio MCP y cierre manual. El runner aislado mide llamadas y efectos, conserva límites/evidencia y exige las denegaciones de acceso; no acepta éxito aparente con reenvío. Se suma a nueve evals de lectura y ocho de escritura, sin requerir API keys ni sesiones de la app. Guía 44. Se prepara un checkpoint local del recorrido M3d1–M3d5 para comenzar M5, sin declarar cerrados los requisitos de producción de M3/M4/S09.

Validación de cierre previo a M5: **235 pruebas locales, 148 PostgreSQL y 21/21 evaluaciones aprobadas** (9 lectura, 8 escritura y 4 autenticadas). El runner nuevo usa HTTP ASGI y procesos MCP reales del laboratorio, con schemas/proveedores temporales. Dos integraciones comprueban ejecución/limpieza y fallo de limpieza sin PASS; siete pruebas del grader/CLI verifican detección de regresiones y errores sanitizados. Las suites Compose/rotación no se repitieron, porque esta parte no modifica infraestructura. Se revisa el árbol completo del checkpoint antes de guardarlo; los reportes, sesiones, claves, certificados y bases siguen fuera de Git. M5 es el próximo hito funcional.

Revisión del checkpoint autenticado: se comprobaron los **174 archivos del árbol completo** preparado para Git. Gitleaks no encontró secretos; no contiene material privado de runtime, bases/claves, rutas personales ni enlaces locales rotos en Markdown. El checkpoint se guarda localmente para iniciar M5; esta revisión no despliega ni publica servicios.
