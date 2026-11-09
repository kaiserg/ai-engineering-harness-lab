# AI Engineering Harness Lab

Etapa 2: construir y operar un harness DevOps desde su core. **M0–M2 y M3a/M3b están implementados en su alcance local:** core persistido, MCP Python/Go, comparación de revisiones, API y primer frontend. El modelo sigue guionado y Jira usa datos ficticios, ahora detrás de un servidor MCP local. No requiere API keys de proveedores ni llamadas pagadas; Compose genera claves locales de identidad de servicio. Los cuatro documentos iniciales describen la arquitectura objetivo, no una plataforma ya terminada.

## Levantar la app completa con Compose

```bash
uv sync --locked
uv run python scripts/stack.py up
```

Abrí **[http://127.0.0.1:8002](http://127.0.0.1:8002)**. Compose opera PostgreSQL, API, worker, Jira MCP y un emisor de identidad independiente. Usa el mismo PostgreSQL/historial del modo 8001. Los runs HTTP usan Jira por HTTPS con tokens obtenidos del emisor; GitHub/Jenkins siguen por stdio. Los runs anteriores conservan su perfil stdio para recuperar sin cambiar de transporte.

```bash
uv run python scripts/stack.py status
uv run python scripts/stack.py logs
uv run python scripts/stack.py down
```

`down` conserva el volumen y los materiales locales de identidad/TLS. Solo el emisor tiene la clave de firma; el worker recibe una credencial de cliente y Jira obtiene la clave pública mediante JWKS por TLS. La CA es propia del laboratorio, sin instalarse en el sistema. Los endpoints son `https://127.0.0.1:8010/mcp/` y `https://127.0.0.1:8011/jwks`; la consola permanece en el puerto 8002. Ver las secciones 24–26 de `docs/guide.md` para identidad, renovación TLS y rotación de firma recuperables. La imagen compila Jenkins para Linux, instala dependencias del lockfile y excluye datos/secretos del host. La aplicación abandona root antes de servir/procesar; código e imagen son de solo lectura y las credenciales se preparan en runtime.

Para la integración de contenedores: `uv run python -m unittest discover -s container_tests -v`. Requiere la app levantada, reinicia servicios y crea runs sintéticos; detené otros workers del host que puedan consumir esos jobs.

## Métricas y dashboard con Prometheus/Grafana

Abrí **[Harness Lab · Operación](http://127.0.0.1:3003/d/harness-overview)**. El dashboard provisionado muestra scrapeo, cola/leases, estados de runs, intentos/recuperaciones, p95 de intento/modelo/tools, rechazos auth y vencimiento TLS. Usa un Viewer anónimo local sin administrador inicial; la configuración se mantiene en `observability/`.

**[Prometheus](http://127.0.0.1:9090)** scrapea cuatro exporters internos cada 10 segundos. Los contadores de proceso se distinguen de los Gauges durables de PostgreSQL; no usamos UUID, usuarios, contenido ni credenciales como etiquetas. Esperá dos o tres scrapes después de generar tráfico para ver distribuciones. La caída de los monitores no bloquea ejecución/recuperación.

Prometheus conserva siete días en su volumen; `down` conserva sus datos y los de Grafana/PostgreSQL. Las decisiones, tipos de métricas y ejercicios están en la sección 28 de `docs/guide.md`. No hay alertas/notificaciones configuradas todavía.

## Trazas distribuidas con OpenTelemetry y Jaeger

Abrí **[Jaeger](http://127.0.0.1:16686)** después de `stack.py up`. Creá un run nuevo en la consola, elegí servicio `harness-worker`, operación `worker.attempt` y filtrá por `run.id`. Cada intento tiene su propia traza, enlazada con el productor API y el intento anterior. Podés ver modelo, políticas, herramientas y requests TLS a emisor/Jira.

La instrumentación exporta metadatos permitidos, sin prompts, resultados, identidades ni credenciales. Jaeger usa memoria: reiniciarlo pierde trazas; los checkpoints/evidencia siguen en PostgreSQL. Su caída no bloquea el run ni su recuperación. Fuera de Compose, tracing está desactivado por defecto. Ver la sección 27 de `docs/guide.md` para recorrer una pausa/recuperación y entender parent/links, privacidad y pruebas.

## Renovación programada y rotación en vivo

`stack.py up` inicia el planificador local: revisa cada minuto, renueva certificados cuando quedan siete días, rota firma cada 30 días y completa retiros/recuperaciones pendientes. `down` lo detiene. Corre mientras el host y los servicios estén disponibles; no instala un servicio del sistema.

```bash
uv run python scripts/security_live.py status
uv run python scripts/security_live.py renew-certificates
uv run python scripts/security_live.py rotate-signing-key
# Después del solapamiento de 90 segundos:
uv run python scripts/security_live.py retire-signing-key
# Si una transición quedó pendiente:
uv run python scripts/security_live.py recover
```

TLS recarga para handshakes nuevos y deja terminar conexiones abiertas. La rotación anuncia la pública y espera a Jira antes de cambiar el firmante. El controlador en vivo no reinicia emisor/Jira/worker. Los materiales se entregan cifrados por rol y se validan antes de sustituir el snapshot. Ver sección 26 de `docs/guide.md` para fases, política persistida, backoff y límites de continuidad.

El procedimiento coordinado anterior sigue en `scripts/security.py`. Las generaciones históricas conservan materiales privados bajo `data/runtime/`: requieren protección y retención propia.

## Abrir el primer frontend (M3a / M3b)

```bash
uv sync --locked
uv run python scripts/build_jenkins.py
uv run python -m harness.web
```

Abrí **[http://127.0.0.1:8000](http://127.0.0.1:8000)**. La consola permite ejecutar siete escenarios, ver evidencia/evaluación/presupuestos e interrumpir y recuperar runs desde el navegador. Usa la base SQLite habitual y el mismo core, políticas y adaptadores MCP de la CLI. No requiere Node, un build frontend ni API keys.

![Consola local del harness](docs/images/frontend-m3b.png)

Para practicar, elegí **Build de otra revisión → Después de GitHub → Iniciar ejecución → Recuperar run**. Para un informe parcial, elegí **Jenkins no disponible** y la pausa tras su fallo.

La app corre localmente con identidad sintética y una ejecución a la vez. No es autenticación multiusuario ni un worker durable. La CLI persistida y la web no pueden usar simultáneamente la misma base: detené el servidor antes de ejecutar comandos persistidos sobre ella. El bloqueo se libera al cerrar la app. Podés elegir otra base con `--db` u otro puerto con `--port`.

## Ejecutar M0

Desde la carpeta de este proyecto, con Python 3.13 o posterior y `uv`. Para la suite completa, primero prepará el binario Go con las instrucciones de Jenkins más abajo:

```bash
uv sync --locked
uv run python -m harness
uv run python -m harness --scenario denied
uv run python -m unittest discover -s tests -v
```

El escenario `read` termina en `COMPLETED` y devuelve `DEV-123: IN_REVIEW` con su referencia ficticia. `denied` termina en `FAILED` con código `FORBIDDEN`, cero invocaciones al adaptador y salida de proceso 1: es el rechazo esperado, no un error de instalación.

M2 agrega el SDK oficial de MCP para Python como dependencia, fijada en `uv.lock`. `unittest` sigue siendo el ejecutor de pruebas; usá el entorno preparado por `uv sync --locked`.

## Guardar y recuperar con SQLite

```bash
uv run python -m harness start --interrupt-at after-result
uv run python -m harness status UUID_DEL_RUN
uv run python -m harness resume UUID_DEL_RUN
```

Reemplazar `UUID_DEL_RUN` por el `run_id` del primer comando. La interrupción sale con código 2 y es esperada. El resultado de Jira queda guardado; al recuperar, `adapter_invocations_this_process` es cero. La base está en `data/runtime/harness.sqlite3`, excluida de Git.

También se puede interrumpir en `before-tool` o `before-model`. Son lecturas y decisiones ficticias: un intento sin resultado confirmado consume otra reserva al retomarse. No recuperamos escrituras automáticamente y el proceso original debe haber terminado antes de ejecutar `resume`.

## Tiempo activo global

```bash
# Agotar el presupuesto deliberadamente con adaptadores falsos lentos.
uv run python -m harness --active-timeout 0.2 --demo-delay 0.3 start

# Guardar el tiempo, interrumpir después de la lectura y recuperar el mismo run.
uv run python -m harness --active-timeout 2 --demo-delay 0.05 start --interrupt-at after-result
uv run python -m harness --demo-delay 0.05 resume UUID_DEL_RUN
```

El presupuesto global es 120 segundos por defecto; cada llamada recibe el mínimo entre su timeout y el remanente del run. El tiempo activo se conserva en SQLite y la pausa entre procesos no se cobra. Una llamada interrumpida sin resultado confirmado imputa su reserva de manera conservadora, visible en `recovered_time_seconds`. No es un deadline duro de tiempo real.

Los runs anteriores sin medición siguen siendo consultables, pero no se reanudan con un presupuesto inventado. Ver la guía para los campos y límites de la recuperación.

## Primera herramienta MCP (M2)

```bash
uv run python -m harness --tool-backend mcp start
uv run python -m harness --tool-backend mcp start --interrupt-at after-result
uv run python -m harness resume UUID_DEL_RUN
uv run python -m harness --tool-backend mcp --scenario denied start
```

El host inicia el servidor local automáticamente. La recuperación selecciona el backend guardado y reutiliza el resultado confirmado: cero conexiones MCP nuevas después de `after-result`. La denegación esperada produce `FORBIDDEN` sin abrir conexión. No se conecta a Jira real ni requiere credenciales. Ver en la guía el cliente explícito, la validación del protocolo y los límites de identidad de esta fixture.

## Jira → GitHub y recuperación entre herramientas

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github start
uv run python -m harness --tool-backend mcp --workflow jira-github start --interrupt-at after-result
uv run python -m harness resume UUID_DEL_RUN
```

Jira aporta el vínculo al PR 42 de `demo/atlas`; GitHub devuelve estado y revisión `revision-A`, con referencias de ambos sistemas. Los permisos del proyecto Jira y del repositorio GitHub son independientes. Todo usa datos sintéticos y servidores MCP locales: no requiere tokens ni consulta APIs externas.

Después de interrumpir tras Jira, recuperar solo conecta GitHub. Agregá `--interrupt-after-results 2` al comando de interrupción para confirmar ambos resultados antes de parar: recuperar no abre ninguna conexión MCP. El perfil guardado restaura el recorrido automáticamente; los runs anteriores siguen con Jira solamente.

## Jenkins en Go y revisión del build

Compilar el servidor antes de iniciar el recorrido (Go 1.27.3 en PATH o `.tools/go`; ya preparado en este entorno):

```bash
uv run python scripts/build_jenkins.py --test
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins start
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario stale start
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario failed start
```

`assessment.status` devuelve `VERIFIED`, `STALE_BUILD` o `BUILD_FAILED`. El core compara repositorio, job, revisión y resultado; el modelo no puede convertir un build verde de otra revisión en evidencia válida. `COMPLETED` significa que terminó la evaluación, no que CI pasó ni que se autoriza un despliegue.

El camino normal usa cuatro decisiones y tres herramientas. Para recuperar después de GitHub, usá `start --interrupt-at after-result --interrupt-after-results 2`; después de Jenkins, elegí 3. `resume UUID_DEL_RUN` conserva escenario, límites y resultados, sin repetir consultas confirmadas.

El SDK Go queda fijado en `services/jenkins-mcp/go.mod`/`go.sum`; binarios, toolchain y cachés se excluyen de Git. Jenkins sigue siendo ficticio. `--build-scenario unavailable` genera ahora un informe parcial con Jira/GitHub y `assessment.status: INCOMPLETE`; los perfiles CI v1 conservan el comportamiento anterior. Ver la guía para instalación, decisiones y recuperación de la lectura pendiente.

## Informe parcial cuando Jenkins no está disponible

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario unavailable start
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario unavailable start --interrupt-at after-tool-failure
uv run python -m harness resume UUID_DEL_RUN
```

Los runs nuevos usan `mcp-devops-ci-v2:<escenario>`. Si Jenkins devuelve indisponibilidad explícita o vence su timeout individual mientras queda presupuesto global, se guardan dos resultados y un `ToolFailure`; el informe termina COMPLETED con evaluación INCOMPLETE. No afirma que CI pasó o falló: revisión y éxito del build quedan desconocidos (`null`). La ausencia no recibe una cita Jenkins inventada.

Después de `after-tool-failure`, recuperar no abre ninguna conexión MCP. Los permisos se revalidan para resultados y fallos guardados; deadline global, presupuestos, cancelación y errores de permisos/schema siguen bloqueando. La clasificación general `MCP_UNAVAILABLE` también se mantiene bloqueante. Los runs CI v1 conservan su semántica original al recuperar.

## S04: contenido malicioso y decisiones adversariales

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --content-scenario injection start
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --content-scenario injection --attack job start
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --content-scenario injection --attack shell start
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --content-scenario injection --attack claim --build-scenario stale start
```

La descripción Jira lleva instrucciones maliciosas como datos externos. `job` fuerza una consulta a un job no permitido: FORBIDDEN. `shell` fuerza una herramienta inexistente: UNKNOWN_TOOL. Ambas salidas 1 son rechazos esperados antes de despachar. `claim` fuerza una afirmación falsa de CI exitoso; el finalizador conserva STALE_BUILD o INCOMPLETE según la evidencia.

El escenario usa un modelo adversarial guionado y un perfil v3 que se conserva al recuperar; no ejecuta comandos del texto ni lee secretos. Las pruebas fuerzan acciones del modelo para comprobar los límites del backend, no la resistencia estadística de un LLM real. `--content-scenario benign` ofrece el mismo contrato con texto normal.

**M2 queda cerrado en su alcance simulado. M3a (API local) y M3b (frontend mínimo) ya funcionan.** M3c incorporará PostgreSQL/worker/operación entre servicios. El frontend usa los mismos controles y no otorga permisos desde el navegador.

## M3c, primer incremento: PostgreSQL y trabajos pendientes

```bash
uv sync --locked
uv run python scripts/postgres.py up
uv run python -m harness.postgres enqueue
uv run python -m harness.postgres status
uv run python -m unittest discover -s integration_tests -p test_postgres.py -v
```

Requiere un motor Docker activo. Si usa un contexto distinto del predeterminado, indicá `scripts/postgres.py --context NOMBRE_CONTEXTO up` la primera vez; se recuerda localmente sin cambiar la preferencia global.

Compose inicia PostgreSQL 18.6 en `127.0.0.1:55432`, con volumen propio. Generamos credenciales locales excluidas de Git y un rol de aplicación sin superusuario. `enqueue` confirma run y job en una transacción; si falla el job se revierte el run. El trabajo queda PENDING hasta que un worker independiente lo reclame.

La web predeterminada continúa usando SQLite; `--storage postgres` activa el productor/consulta de jobs descrito en la sección siguiente. Para comprobar persistencia, ejecutá `scripts/postgres.py down`, luego `up` y consultá `harness.postgres status`: ambos IDs deben conservarse. `down` conserva el volumen y los archivos locales de credenciales.

## Worker independiente y frontend PostgreSQL (M3c)

```bash
uv run python scripts/postgres.py up
uv run python scripts/build_jenkins.py
```

En dos terminales:

```bash
uv run python -m harness.web --storage postgres --port 8001
```

```bash
uv run python -m harness.worker
```

Abrí **[http://127.0.0.1:8001](http://127.0.0.1:8001)**. La API solo admite/consulta trabajos; el worker ejecuta el mismo core. Los jobs se reclaman con lease renovable, token creciente y presupuesto de intentos. Los checkpoints exigen el token vigente y la finalización del run/job se confirma junta.

La pantalla conserva sus escenarios y pausas, y muestra el procesamiento independiente. Un job PAUSED requiere **Recuperar run**; un RUNNING con lease vencido puede retomarse automáticamente. Los runs SQLite y su modo local se conservan por separado.

Las pruebas fuerzan caídas reales y cambios de propietario. El fencing protege los checkpoints; no promete una sola llamada remota ni habilita escrituras. Identidad de desarrollo y MCP stdio siguen siendo el alcance actual.

![Frontend con worker independiente](docs/images/frontend-worker-m3c.png)

## Orden de lectura

1. [Alcance y escenarios](01-alcance-y-escenarios.md).
2. [Arquitectura](02-arquitectura.md).
3. [Modelo de amenazas](03-modelo-de-amenazas.md).
4. [Contratos y estados](04-contratos-y-estados.md).
5. [Guía de construcción y decisiones de M0](docs/guide.md).

## Qué contiene el código

| Archivo | Responsabilidad |
| --- | --- |
| `harness/contracts.py` | Tipos de dominio, interfaces y contratos puros de propuesta, lease y operación. |
| `harness/core.py` | Ciclo propio: reservar, consultar, validar, autorizar, ejecutar y terminar. |
| `harness/fakes.py` | LLM guionado, Jira ficticio, política de lectura y store en memoria. |
| `compose.yaml` | PostgreSQL y perfil app con API/worker, secretos y volumen. |
| `Dockerfile`, `.dockerignore` | Imagen Python/Go Linux y contexto de build acotado. |
| `harness/container_entrypoint.py` | Preparar credencial privada y abandonar root antes del servicio. |
| `scripts/stack.py` | Operar la app completa con Compose. |
| `harness/postgres_store.py` | Store, productor atómico y claims/leases/fencing. |
| `harness/worker.py` | Consumidor independiente, heartbeat y recuperación. |
| `harness/checkpoints.py` | Pausas didácticas compartidas por los modos local/worker. |
| `harness/migrations/` | Schema SQL versionado de runs/jobs. |
| `harness/postgres.py`, `scripts/postgres.py` | Ejercicio de admisión y operación de Compose. |
| `harness/storage.py` | Store SQLite, serialización y reglas compartidas de actualización/recuperación. |
| `harness/evidence.py` | Comparación determinista e informes completos/parciales con evidencia. |
| `harness/adapters/mcp_jenkins.py` | Cliente Python del binario Go y normalización de builds. |
| `services/jenkins-mcp/` | Servidor Go de lectura, SDK fijado y pruebas propias. |
| `scripts/build_jenkins.py` | Compilación y pruebas Go con cachés locales. |
| `harness/timing.py` | Acumulado monotónico y reservas para el presupuesto activo global. |
| `harness/fixtures.py` | Descripciones sintéticas benignas y maliciosas. |
| `harness/profiles.py` | Versiones y configuración persistida de los escenarios CI. |
| `harness/composition.py` | Dependencias y adaptadores compartidos por CLI y API. |
| `harness/web.py` | API local, ejecución serial y servicio de archivos frontend. |
| `harness/ownership.py` | Un dueño local por base, compartido por CLI/API. |
| `frontend/` | Primera consola HTML/CSS/JavaScript, sin build separado. |
| `harness/__main__.py` | Composición de dependencias y escenarios de consola. |
| `harness/adapters/mcp_read.py` | Cliente MCP compartido, descubrimiento y ciclo de vida. |
| `harness/adapters/mcp_jira.py`, `harness/adapters/mcp_github.py` | Contratos y normalización por proveedor. |
| `harness/mcp_server.py` | Servidor stdio compartido y validación de llamadas. |
| `harness/jira_mcp_server.py`, `harness/github_mcp_server.py` | Servidores separados con backends ficticios y autorización. |
| `harness/mcp_contract.py` | Schema cerrado de la primera herramienta. |
| `tests/` | Pruebas locales, incluidas sesiones MCP reales, sin APIs o claves. |

El recorrido puede consultar Jira, continuar con su PR en GitHub y comparar con un build Jenkins. SQLite conserva estado, contadores, resultados y tiempo. M2 agrega un transporte MCP real; el modelo y los datos siguen siendo ficticios. La API y el frontend funcionan con ejecución SQLite local o worker independiente sobre PostgreSQL. La operación empresarial entre servicios sigue pendiente. Los contratos de aprobación y operaciones externas inciertas siguen aislados del recorrido y no ejecutan escrituras.

**Validación actual: 126 pruebas habituales, 36 de PostgreSQL real, 5 de Compose y las 2 pruebas Go previas.** Incluyen permisos por proyecto/repositorio en host y servidor, schemas, errores sanitizados, timeout, cancelación, recuperación entre herramientas y conservación del recorrido. M2 cubre S01–S04 con modelo/backends simulados; la plataforma objetivo todavía no está completa.

### Alertas e incidente local

Prometheus vigila exporters, snapshot y vencimiento TLS. Grafana alerta ante cola pendiente sostenida. [La guía, sección 29](docs/guide.md#m3c-alertas-e-incidente-investigable) explica condiciones, recuperación e investigación de dos intentos en Jaeger. Alertmanager en http://127.0.0.1:9093 agrupa ambas fuentes sin notificaciones externas.

```bash
uv run python scripts/incident.py run
uv run python scripts/incident.py status
# Si el ejercicio quedó interrumpido:
uv run python scripts/incident.py recover
```

El ejercicio detiene temporalmente solo el worker del laboratorio; requiere cola inactiva y ningún otro consumidor. Restaura el servicio y comprueba la evidencia Jira sin duplicarla. El journal privado queda en `data/runtime/`, ignorado por Git.

### Alcance y próximos hitos

La [matriz de cierre](docs/status.md) contrasta M0–M7 y S01–S12 con la implementación. M4 (acciones aprobadas/efectos inciertos) y M5 (conocimiento) siguen pendientes; la observabilidad local de M6 se adelantó. El próximo incremento propuesto es una línea base de evals deterministas, seguida de M4 por pasos.
