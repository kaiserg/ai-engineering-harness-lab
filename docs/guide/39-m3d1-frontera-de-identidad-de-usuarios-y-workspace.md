# M3d1 frontera de identidad de usuarios y workspace

[Índice](../guide.md) · [← Anterior](38-m4h-evaluaciones-deterministas-de-escrituras.md) · [Siguiente →](40-m3d2-lecturas-autenticadas-ownership-y-cola.md)


### Paso 1 separar autenticación de autorización

Hasta M4h la API asigna `reader-demo` y `approver-demo` desde código. Eso sirve para practicar ledger y recuperación, pero no demuestra quién está usando la aplicación.

Autenticar responde «¿qué identidad está presentando esta credencial?». Autorizar responde «¿qué puede hacer esa identidad en este workspace?». El siguiente cambio debe preservar esta separación y el contrato `Identity` que ya conoce el core.

Este primer incremento construye y verifica esa frontera de forma aislada. Añadimos un directorio persistido, credenciales locales y un endpoint `GET /api/me` en el puerto **8004**. Devuelve actor, workspace y permisos resueltos por el servidor. Las APIs/frontend de **8002/8003 siguen usando sus fixtures de desarrollo**; no están protegidas por esta nueva credencial todavía. El endpoint lo declara con `business_routes_connected: false`.

La siguiente parte conectará esa identidad al flujo de negocio. No incorporamos una pantalla que aparente login antes de tener comprobada su frontera de confianza.

### Paso 2 persistir usuarios y pertenencias

`harness/user_identity.py` contiene `UserIdentityStore`, y `harness/migrations/users/001_identity.sql` crea:

| Tabla | Qué guarda |
| --- | --- |
| `user_accounts` | Actor y habilitación global. |
| `user_memberships` | Pertenencia actor/workspace, habilitación, roles y recursos autorizados. |
| `user_sessions` | ID administrativo, hash de credencial, actor/workspace, creación, vencimiento y revocación. |

La migración usa su propio `user_schema_version=1` y lock transaccional. La versión del core PostgreSQL sigue en 3 y la del módulo de propuestas en 5. No cambiamos runs, operaciones, aprobaciones ni jobs.

El bootstrap explícito crea tres identidades ficticias:

| Actor | Workspace | Roles iniciales |
| --- | --- | --- |
| `reader-demo` | `workspace-alpha` | reader, operator |
| `approver-demo` | `workspace-alpha` | approver |
| `reader-beta` | `workspace-beta` | reader, operator |

Los recursos iniciales son los fixtures DEV, demo/atlas y atlas-tests. Repetir bootstrap no restaura roles o cuentas deshabilitadas: usa inserciones idempotentes, no un reset de permisos. Su salida confirma preparación, no pretende mostrar los roles actuales.

### Paso 3 usar una credencial sin roles dentro

La emisión genera 32 bytes aleatorios mediante `secrets`, representados como un token opaco con prefijo `hsu_`. El token no contiene actor, roles ni workspace decodificables. La base guarda solamente su SHA-256; el cliente conserva la credencial original para presentarla.

Hash rápido aquí no significa almacenar contraseñas humanas con SHA-256: estamos identificando un secreto aleatorio de 256 bits. No implementamos contraseñas, recuperación de cuenta ni MFA.

El servidor fija un vencimiento absoluto de una hora con el reloj de PostgreSQL. Cada verificación lee la sesión, cuenta y pertenencia vigentes. No usa una caché que mantenga permisos retirados. Revocar una sesión no afecta las otras; deshabilitar la cuenta o pertenencia rechaza todas las credenciales correspondientes.

Esto sigue la separación entre identificador aleatorio y estado mantenido en servidor descrita por [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html). Es una credencial educativa administrada localmente; **no es un login OIDC ni un sistema empresarial de identidad completo**. El flujo futuro de autenticación deberá establecer una identidad validada antes de crear/resolver la sesión; no aceptaremos un actor elegido libremente en una API de emisión.

