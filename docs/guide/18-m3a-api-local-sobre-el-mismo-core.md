# M3a API local sobre el mismo core

[Índice](../guide.md) · [← Anterior](17-m2-s04-contenido-malicioso-y-cierre-del-hito.md) · [Siguiente →](19-m3b-primer-frontend-del-harness.md)


### Paso 1 elegir el alcance del servidor

Ahora agregamos una API local y un frontend operable sobre lo que ya probamos. Conservamos SQLite, los perfiles versionados y los tres proveedores ficticios por MCP. No sustituimos todavía la operación local por PostgreSQL, cola durable o workers distribuidos.

```text
Navegador → API FastAPI → composición compartida → Harness
                                ↓                    ↓
                       SQLite / checkpoints     MCP Python / Go
```

Usamos FastAPI para declarar contratos HTTP y Uvicorn para servir la app. Las versiones resueltas quedan en `uv.lock`; agregamos `httpx` al grupo de desarrollo para pruebas HTTP. Ver [la documentación de archivos estáticos](https://fastapi.tiangolo.com/tutorial/static-files/) y [ciclo de vida](https://fastapi.tiangolo.com/advanced/events/) del framework.

**Decisión:** una API Python con los archivos del frontend en el mismo origen. No necesitamos otro servidor de desarrollo, Node, un bundler ni una instalación frontend para aprender este primer recorrido. HTML/CSS/JavaScript alcanzan para esta pantalla; la elección de un framework futuro sigue abierta.

### Paso 2 compartir composición con la consola

Extraemos `harness/composition.py`. `compose` recibe perfil, identidad, store, límites y hook de checkpoints, y construye el modelo guionado, las políticas y los adaptadores correspondientes. Devuelve `Execution`, con el harness y contadores de sus adaptadores.

Tanto la CLI como la web usan esa función. No implementamos otro loop de agentes ni otra comparación del build en la API. Los perfiles v1/v2/v3 conservan sus reglas de recuperación y la identidad sintética continúa teniendo permisos explícitos por proyecto, repositorio y job.

También extraemos `authorize_run` del core: recuperaciones y lecturas HTTP reautorizan resultados, llamadas pendientes y fallos guardados con la misma política. La propiedad del run se comprueba en el store; poseer su UUID no alcanza para obtener datos de otro actor/workspace.

### Paso 3 declarar contratos HTTP pequeños

`harness/web.py` define estas rutas:

| Método y ruta | Función |
| --- | --- |
| `GET /` | Pantalla del laboratorio. |
| `GET /assets/...` | CSS, JavaScript y favicon del frontend. |
| `GET /api/config` | Catálogo fijo de escenarios permitidos. |
| `POST /api/runs` | Crear un run y admitirlo para ejecución local. |
| `GET /api/runs` | Hasta 30 runs recientes visibles para esta identidad. |
| `GET /api/runs/{run_id}` | Estado, resultados, errores, evaluación y presupuestos. |
| `POST /api/runs/{run_id}/resume` | Recuperar un run con su configuración original. |
| `GET /api/openapi.json` | Contrato OpenAPI generado. |

Para iniciar:

```json
{"scenario": "stale", "pause_at": "github"}
```

El cliente elige un escenario conocido y una pausa didáctica. No puede proporcionar roles, workspace, actor, perfil interno, comandos o límites. Pydantic usa `extra='forbid'` y tipos estrictos: un campo desconocido devuelve `INVALID_REQUEST` sin reflejar su contenido en el error.

Para recuperar se envía `{}`. La API carga perfil y límites del run guardado. No hay un formulario de roles ni parámetros para ampliar permisos al recuperar.

`202 Accepted` significa que el servidor admitió la ejecución. El resultado se consulta después: un run admitido puede terminar FAILED por permisos o producir una evaluación INCOMPLETE. No confundimos el éxito de la solicitud HTTP con el éxito de CI.

### Paso 4 decidir quién ejecuta y cómo evitar duplicados

`RunService` administra tareas `asyncio` dentro del proceso web. Una exclusión `asyncio.Lock` permite un solo run en ejecución; los demás esperan. Admitimos hasta 16 tareas entre activas y pendientes, y rechazamos más con `QUEUE_FULL` antes de crear otro run.

La tarea es independiente de la respuesta HTTP. Cerrar o recargar la pestaña no inicia otro intento ni cancela el run. La pantalla consulta el estado periódicamente. Un segundo pedido de recuperación mientras el run está activo devuelve `RUN_BUSY`.

Los límites se guardan ya al crear el run, incluso antes de que empiece a ejecutar. La espera en la fila local no consume tiempo activo; ese tiempo empieza cuando el core ejecuta el run. El mismo core reserva y contabiliza cada llamada, incluido el ciclo MCP completo.

**No es una cola durable.** Las tareas en memoria no sobreviven a un reinicio del servidor. Los estados y checkpoints sí quedan en SQLite y pueden recuperarse manualmente desde la pantalla. No reclamamos automáticamente todos los runs viejos al iniciar.

En un cierre normal de la app cancelamos las tareas y esperamos su limpieza. Un run que ya estaba ejecutando puede quedar CANCELLED según las reglas del core; uno que esperaba sin empezar puede quedar QUEUED. Una terminación abrupta puede dejar RUNNING y una reserva pendiente, que se tratan como en M1. La pausa didáctica es la forma controlada de practicar recuperación sin matar el servidor.

### Paso 5 coordinar la CLI con la app local

Agregamos `harness/ownership.py`: un bloqueo de archivo del sistema operativo, mantenido durante toda la sesión que usa la base. La CLI persistida y la API adquieren el mismo bloqueo. Si hay otro dueño, fallan con `DATABASE_IN_USE` antes de ejecutar.

Esto evita que la consola recupere un run que sigue ejecutándose en la web o que dos servidores locales trabajen sobre la misma base. No sustituye leases, fencing o coordinación distribuida de M3c. Está implementado con `fcntl` para el entorno macOS/Linux de este laboratorio.

**Uso práctico:** mientras la web usa la base habitual, consultá y recuperá desde la pantalla. Para volver a los comandos CLI persistidos sobre esa misma base, detené primero el servidor web. Los demos en memoria y las pruebas con bases temporales siguen siendo independientes. El archivo `.owner.lock` queda excluido de Git; el bloqueo se libera al cerrar el proceso, aunque el archivo permanezca.

### Paso 6 mantener la identidad en el servidor

La identidad de desarrollo es fija y se construye en el servidor con los mismos permisos del laboratorio. No autenticamos personas distintas ni conectamos OIDC en este incremento. La app se inicia en `127.0.0.1`, con un único proceso, para uso local.

El navegador no tiene autoridad para adjudicar roles. Las rutas de lectura filtran por actor/workspace y reautorizan la evidencia. Si retiramos el acceso al job, no se devuelve su run detallado y se omite del historial visible.

Para las solicitudes que modifican estado exigimos un encabezado propio de la consola, verificamos Origin cuando existe y rechazamos solicitudes de navegador marcadas cross-site. También limitamos Host a direcciones locales, no habilitamos CORS y no emitimos cookies de autenticación.

Ese encabezado protege la frontera del navegador; **no es una credencial o autenticación empresarial**. Cualquier proceso local con acceso a este servidor puede usar la identidad de desarrollo. No desplegamos esta versión como servicio compartido. M3c/M7 deberán incorporar los mecanismos de identidad y servicio del diseño.

La app entrega solo sus archivos frontend, no la carpeta completa del proyecto. La política de contenido limita scripts, estilos y conexiones al propio origen y bloquea frames y objetos. Las respuestas HTTP no se cachean y los errores hacia el cliente contienen códigos sanitizados.
