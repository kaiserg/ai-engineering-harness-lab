# M3d2 lecturas autenticadas ownership y cola

[Índice](../guide.md) · [← Anterior](39-m3d1-frontera-de-identidad-de-usuarios-y-workspace.md) · [Siguiente →](41-m3d3-propuestas-y-aprobaciones-con-identidad-autenticada.md)


### Paso 1 conectar la identidad al recorrido de lecturas

M3d1 verificaba credenciales en un endpoint aislado. M3d2 conecta esa verificación con crear, listar, consultar y recuperar runs. La API se inicia con `--user-auth`; sin credencial no entra al productor de jobs.

En cada petición a `/api/`, el servidor resuelve la credencial contra `UserIdentityStore`. Las rutas de runs requieren rol reader u operator. Actor y workspace se toman del resultado validado; no se aceptan en el body ni se adjudican desde headers de roles.

`PostgresRunService` se construye por petición con esa identidad, sin compartir un objeto mutable de usuario entre peticiones. Reutilizamos las consultas existentes del store: filtran por **actor y workspace**, no solo por conocer un UUID.

| Petición | Resultado esperado |
| --- | --- |
| Sin credencial, vencida o revocada | 401 antes de admitir trabajo. |
| Usuario válido sin rol de lectura | 403. |
| Solicita otro workspace con su credencial actual | 403. |
| Pide un run de otro actor o workspace | 404; no se confirma si existe. |
| Incluye `actor_id` u otros campos desconocidos en start | 422. |
| Directorio de identidad no disponible | 503 sin volver a la identidad de desarrollo. |

Esta aplicación de los controles en cada petición sigue el criterio de [OWASP Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html). La sesión demuestra identidad; ownership y política siguen determinando qué puede leer o ejecutar.

### Paso 2 separar el almacenamiento del modo de desarrollo

Las consolas anteriores asignan identidades fijas. Si compartieran los nuevos runs, un visitante de esas consolas podría ver trabajo cuyo dueño lógico coincidiera con el fixture. Por eso no basta con añadir autenticación a otro puerto usando el mismo historial.

| Componente | Puerto/schema por defecto | Modo |
| --- | --- | --- |
| Consola Compose | 8002 / harness_lab | Fixtures existentes. |
| Consola M4 | 8003 / harness_lab | Aprobaciones/escrituras de desarrollo. |
| Comprobación M3d1 | 8004 / directorio en harness_lab | Identidad, sin runs. |
| API M3d2 | 8005 / harness_users_lab | Lecturas con credencial de usuario. |

El directorio de cuentas/sesiones sigue centralizado en harness_lab. Los runs/jobs autenticados se guardan en harness_users_lab, creado con las migraciones normales. La API no admite `--user-auth` con el schema de fixtures ni junto con `--approvals-demo`. El schema reservado de usuarios tampoco se puede abrir con la API o el worker de fixtures sin resolución de identidad.

No se copian ni se borran runs anteriores; el historial de desarrollo no se convierte automáticamente en historial autenticado. Es una separación del laboratorio mediante schemas y modos explícitos, no PostgreSQL RLS ni aislamiento de administradores con acceso directo a la base. La CLI/credenciales locales de administración siguen siendo una frontera confiable.

### Paso 3 configurar qué actor y workspace atiende un worker

El worker recibe `--user-actor` y `--user-workspace` desde su configuración local confiable. Eso selecciona la partición de jobs que puede reclamar; no concede roles. Sus permisos efectivos se consultan en el directorio al comenzar **cada reclamo**.

Agregamos `UserIdentityStore.resolve(actor, workspace)`. A diferencia de authenticate, no usa un token del cliente: obtiene pertenencia/cuenta habilitadas y permisos actuales para una intención durable ya admitida. Antes de ejecutar, verifica que la identidad devuelta coincida con actor/workspace del worker y tenga rol de lectura.

Se conserva `Worker`, `claim_next`, heartbeat, fencing y los stores ya probados. No introducimos un segundo core ni un consumidor que cambie de identidad global mientras procesa varias solicitudes. Para el laboratorio usamos un worker explícito por actor/workspace; un dispatcher multiusuario administrado será otro incremento operativo.