### Paso 4 mantener la emisión en una frontera administrativa

`scripts/users.py` es una CLI local con acceso confiable al PostgreSQL del laboratorio. Puede preparar fixtures, emitir/revocar credenciales y actualizar roles de una pertenencia existente. La API no tiene un endpoint público que emita credenciales al recibir un nombre.

Quien puede ejecutar esa CLI y usar las credenciales de administración local está dentro de la frontera de confianza del laboratorio. Esto no sustituye roles de administración independientes, auditoría empresarial o un IdP. No lo confundimos con autenticar a usuarios remotos.

El token se escribe en `data/runtime/user-sessions/<session_id>.json`, con permiso 0600; el directorio nuevo se crea con 0700. No aparece en stdout, argumentos de shell, query string ni `.env`. El archivo se crea de forma exclusiva y sin seguir enlaces simbólicos. Si falla la escritura, se intenta revocar la sesión recién emitida antes de devolver error.

El ID administrativo sí puede mostrarse y usarse para revocar; no funciona como credencial. Todos los archivos de runtime ya están ignorados por Git y contienen material privado generado.

### Paso 5 comprobar identidad por HTTP local

`harness/user_identity_api.py` expone únicamente la comprobación de identidad. El cliente presenta `Authorization: Bearer ...`; la respuesta se construye desde `Identity`, no desde headers de actor/rol ni desde parámetros enviados por el navegador.

| Situación | Respuesta |
| --- | --- |
| Credencial válida y vigente | 200, identidad y permisos actuales. |
| Ausente, falsificada, vencida, revocada o cuenta/pertenencia deshabilitada | 401, UNAUTHENTICATED. |
| Credencial válida pero `X-Harness-Workspace` no coincide con su workspace | 403, FORBIDDEN. |
| Parámetros extra en la URL autenticada | 422, INVALID_REQUEST. |
| PostgreSQL no disponible o error inesperado | 503, IDENTITY_UNAVAILABLE; sin identidad de respaldo. |

Cada credencial está vinculada a un workspace al emitirse. No se cambia su scope enviando otro header. Si en el futuro un usuario pertenece a varios workspaces, cada selección deberá resolverse con pertenencia validada por el servidor.

Un token de servicio MCP no se acepta como credencial de usuario. La identidad de servicio actual prueba qué proceso llama a otro servicio; esta credencial prueba posesión de un secreto de usuario local. Se conservan como fronteras distintas.

La API solo se inicia en `127.0.0.1`, no habilita CORS ni cookies y devuelve `Cache-Control: no-store`. Desactivamos el access log de este proceso para no registrar URLs de peticiones malformadas. El cliente CLI usa un destino fijo, no sigue redirects y no usa proxies del entorno. HTTP se limita a este ejercicio loopback; no habilitamos despliegue remoto con estas credenciales.

### Paso 6 ejecutar el ejercicio

Desde la raíz, con las dependencias instaladas y PostgreSQL activo:

```bash
uv run python scripts/postgres.py up
uv run python scripts/users.py bootstrap
uv run python -m harness.user_identity_api
```

Dejá esa terminal abierta. En otra, emití la credencial:

```bash
uv run python scripts/users.py issue --actor reader-demo --workspace workspace-alpha
```

La salida incluye `session_id`, `expires_at` y `credential_file`, sin el token. Copiá el session ID y reemplazá `UUID_DE_SESION` en los ejemplos siguientes:

```bash
uv run python scripts/users.py whoami UUID_DE_SESION
uv run python scripts/users.py whoami UUID_DE_SESION --workspace workspace-beta
```

El primero devuelve 200 con reader-demo/workspace-alpha. El segundo devuelve 403 y el comando sale con código 1: es el rechazo esperado. El cliente lee el archivo privado, arma el header y muestra solo la respuesta sanitizada.

