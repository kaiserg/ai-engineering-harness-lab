# M3c Jira por Streamable HTTP e identidad de servicio

[Índice](../guide.md) · [← Anterior](22-m3c-api-y-worker-en-docker-compose.md) · [Siguiente →](24-m3c-tls-y-emisor-de-identidad-separado.md)


> Esta sección registra el incremento HTTP inicial. La sección 24 endurece los runs HTTP existentes con HTTPS y un emisor separado; Compose ya no entrega la clave de firma al worker.

### Paso 1 separar el proceso y entender el objetivo

Antes, el worker iniciaba Jira como proceso hijo por stdio. Ahora Jira vive como servicio independiente de Compose y recibe mensajes MCP por HTTP. Podemos reiniciarlo sin reiniciar el worker, y necesitamos verificar quién le pide herramientas. GitHub y Jenkins conservan stdio: cambiamos una frontera por vez.

```text
Navegador → API → PostgreSQL (run + job)
                         ↓ claim, lease, fencing
                       Worker
                         ├─ HTTP interno + JWT firmado → Jira MCP → FakeJira
                         ├─ stdio → GitHub MCP → fixture
                         └─ stdio → Jenkins MCP Go → fixture
```

El servidor Jira queda vivo entre consultas. En este primer incremento el cliente abre/cierra una conexión por invocación, igual que el alcance anterior: facilita medir startup, discovery, llamada y cleanup dentro de un único timeout. No agregamos todavía un pool duradero de conexiones. El modelo continúa guionado y las fuentes siguen siendo ficticias.

### Paso 2 identificar al servicio con una firma

`harness/service_identity.py` genera una pareja **Ed25519**:

- Clave privada: solo el worker puede firmar credenciales.
- Clave pública: Jira verifica firmas sin poder crear nuevas credenciales.
- API: conserva su contraseña PostgreSQL, sin recibir ninguna clave MCP.

`scripts/stack.py up` crea los archivos en `data/runtime/`, excluido de Git, con permisos `0600`. No los imprime ni los agrega a la imagen. Una pareja existente se valida y conserva; no hay rotación silenciosa. Si falta una mitad, hay que restaurar la pareja: no regeneramos claves durante una recuperación.

Compose monta los materiales por servicio. El bootstrap los copia al `tmpfs` privado y después abandona root. Jira no recibe la contraseña PostgreSQL ni la clave privada del worker. La imagen sigue siendo común, de solo lectura y sin secretos incorporados.

**¿Qué es un JWT?** Un conjunto de claims firmado. Está codificado, no cifrado: no ponemos contraseñas en su payload. Por conexión, el worker crea uno válido durante 60 segundos con estos campos:

| Campo | Decisión |
| --- | --- |
| `iss` | Emisor local fijo: `harness-lab-worker`. |
| `sub` | Servicio autorizado: `harness-worker`. |
| `aud` | Destinatario exclusivo: `jira-mcp`. |
| `iat`, `nbf`, `exp` | Emisión, inicio y vencimiento; duración máxima de 60 segundos. |
| `scope` | Capacidad solicitada: `jira:read`. |
| `actor`, `workspace` | Delegación sintética, limitada por reglas del servidor. |
| `fixture` | Selector didáctico firmado: descripción benigna o inyección. No concede permisos. |

