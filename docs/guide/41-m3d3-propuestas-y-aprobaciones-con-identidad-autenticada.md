# M3d3 propuestas y aprobaciones con identidad autenticada

[Índice](../guide.md) · [← Anterior](40-m3d2-lecturas-autenticadas-ownership-y-cola.md) · [Siguiente →](42-m3d4-cola-y-worker-de-escrituras-autenticadas.md)


### Paso 1 sustituir al aprobador asignado por la API

La consola M4 de desarrollo usa dos fixtures desde código; una persona podía practicar ambos papeles sin autenticarse. En el modo de usuarios 8005, la decisión ahora pertenece al actor de la **credencial de esa petición**.

El nuevo recorrido permite crear, revisar, aprobar/rechazar y preparar una operación. El despacho autenticado todavía no está habilitado: no se crea un trabajo de escritura ni se acepta `/dispatch`. Cerramos primero la frontera de aprobación; el siguiente incremento conectará el ejecutor de efectos y su autoridad de recuperación.

No confundimos aprobar con enviar. Como referencia de diseño, [OWASP Transaction Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html) distingue el control de autorización del control final antes de ejecutar. Aquí seguimos preservando esa separación; las comprobaciones para el despacho autenticado deberán completarse antes de habilitarlo.

### Paso 2 reutilizar las propuestas y el ledger

`harness/authenticated_proposals.py` conecta las rutas nuevas con `ProposalStore` y `Operations`; no define otra máquina de estados. Usa el mismo schema de runs autenticados, `harness_users_lab`, y las migraciones del módulo de propuestas.

Se conservan estos controles:

- Intención inmutable vinculada a actor, workspace, recurso, revisión, evidencia y vencimiento.
- Request key UUID estable del cliente para repetir una creación sin duplicar la propuesta.
- Decisión sobre hash y versión exactos, con actor distinto al solicitante.
- Consumo atómico de una propuesta APPROVED en una operación única PREPARED.
- Historial del ledger y UUID de la operación conservados entre reintentos.

Las versiones siguen siendo core 3, usuarios 1 y propuestas 5. No cambiamos los presupuestos del run original ni repetimos sus lecturas.

El proveedor de este recorrido usa otra SQLite privada, `data/runtime/jenkins-authenticated-simulator.sqlite3`. Preparar solo consulta su revisión por MCP; no llama `request`. No comparte los efectos ni el estado del simulador anterior de M4 en 8003.

### Paso 3 definir quién puede revisar y decidir

| Acción | Identidad y alcance |
| --- | --- |
| Crear propuesta | Operator, sobre un run propio del mismo workspace con evidencia completa. |
| Ver su propuesta | Solicitante con los grants necesarios. |
| Revisar propuestas de otros | Approver del mismo workspace y recursos autorizados. |
| Aprobar/rechazar | Approver autenticado distinto al solicitante, hash/versión exactos. |
| Preparar la operación | Solicitante operator, con aprobación vigente y permisos actuales de ambos. |
| Consultar directamente el ledger | Solicitante operator; ownership se mantiene. |
| Enviar al proveedor | No habilitado en la API autenticada de este incremento. |

La bandeja `GET /api/proposals` muestra hasta veinte propuestas recientes: propias para el operator, o del workspace para un approver con grants. Cada elemento vuelve a pasar por la autorización del store. Otro operator del mismo workspace no obtiene acceso por conocer el UUID.

El approver puede revisar el contenido inmutable de la propuesta: argumentos, revisión, fuentes y referencia/hash de evidencia. Esto no le concede acceso al historial entero de runs de otro actor. Las rutas `/api/runs` mantienen la política de lectura anterior.

Un usuario con roles operator y approver tampoco puede aprobar su propia propuesta. La comparación es por actor persistido, no por el nombre del rol ni por usar una segunda credencial de ese mismo actor.

### Paso 4 comprobar permisos actuales y fallar sin consumir

La API autentica cada petición. Antes de decidir vuelve a resolver los grants del revisor y comprueba que correspondan a la identidad recibida. Para una nueva aprobación, también comprueba que el solicitante siga habilitado como operator y autorizado para el recurso. El rechazo puede cerrar una propuesta cuyo solicitante ya perdió permisos: no concede autoridad para ejecutar.