Para ver cómo cambian los permisos sin emitir otro token:

```bash
uv run python scripts/users.py set-membership --actor reader-demo --workspace workspace-alpha --roles reader
uv run python scripts/users.py whoami UUID_DE_SESION
```

Ahora solo aparece reader. Restaurá explícitamente el grant para seguir los próximos ejercicios:

```bash
uv run python scripts/users.py set-membership --actor reader-demo --workspace workspace-alpha --roles reader operator
```

Esto modifica el directorio nuevo; en este incremento todavía no altera los operadores fijos de la API M4 ni de sus workers. La integración de ambas fronteras es el siguiente trabajo.

Finalmente, revocá y repetí:

```bash
uv run python scripts/users.py revoke UUID_DE_SESION
uv run python scripts/users.py whoami UUID_DE_SESION
```

Debe devolver 401. Repetir la revocación es idempotente. La CLI no borra automáticamente el archivo privado; una credencial revocada ya no sirve y podés retirarla del runtime cuando termine el ejercicio. Para otro usuario de prueba, repetí issue con approver-demo/alpha o reader-beta/beta.

### Paso 7 preservar los controles y probar los rechazos

La verificación devuelve el mismo contrato `Identity` del core. No reescribimos el ledger, no cambiamos los UUID de intención ni los límites y no reiniciamos aprobaciones. La revocación se observa en verificaciones posteriores a su commit; no cancela una petición que ya estaba en curso ni revierte un efecto remoto.

Las pruebas PostgreSQL cubren persistencia/reapertura, solo hash en la base, scope, token falso/de servicio, vencimiento con el reloj de la base, cuenta y pertenencia deshabilitadas, cambio de roles, revocación idempotente e independencia de sesiones. Las pruebas HTTP demuestran que headers de actor/roles no cambian la identidad, que no existe emisión pública ni rutas de negocio y que la respuesta nunca muestra la credencial.

También verificamos permisos 0600, rechazo de sobrescritura/symlink, revocación si falla el archivo, secretos fuera de stdout, cliente sin redirects/proxies y fallo cerrado con 503 cuando no se puede resolver identidad.

```bash
uv run python -m unittest discover -s tests -p 'test_user_identity.py' -v
uv run python -m unittest discover -s integration_tests -p 'test_user_identity.py' -v
uv run python scripts/evaluate.py
uv run python scripts/evaluate_writes.py
```

El ejercicio real por HTTP comprobó las tres identidades, rechazos cruzados entre alpha/beta, cambio de roles y revocación. Se revocaron todas las credenciales emitidas para esa verificación. El servidor queda disponible para que emitas las tuyas de prueba.

### Paso 8 siguiente conexión y límites pendientes

El siguiente incremento conectará identidad verificada y permisos vigentes a las rutas de lectura, ownership y cola. Después adaptaremos la aprobación para que el aprobador sea el usuario autenticado, conservando dos actores y evidencia/ledger. Deberemos revisar también cómo el worker obtiene y revalida permisos actuales del solicitante antes del I/O.

Quedan pendientes login web/OIDC, cookies de sesión, renovación, MFA, permisos administrativos separados, acceso autorizado a observabilidad y operación multiusuario completa. En este paso no se protege retroactivamente la consola M4 por levantar el puerto 8004; ambas rutas siguen claramente separadas.

Validación M3d1: **216 pruebas locales, 111 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Se verificó HTTP real con tres identidades, rechazo cross-workspace, cambios de roles y revocación; todas las credenciales del ejercicio quedaron revocadas. No quedaron schemas de pruebas de identidad, trabajos de escritura pendientes/activos ni operaciones inciertas. Se conservan los monitores/servicios anteriores y no se repitieron suites Compose/rotación. Gitleaks no encontró secretos en los archivos nuevos/modificados publicables; runtime y credenciales siguen excluidos.