### Paso 4 revalidar antes del I/O

El worker existente ya envolvía modelo y herramientas para comprobar el lease. Extendemos esa misma comprobación:

1. Confirmar que el reclamo sigue vigente y pertenece al worker.
2. Resolver la pertenencia actual del solicitante en el directorio.
3. Compararla con la identidad fijada para ese intento.
4. Solo continuar si no cambió; el core conserva sus validaciones por herramienta y recurso.

Se comprueba en checkpoints y justo antes de invocar modelo/herramienta. Si una pertenencia se deshabilita o cambian sus grants, el intento falla de forma conservadora con FORBIDDEN. No mezclamos permisos nuevos con decisiones calculadas bajo los anteriores. El siguiente reclamo obtiene nuevamente los permisos actuales, sin exigir reiniciar el proceso del worker.

La prueba de revocación en `before-tool` demuestra cero invocaciones reales a Jira. La reserva de herramienta ya realizada se conserva: el contador puede ser uno aunque la llamada no haya salido. No borramos reservas para presentar un consumo artificialmente menor ni reiniciamos límites.

Resolver permisos no elimina todas las carreras con un servicio remoto: un cambio posterior a la última comprobación no cancela I/O que ya empezó. No prometemos revocación retroactiva ni atomicidad distribuida. Los efectos de escritura conservan su ledger/corte remoto y todavía no se conectan a este modo.

### Paso 5 distinguir sesión de intención durable

Revocar o dejar vencer la credencial impide nuevas peticiones HTTP. **No cancela por sí mismo un run ya admitido.** El worker no necesita la credencial del navegador para continuar: revalida cuenta/pertenencia y permisos del solicitante.

Esto evita que cerrar una sesión, perder el cliente o llegar a su vencimiento se interprete como rollback o cancelación del trabajo. Para impedir nuevos accesos del usuario y su I/O pendiente, se deshabilita la cuenta/pertenencia o se retiran los permisos; el worker lo detecta en sus comprobaciones posteriores. Una orden explícita de cancelación de trabajo sigue siendo un contrato distinto.

No guardamos el token, hash de sesión ni session ID en runs/jobs. Actor/workspace y el estado de ejecución bastan para el procesamiento; la autenticación del cliente no se convierte en una credencial de servicio MCP.

### Paso 6 iniciar API y worker