Jira fija `EdDSA` en el verificador; no elige el algoritmo a partir de lo que solicite el token. Verifica firma, emisor, audiencia y tiempos. Un token válido para otro servicio no sirve aquí. No guardamos tokens en el run, resultados, historial ni navegador. Usamos la biblioteca [PyJWT](https://pyjwt.readthedocs.io/en/stable/usage.html) con soporte criptográfico declarado explícitamente.

**Autenticación no es autorización.** Una firma válida demuestra posesión de la clave de un servicio confiable. Jira además exige servicio/scope/actor/workspace conocidos y construye una `Identity` con sus propios grants: `reader`, proyecto `DEV`, workspace `workspace-alpha`. Claims extra como `roles=admin` o `projects=OTHER` no amplían esos grants. Los argumentos de la herramienta no pueden incluir roles. El backend vuelve a aplicar `ReadOnlyPolicy` al recurso solicitado.

Esto **no es OAuth/OIDC ni login humano**. Es un emisor local dentro del worker, útil para estudiar firma, audience y alcance. En una empresa, un emisor confiable externo y políticas de delegación reemplazarían esta emisión local. El worker comprometido puede firmar por el actor sintético permitido: la allowlist del servidor limita ese alcance, pero no reemplaza protección/rotación/revocación de credenciales.

### Paso 3 usar el transporte oficial

`harness/jira_http_server.py` usa `StreamableHTTPSessionManager` del SDK oficial. `harness/adapters/mcp_jira_http.py` usa `streamable_http_client` y `Client`: seguimos hablando MCP, no inventamos endpoints REST de tools.

El cliente envía `Authorization: Bearer ...` mediante un cliente HTTP dedicado, sin proxies heredados del ambiente ni redirecciones automáticas. La URL procede de configuración confiable del worker, nunca del modelo. Esta versión solo admite hosts del laboratorio y la ruta `/mcp/`.

El servidor es **stateless**: no mantiene una sesión remota que pueda sobrevivir a la credencial. Autentica cada solicitud MCP. Las solicitudes concurrentes mantienen identidad/fixture en un `ContextVar` por request, sin una variable global mutable compartida. El contexto se restablece al terminar. El endpoint sin credencial o con firma/tiempos/audiencia inválidos devuelve `401`; una identidad válida fuera del alcance devuelve `403`.

También validamos Host y Origin cuando está presente, sin habilitar CORS. El endpoint de salud no requiere token y solo expone `status=ok`. Estos controles siguen las recomendaciones del [transporte MCP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

En Compose se usa **HTTP dentro de la red local** y el puerto del host se publica solo en loopback, `127.0.0.1:8010`. La firma no cifra el bearer token: fuera de este laboratorio se necesita TLS, gestión de claves y un emisor de identidad de producción. No expongás este puerto públicamente. No implementamos revocación, rotación con JWKS ni prevención de replay; la credencial corta puede reutilizarse mientras siga vigente.

### Paso 4 mantener los controles del harness

El nuevo adaptador reutiliza `MCPRead.invoke` y `MCPJira._normalize`; cambia la conexión, no las reglas:

1. El host verifica identidad, permiso y argumentos antes de abrir la conexión.
2. El core persiste la reserva de presupuesto antes de hacer I/O.
3. El cliente descubre la herramienta y exige el schema esperado exacto.
4. `session.call_tool` hace la llamada sin el bucle automático de callbacks del cliente.
5. El servidor autentica y vuelve a autorizar el recurso.
6. La respuesta pasa por la misma normalización estricta antes de guardarse.

No agregamos reintentos propios. El deadline del core incluye conexión, discovery, llamada y cierre. La autenticación rechazada es un fallo bloqueante: no entra en la lista de fallos opcionales de Jenkins. El worker conserva sus prechecks de lease, heartbeat, CAS y fencing; un worker viejo no gana permiso para confirmar resultados por usar HTTP.

Una cancelación local no prueba que el servidor remoto haya detenido el trabajo. Por eso seguimos limitados a lecturas, y una lectura pendiente puede repetirse después de una interrupción. No prometemos exactly-once remoto.

### Paso 5 fijar el transporte para recuperar correctamente

Los perfiles `v1`, `v2` y `v3` conservan stdio. La API de Compose marca **solo los runs nuevos** con `mcp-devops-ci-v4:build:contenido:ataque`, que selecciona Jira HTTP. La API guarda esa decisión, pero no firma credenciales ni ejecuta herramientas. La web SQLite y los comandos anteriores conservan su comportamiento.

Al recuperar:

- Si la respuesta Jira quedó confirmada en PostgreSQL, se reutiliza: no se abre otra conexión Jira.
- Si quedó pendiente, se conserva el `call_id`, se carga la reserva huérfana y se cobra otro intento antes de repetir la lectura.
- El JWT se vuelve a emitir desde la identidad confiable actual. Nunca recuperamos una credencial vencida desde el checkpoint.
- El perfil, límites y grants guardados/actuales siguen pasando los controles anteriores.

No movimos estado del run al servidor HTTP. PostgreSQL sigue siendo la fuente durable y el worker sigue siendo dueño del ciclo de ejecución.

### Paso 6 probarlo en la consola

Desde la carpeta del proyecto:

```bash
uv sync --locked
uv run python scripts/stack.py up
```

Abrí **http://127.0.0.1:8002** y creá un run nuevo con `Build coincidente`. El resultado debería seguir siendo `VERIFIED`, con cuatro decisiones del modelo guionado y tres herramientas. Ahora la consulta Jira cruzó HTTP; GitHub/Jenkins cruzaron stdio.

Para practicar recuperación, elegí pausa después de Jira. Su evidencia ya está confirmada. Podés reiniciar el stack con los comandos siguientes; si el laboratorio usa un contexto Docker distinto del predeterminado, el script `stack.py` sigue siendo la vía recomendada para operar toda la app:

```bash
uv run python scripts/stack.py down
uv run python scripts/stack.py up
```

Esto detiene y levanta todos los servicios sin eliminar el volumen. Volvé al historial y recuperá el run pausado: deben conservarse la primera evidencia y los tres intentos totales de tools al completar. `down` no borra claves ni contraseñas.

Una llamada anónima a `http://127.0.0.1:8010/mcp/` debe fallar con `401`. No hace falta copiar un token a mano ni llevarlo al navegador.

### Paso 7 pruebas y límites

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s integration_tests -v
uv run python -m unittest discover -s container_tests -v
```

**Verificación final: 137 pruebas del laboratorio, 36 PostgreSQL y 6 Compose pasaron.**

Agregamos pruebas HTTP reales en puerto temporal con claves temporales: firma/audiencia/emisor/vencimiento, algoritmo fijado, grants del servidor, denegación de argumentos extra, Host/Origin, aislamiento de requests concurrentes, timeout y ambos casos de recuperación. La suite Compose verifica el perfil `v4` durable, el rechazo anónimo, los archivos disponibles para cada servicio y recuperación tras reiniciar Jira. Nunca imprime claves ni tokens.

La suite PostgreSQL conserva las pruebas de muerte del worker y fencing. Las pruebas de Compose reinician servicios y crean runs sintéticos; ejecutarlas sin otro worker que consuma la misma cola.

| Término | Qué aprendimos |
| --- | --- |
| Service identity | Quién es el programa que solicita una herramienta. |
| Bearer token | Quien posee la credencial puede presentarla hasta que venza. |
| Firma asimétrica | Una clave firma y otra verifica sin poder firmar. |
| Audience | Servicio concreto al que va dirigida la credencial. |
| Scope | Capacidad declarada que el servidor limita y valida. |
| Stateless | No conservamos estado de sesión MCP entre solicitudes HTTP. |
| ContextVar | Contexto de una ejecución concurrente, sin compartir identidad mutable. |

El siguiente incremento puede sustituir la emisión local por identidad administrada y sumar trazas entre worker y MCP. Este paso deja preparada la frontera verificable; todavía no conecta proveedores DevOps reales ni un modelo de pago.
