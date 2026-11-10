# M3d4 cola y worker de escrituras autenticadas

[Índice](../guide.md) · [← Anterior](41-m3d3-propuestas-y-aprobaciones-con-identidad-autenticada.md) · [Siguiente →](43-m3d5-resolucion-manual-autenticada-de-unknown.md)


### Paso 1 habilitar admisión sin enviar desde la API

M3d3 terminaba en una operación PREPARED. Ahora el dueño autenticado puede pedir su procesamiento con `POST /api/operations/<id>/dispatch`. La API revalida autoridad y guarda un trabajo único en PostgreSQL, con respuesta **202**. El proveedor no recibe la solicitud durante esa petición HTTP.

Reutilizamos `WriteQueue` y `WriteWorker`: cuota de dieciséis trabajos activos por solicitante/workspace, máximo de cinco reclamos, heartbeat, fencing y la relación única entre job y operación. Repetir una admisión con el mismo punto de fallo devuelve el mismo trabajo; no reinicia contadores. Cambiarlo en un reintento produce conflicto.

El scope sigue siendo el del usuario autenticado: otro actor/workspace no puede encolar ni consultar su ledger. Un approver no recibe capacidad de despacho por haber aprobado. `authenticated_dispatch`/`dispatch_enabled` indican ahora que está disponible la admisión por cola, no un envío síncrono.

### Paso 2 distinguir autoridad humana y autoridad del servicio

La parte más importante es decidir qué comprobamos antes de enviar y qué necesitamos para recuperar después.

| Acción | Autoridad necesaria |
| --- | --- |
| Preparar y admitir | Usuario autenticado, ownership, aprobación y permisos actuales. |
| Iniciar el primer request | Permisos actuales del solicitante y aprobador, aprobación vigente, evidencia/revisión válidas y reclamo de servicio vigente. |
| Confirmar un recibo de un request ya aceptado | Servicio limitado a la operación de su partición y guard/fencing vigentes. |
| Consultar DISPATCHING/UNKNOWN | Mismo servicio, ID/hash original y controles del ledger. No habilita otro request. |
| Crear/aprobar otra intención | Usuarios autorizados; el servicio no puede hacerlo. |

Revocar una sesión no cancela una intención durable. Retirar permisos sí impide un **nuevo envío**, pero tampoco deshace uno que el proveedor ya aceptó. Exigir al usuario seguir habilitado para registrar aquel recibo dejaría el ledger sin una verdad que ya podemos comprobar.

Esta separación conserva el control final antes del efecto descrito en [OWASP Transaction Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html), y añade una autoridad explícita de servicio para el seguimiento del resultado. La consulta nunca transforma ausencia de recibo en permiso para reenviar.

### Paso 3 representar un alcance de servicio sin fabricar permisos del usuario

`harness/user_write_worker.py` configura el servicio con actor y workspace desde la CLI administrativa local. Su `Identity` de partición tiene roles y recursos vacíos: sirve para identificar qué ledger puede atender, no para conceder el permiso del solicitante.

`ServiceWriteOperations` permite leer y actualizar ese ledger usando el guard del job y verificando la integridad de la propuesta. Sus consultas siguen filtrando actor/workspace. No acepta una identidad diferente, no prepara propuestas y no permite dispatch/reconcile sin un guard. El factory también rechaza el schema de fixtures `harness_lab`.

Para autorizar el primer envío obtiene al usuario actual del directorio y delega en la validación completa de `Operations`: rol operator, recursos, aprobador distinto/vigente, workspace, vencimiento, versión/hash de evidencia, política/configuración y revisión. No inventamos una segunda política de aprobación.

El proveedor MCP recibe un sobre de servicio fijo y acotado a DEV, demo/atlas y atlas-tests para la partición configurada. Sus campos de actor/workspace vinculan el registro del simulador al dueño de la operación; **no son una sesión humana ni una delegación OAuth**. No se envía el bearer del usuario por MCP ni se almacena en jobs. El proceso local y su acceso PostgreSQL/MCP son una frontera administrativa confiable del laboratorio; todavía no representan IAM administrado de producción.

### Paso 4 comprobar dos veces antes del primer I/O

La primera validación ocurre al reclamar PREPARED para registrar DISPATCHING. El servicio vuelve a comprobar autoridad justo antes de llamar al proveedor, además de verificar estado/versión del ledger y el guard vigente. Esto cubre una retirada de permisos entre ambos pasos.

Si la autoridad falla antes de invocar al proveedor, el intento termina NOT_APPLIED con el código correspondiente. En ese recorrido sabemos que no se hizo request. Si hay un error de identidad/directorio antes del envío también se deniega; no sustituimos el usuario por el fixture ni enviamos con permisos en caché. La intención queda cerrada sin efecto, y no se relanza sola al volver el directorio.