Al preparar la operación, `Operations` acepta un resolver opcional de principales actuales. En este modo comprueba otra vez al solicitante y obtiene al aprobador real desde el directorio. Después aplica las validaciones existentes de roles, recursos, workspace, actor distinto, plazo, evidencia y revisión.

Si se retiró el rol del aprobador tras APPROVED, preparar devuelve 403: no crea una operación, no consume la aprobación y no consulta MCP. Si el directorio no puede resolver identidad, devuelve 503 y conserva el estado anterior. No usa el aprobador fijo de desarrollo como respaldo.

Los reintentos de una operación ya preparada devuelven la misma intención; eso no amplía el vencimiento ni autoriza un envío futuro sin otra revalidación. La autoridad deberá comprobarse nuevamente antes del efecto, no inferirse de que existe PREPARED.

El resolver es opcional para conservar la compatibilidad de los ejercicios M4 anteriores. El worker conserva ese resolver al construir sus operaciones ligadas al guard; no lo descarta al clonar el servicio. Su ejecutor de fixtures rechaza el schema reservado de usuarios mientras el despacho autenticado siga deshabilitado.

### Paso 5 iniciar el recorrido

Usamos la API y worker de lectura de M3d2:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
# En otra terminal:
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha
```

Emití dos credenciales locales distintas:

```bash
uv run python scripts/users.py issue --actor reader-demo --workspace workspace-alpha
uv run python scripts/users.py issue --actor approver-demo --workspace workspace-alpha
```

Conservá sus IDs como `SESION_OPERADOR` y `SESION_APROBADOR`. Son placeholders de los comandos siguientes, no variables configuradas por el script. Los tokens siguen en archivos 0600 de runtime y no se imprimen. La emisión continúa siendo una operación administrativa local; no agregamos contraseñas ni login web/OIDC.

Con la sesión del operador:

```bash
uv run python scripts/user_runs.py --session SESION_OPERADOR start --scenario stale
uv run python scripts/user_runs.py --session SESION_OPERADOR status UUID_DEL_RUN
```

Esperá COMPLETED antes de crear la propuesta. Este incremento sigue usando backends ficticios y decisiones guionadas; no hace escrituras DevOps reales.

### Paso 6 crear una intención y revisar el contenido

Generá un UUID de request key una sola vez, por ejemplo:

```bash
uv run python -c 'from uuid import uuid4; print(uuid4())'
```

Copialo como `UUID_DE_SOLICITUD` y reutilizalo si se pierde la respuesta o repetís el mismo comando:

```bash
uv run python scripts/user_proposals.py --session SESION_OPERADOR propose UUID_DEL_RUN --request-key UUID_DE_SOLICITUD
```

La respuesta contiene `proposal_id`, `payload`, `payload_hash`, `version`, `status` y `expires_at`. El actor/requested_by viene de la sesión del operador, no del body. Repetir con la misma request key devuelve la misma propuesta y vencimiento.

Ahora cambiá a la credencial del aprobador:

```bash
uv run python scripts/user_proposals.py --session SESION_APROBADOR inbox
uv run python scripts/user_proposals.py --session SESION_APROBADOR review UUID_DE_PROPUESTA
```

Revisá job, repositorio, revisión, solicitante, workspace, fuentes y plazo dentro del payload. Copiá el hash y la versión de ese contenido revisado. El cliente no hace un GET y una aprobación automática escondidos en un solo comando.

### Paso 7 decidir con la identidad correcta

```bash
uv run python scripts/user_proposals.py --session SESION_APROBADOR decide UUID_DE_PROPUESTA --decision APPROVED --payload-hash HASH_REVISADO --version 0
```

Para una propuesta pendiente inicial la versión es cero; usá siempre la que devolvió review. Para rechazar, sustituí APPROVED por REJECTED. El servidor registra `decided_by` desde el usuario autenticado y el momento desde PostgreSQL.

Con la sesión del solicitante, ese mismo intento devuelve 403 aunque tenga ambos roles. Un hash o una versión diferentes devuelven 409; un `actor_id` agregado al body devuelve 422. El cliente devuelve código de salida 1 ante esos rechazos esperados.

Repetir exactamente una decisión ya confirmada conserva el resultado y el actor, sin extender el plazo. Cambiar una aprobación ya registrada por rechazo no es una actualización libre: el conflicto se rechaza.

### Paso 8 consumir una vez y observar el ledger

Volvé a la sesión del operador:

```bash
uv run python scripts/user_proposals.py --session SESION_OPERADOR prepare UUID_DE_PROPUESTA
uv run python scripts/user_proposals.py --session SESION_OPERADOR operation UUID_DE_OPERACION
```

La propuesta queda CONSUMED y la operación PREPARED. Su primer evento registra al solicitante. Repetir prepare devuelve el mismo operation ID; no crea otra intención ni otra autorización.

Un approver que no es el dueño no puede preparar en nombre del solicitante. Puede seguir viendo el resumen de la operación dentro de la propuesta que tiene permiso para revisar, pero no adquiere la capacidad del operador para su ledger.

`dispatch_enabled` y `authenticated_dispatch` permanecen en false. No existe una ruta de envío en este modo: POST `/api/operations/<id>/dispatch` devuelve 404 y la tabla write_jobs continúa vacía. El único I/O MCP de prepare es una lectura de revisión. No uses el worker de fixtures para saltar este límite; su constructor/CLI rechazan el schema reservado de usuarios.

### Paso 9 investigar cambios de permisos y resultados

Las pruebas retiran el rol del aprobador después de APPROVED y verifican que preparar no consume la propuesta. También retiran el rol operator del solicitante antes de aprobar: la aprobación se rechaza, mientras otro actor autorizado puede rechazarla para cerrar la intención.

Si lo practicás, guardá/restaurá explícitamente los grants con `scripts/users.py set-membership`. Bootstrap no los restablece. Revocar una sesión impide seguir usando esa credencial, pero no cambia quién aprobó históricamente ni convierte una propuesta consumida en reutilizable.

Después del ejercicio, revocá las credenciales de prueba:

```bash
uv run python scripts/users.py revoke SESION_OPERADOR
uv run python scripts/users.py revoke SESION_APROBADOR
```

En el recorrido HTTP real se verificó creación idempotente, autoaprobación rechazada, otro workspace sin acceso, hash alterado rechazado, aprobación por approver-demo y denegación de prepare con sus permisos retirados. Tras restaurarlos se consumió una sola propuesta en una operación PREPARED. Quedaron cero jobs y cero solicitudes remotas; la lectura original conservó 4/3 llamadas. Se revocaron todas las credenciales del ejercicio. La operación preparada permanece como evidencia durable del recorrido, todavía sin envío.

### Paso 10 pruebas y siguiente frontera

```bash
uv run python -m unittest discover -s tests -p 'test_authenticated_proposals.py' -v
uv run python -m unittest discover -s integration_tests -p 'test_authenticated_proposals.py' -v
uv run python scripts/evaluate.py
uv run python scripts/evaluate_writes.py
```

Los tests cubren dos credenciales, autoaprobación con ambos roles, ownership y workspace, permisos retirados, error del directorio, hash/versión/campos alterados, rechazo persistido, idempotencia y consumo único. Se comprueba que no haya trabajos ni efectos remotos en este modo, y que la CLI envíe solo la decisión/hash/versión revisados sin exponer el token.

El próximo incremento conectará la cola y el worker de escrituras autenticadas. Deberá distinguir autorización vigente para iniciar un efecto de la autoridad del servicio para confirmar o investigar uno ya enviado, incluso si luego se retiran permisos o vence la sesión. Seguiremos usando el ledger para no repetir una escritura incierta. Login web/OIDC, frontend multiusuario y acceso operativo a trazas siguen pendientes.

Validación M3d3: **222 pruebas locales, 126 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Siete integraciones nuevas y tres pruebas de frontera/cliente verifican dos credenciales, alcance, permisos actuales, idempotencia y consumo único. La prueba HTTP real dejó una operación PREPARED, cero jobs/envíos y la lectura 4/3 intacta. No quedaron sesiones activas de prueba, schemas temporales de esta etapa ni operaciones inciertas en ambos modos; la intención preparada permanece como evidencia sin despacho. Las suites Compose/rotación no se repitieron. Gitleaks no encontró secretos en los archivos publicables nuevos/modificados; runtime sigue excluido.