Con PostgreSQL, dependencias y el binario MCP Go preparados, en una terminal:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
```

En otra:

```bash
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha
```

Ambos usan el schema de runs autenticados por defecto. El worker de Compose puede seguir funcionando sobre el schema anterior. Los flags de usuario se usan juntos; los comandos rechazan mezclar por accidente los schemas reservados de ambos modos.

La raíz 8005 informa que esta etapa es una API autenticada, con `frontend_login_connected: false`. La pantalla anterior permanece en 8002/8003. Aún no incorporamos login web, cookies, OIDC ni pegado de tokens en el frontend.

### Paso 7 recorrerlo con un cliente que protege la credencial

Emití una sesión con la CLI de M3d1:

```bash
uv run python scripts/users.py issue --actor reader-demo --workspace workspace-alpha
```

Copiá su session ID en lugar de `UUID_DE_SESION`:

```bash
uv run python scripts/user_runs.py --session UUID_DE_SESION me
uv run python scripts/user_runs.py --session UUID_DE_SESION start --scenario matching --pause-at jira
```

El cliente nuevo lee el archivo privado de runtime y envía el token exclusivamente en Authorization. Usa el destino fijo 127.0.0.1:8005, sin proxies ni redirects. En la salida solo aparecen estado HTTP y datos de la aplicación. Los cuerpos contienen escenario/pausa, no actor ni roles.

Con el run ID del segundo comando:

```bash
uv run python scripts/user_runs.py --session UUID_DE_SESION status UUID_DEL_RUN
uv run python scripts/user_runs.py --session UUID_DE_SESION resume UUID_DEL_RUN
uv run python scripts/user_runs.py --session UUID_DE_SESION list
```

Esperá a que status muestre PAUSED antes de recuperar. El mismo resultado de Jira debe conservarse y el run termina COMPLETED con cuatro decisiones y tres herramientas. Si usás el worker con `--once`, repetí el comando para atender el reclamo posterior a resume.

Para probar aislamiento, emití otra sesión reader-beta/workspace-beta e intentá consultar o recuperar el UUID de alpha: devuelve 404. Beta puede crear/ver sus propios runs, pero los adaptadores DevOps conservan fixtures solo de alpha: al ejecutar con un worker beta, termina FAILED/NOT_FOUND. No ampliamos sus datos ni allowlists para hacer aparecer un resultado exitoso. También hay una prueba con dos lectores distintos del mismo workspace para cubrir ownership por actor.

Para dejar un worker beta disponible:

```bash
uv run python -m harness.worker --user-actor reader-beta --user-workspace workspace-beta
```

### Paso 8 probar permisos retirados y recuperación

La prueba automatizada encola un run, cambia el rol del solicitante a approver y luego ejecuta el worker. Debe terminar FAILED/FORBIDDEN con cero decisiones/cero herramientas. Otra prueba retira la pertenencia entre la reserva y la invocación de Jira: conserva la reserva y no despacha.

Si lo practicás manualmente con el worker continuo, puede reclamar antes de que cambies los permisos. Para un ejercicio controlado, dejá el worker apagado, encolá, cambiá el grant y recién entonces iniciá el worker con `--once`. Restaurá los permisos explícitamente al terminar; bootstrap no deshace revocaciones. Un run FAILED por esta política no se vuelve a ejecutar automáticamente al restaurarlos.

El resto de las pruebas verifica credencial ausente, rol insuficiente, workspace/actor incorrectos, campos suplantados, directorio caído y límites/evidencia tras recuperación. Los tests anteriores de fencing, caída de proceso, aprobación y ledger continúan vigentes.

```bash
uv run python -m unittest discover -s tests -p 'test_authenticated_runs.py' -v
uv run python -m unittest discover -s integration_tests -p 'test_authenticated_runs.py' -v
uv run python scripts/evaluate.py
uv run python scripts/evaluate_writes.py
```

### Paso 9 evidencia y siguiente conexión

El recorrido HTTP real de alpha pausó tras Jira y se recuperó con la misma evidencia y conteos 4/3. La credencial beta recibió 404 al consultar/recuperar ese run. Su run propio terminó NOT_FOUND en los fixtures sin datos beta. Un run encolado antes de retirar permisos terminó FORBIDDEN con 0/0; tras restaurarlos pudo consultarse su resultado. Revocar la sesión bloqueó nuevas peticiones con 401. Todas las credenciales de esta verificación quedaron revocadas.

La API 8005 y un worker autenticado de alpha quedan disponibles para emitir tu propia sesión y practicar. Las consolas/workers de desarrollo siguen con su modo anterior.

El siguiente incremento conectará propuestas y aprobación con el **usuario autenticado que realmente decide**, evitando que la API asigne automáticamente el aprobador de prueba. También deberá revalidar los permisos del solicitante y del aprobador antes de escribir. Login web/OIDC, UI multiusuario y acceso autorizado a Jaeger siguen pendientes; no declaramos M3/S09 completos.

Validación M3d2: **219 pruebas locales, 119 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Incluye ocho integraciones nuevas de lectura autenticada y tres tests de frontera/cliente/configuración; las comprobaciones específicas del schema reservado también pasaron tras añadir el guard. La prueba HTTP real conservó evidencia Jira y conteos 4/3, rechazó acceso cruzado y bloqueó I/O con permisos retirados. No quedaron jobs de lectura activos en ninguno de los dos schemas, schemas de pruebas de esta etapa ni credenciales activas de la verificación. La suite Compose/rotación no se repitió. Gitleaks no encontró secretos; los archivos de credenciales y reportes siguen ignorados.