Si la llamada al proveedor ya empezó y su resultado no es fiable, se conserva UNKNOWN según las reglas anteriores. Un timeout no demuestra ausencia. La diferencia es el punto en que ocurrió el fallo, no una interpretación optimista del mensaje de error.

No hay una transacción distribuida entre permisos y el proveedor: retirar un grant después del último control no revierte I/O iniciado. El límite remoto `not_after` sigue siendo el menor entre vencimiento de aprobación, lease del ledger y lease observado del job. Heartbeat y fencing no amplían ese límite de aceptación ya fijado.

### Paso 5 conservar el tipo de operaciones y registrar al servicio

`Operations.with_guard()` concentra la construcción de operaciones ligadas al job. La variante de servicio sobrescribe ese método para conservar su scope y su forma de resolver autoridad; el worker no la degrada accidentalmente al modo de usuario al crear el guard.

El historial ahora distingue responsabilidades:

```text
PREPARED     → reader-demo
DISPATCHING  → service:write-worker
UNKNOWN      → service:write-worker
SUCCEEDED    → service:write-worker
```

La aprobación sigue registrada con el actor de su credencial. Los eventos de procesamiento no aparentan una nueva acción humana ni requieren que su sesión siga activa. El servicio continúa sujeto a la versión del ledger y al propietario/token/lease del job; un proceso obsoleto no confirma resultados.

### Paso 6 iniciar el ejecutor autenticado

La API sigue en 8005 con el mismo modo:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
```

Dejá activo también el worker de lectura de M3d2. En otra terminal iniciá el nuevo worker de escrituras:

```bash
uv run python -m harness.user_write_worker --actor reader-demo --workspace workspace-alpha
```

No usa `--session`. El servicio revalida permisos actuales cuando necesita iniciar el efecto. Por defecto atiende `harness_users_lab` y el proveedor privado `jenkins-authenticated-simulator.sqlite3`. El worker M4 anterior permanece en su schema/proveedor de desarrollo.

Para procesar solo un reclamo añadí `--once`. SIGTERM/Ctrl-C dejan terminar el intento actual y detienen nuevas admisiones del consumidor. El código reutiliza los mecanismos de parada/heartbeat del worker; no instala un servicio del sistema ni una automatización.

### Paso 7 encolar una operación aprobada

Creá un run nuevo y una propuesta, revisala/aprobala con la otra credencial y prepará la operación siguiendo la sección 41. Usá una aprobación vigente: una intención preparada antes puede haber agotado sus treinta minutos.

Con la sesión del operador:

```bash
uv run python scripts/user_proposals.py --session SESION_OPERADOR dispatch UUID_DE_OPERACION
uv run python scripts/user_proposals.py --session SESION_OPERADOR operation UUID_DE_OPERACION
```

La primera respuesta 202 confirma admisión. `write_job` permite ver estado, intentos y resultado; la segunda consulta muestra el ledger y su historial. La bandeja/revisión de la propuesta también incluye un resumen del trabajo para quienes tienen acceso a esa propuesta, sin exponer propietario interno, fencing ni traceparents.

Para observar primero PENDING, dejá el worker apagado hasta encolar. Al iniciarlo debe terminar DONE/SUCCEEDED con una solicitud del simulador. Repetir dispatch no crea otro job ni resetea la operación; los permisos de la petición siguen siendo necesarios para acceder a ella.

### Paso 8 recuperar una respuesta perdida con permisos retirados

Para el ejercicio, admití una operación nueva con:

```bash
uv run python scripts/user_proposals.py --session SESION_OPERADOR dispatch UUID_DE_OPERACION --interrupt after-send
```

El primer reclamo hace un request, el simulador lo guarda y se omite la confirmación local. El trabajo vuelve a PENDING con `not_before` hasta el vencimiento del lease del ledger. El siguiente reclamo consulta el mismo ID/hash y termina SUCCEEDED/DONE. No ejecuta request otra vez.

La prueba de esta etapa deshabilita tanto al solicitante como al aprobador y revoca sus sesiones después de aquel efecto. HTTP devuelve 401, pero un runtime nuevo del servicio encuentra el recibo y confirma el ledger. El resultado fue dos reclamos y **una sola solicitud persistida**. Restaurar los grants para futuros ejercicios no cambia ese historial.

También probamos muerte real del proceso de servicio con salida 17 después del efecto, antes de guardar el resultado local. Otro runtime recupera por lookup aunque el solicitante ya esté deshabilitado. Los flags de caída se usan en pruebas aisladas; no son la forma normal de detener el worker.

### Paso 9 no convertir ausencia en reintento

Con `--interrupt before-send`, el ledger puede quedar DISPATCHING aunque no haya request. Tras el vencimiento, el servicio consulta y, sin prueba suficiente, conserva UNKNOWN y aparca el job en NEEDS_REVIEW. No marca NOT_APPLIED por una búsqueda vacía ni genera un UUID nuevo.

Mientras exista incertidumbre, se conservan las restricciones anteriores sobre nuevas escrituras al recurso. La cola no vuelve a reclamar automáticamente un trabajo aparcado ni borra su contador de intentos.

La revisión/cierre manual autenticados todavía deben conectarse a esta autoridad de servicio y a otro usuario revisor; constituyen el siguiente incremento. No usamos el flujo de fixtures de 8003 para saltar la frontera del schema autenticado. La revisión manual anterior sigue probada y disponible en su modo de desarrollo.

### Paso 10 investigar el recorrido y verificar controles

Para enviar las trazas del servicio al Jaeger local, antes de arrancarlo:

```bash
export HARNESS_TRACING=1
export HARNESS_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
```

Buscá `harness-user-write-worker` y el tag `operation.id`. Se conservan los links entre productor/reclamos y la propagación al servidor MCP. En la traza de recuperación real se verificó únicamente `jenkins_find_request`, con spans del servicio y `harness-jenkins-write`.

El dashboard existente mantiene sus exporters de desarrollo; sus snapshots no incluyen automáticamente el schema de usuarios. En esta etapa verificamos el nuevo trabajo mediante la API/ledger y Jaeger. La instrumentación de intentos del worker se conserva, pero conectar un exporter/scrape propio del servicio autenticado queda pendiente; no sumamos sus estados a los de otro schema.

Las diez integraciones nuevas prueban admisión sin envío, dos consumidores, sesión revocada, permisos retirados antes y después del efecto, control final tras DISPATCHING, scope distinto, falta de guard, fencing de un propietario obsoleto, muerte real y UNKNOWN sin recibo. Las pruebas de frontera comprueban que el sobre de servicio no incluya la credencial humana y que la CLI haga solo una admisión con un header privado.

```bash
uv run python -m unittest discover -s tests -p 'test_user_write_worker.py' -v
uv run python -m unittest discover -s integration_tests -p 'test_user_write_worker.py' -v
uv run python scripts/evaluate.py
uv run python scripts/evaluate_writes.py
```

Una ejecución completa detectó una prueba antigua de caída de lectura dependiente de un lease de medio segundo; al repetirla aislada pasó. Reforzamos las dos pruebas relacionadas con un lease de prueba de cinco segundos y espera del vencimiento según PostgreSQL, sin cambiar el lease de producción. La aserción sigue exigiendo una caída real y recuperación con la misma evidencia/contadores.

### Paso 11 estado del ejercicio y siguiente paso

La verificación real terminó SUCCEEDED/DONE con dos reclamos, un request persistido, eventos de servicio y lectura original 4/3. Se restauraron los grants originales y se revocaron todas las credenciales del ejercicio. El reporte privado vive en `data/runtime/m3d4-user-write-verification.json`; no se creó una copia de esta guía fuera del proyecto.

La API autenticada, el worker de lectura y el servicio de escritura de alpha quedan disponibles para practicar. La propuesta preparada del incremento anterior permanece sin job si no fue admitida: arrancar el servicio no inventa una intención de envío.

El próximo incremento conectará la **resolución manual autenticada de UNKNOWN**, con evidencia de cierre, otro actor revisor y guard/versión vigentes. Login web/OIDC, frontend multiusuario, acceso autorizado a observabilidad y contratos de Jenkins real siguen pendientes.

Validación M3d4: **225 pruebas locales, 136 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Diez integraciones nuevas cubren recuperación tras deshabilitar ambos actores, muerte real del servicio después del efecto, propietario obsoleto, control final y aparcado sin recibo. La prueba HTTP real terminó SUCCEEDED/DONE con dos reclamos y un solo request, con los grants retirados durante la recuperación y la lectura 4/3 intacta. Jaeger confirmó únicamente jenkins_find_request en el segundo reclamo. La suite completa se repitió con éxito tras reforzar dos pruebas antiguas de lease corto de lectura. Se restauraron los grants y se revocaron las credenciales de la verificación. Sin operaciones inciertas ni jobs de escritura activos en ambos schemas. Las suites Compose/rotación no se repitieron. Gitleaks no encontró secretos en los archivos publicables; los reportes permanecen en runtime ignorado.
