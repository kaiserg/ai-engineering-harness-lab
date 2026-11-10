# M4c despacho y consulta del efecto por MCP

[Índice](../guide.md) · [← Anterior](32-m4b-ledger-y-despacho-jenkins-simulado.md) · [Siguiente →](34-m4d-propuestas-y-operaciones-desde-api-y-frontend-local.md)


### Paso 1 cambiar el transporte conservando el ledger

El recorrido ahora es:

```text
CLI → Operations → MCPJenkinsWrite (cliente SDK)
                      ↓ stdio
               servidor MCP Python
                      ↓
               JenkinsSimulator → SQLite independiente

Operations → PostgreSQL: propuesta, ledger y eventos
```

`Operations` conserva su implementación: decide autorización, consumo, estados y recuperación. `MCPJenkinsWrite` implementa los mismos métodos `revision`, `request` y `lookup` que el proveedor directo. El servidor MCP ejecuta el simulador persistente en otro proceso.

No trasladamos el ledger al servidor, no compartimos una transacción entre PostgreSQL y SQLite y no añadimos otro loop de agente. Esa separación permite perder una respuesta después del commit remoto sin perder la capacidad de investigar.

### Paso 2 elegir una fachada pequeña

El servidor de escritura es Python porque envuelve el backend SQLite ya probado; el MCP Jenkins Go de lectura sigue funcionando sin cambios. Portar simultáneamente el almacenamiento y el transporte introduciría otra variable para estudiar el mismo fallo. Este incremento no convierte el backend ficticio en Jenkins real.

Archivos nuevos:

- `harness/jenkins_write_contract.py`: schemas y validación de argumentos/metadatos.
- `harness/jenkins_write_mcp_server.py`: servidor stdio con SDK MCP existente.
- `harness/adapters/mcp_jenkins_write.py`: cliente y normalización del resultado.

No hay dependencias nuevas ni una migración del ledger. El servidor usa el mismo archivo `data/runtime/jenkins-write-simulator.sqlite3`, por lo que los recibos previos permanecen consultables. Cada conexión puede iniciar otro proceso sin borrar esa base.

### Paso 3 exponer solamente tres herramientas

| Herramienta MCP | Argumentos | Resultado |
| --- | --- | --- |
| `jenkins_current_revision` | Objeto vacío | Revisión actual del workspace fijado por el host. |
| `jenkins_request_tests` | operation ID, hash, argumentos aprobados y límite de aceptación | Referencia de cola del mismo operation ID. |
| `jenkins_find_request` | operation ID y hash | Referencia existente o `null`. |

Los schemas son cerrados (`additionalProperties: false`) y limitan el job/repositorio/revisiones a los fixtures. UUID y hash tienen formato acotado; la fecha debe incluir zona horaria. No aceptamos argumentos de actor, roles, workspace, URL, script ni ruta de base de datos desde la herramienta.

El host selecciona identidad y archivo mediante la configuración del proceso stdio. El servidor reautoriza el acceso antes de invocar el backend; el simulador conserva además sus propias comprobaciones. Esta identidad local de desarrollo no equivale a OAuth/OIDC ni a identidad remota de producción.

La herramienta de escritura **no se agrega a `READ_TOOLS`, a la política de lectura ni a las herramientas del modelo**. El ejecutor la usa tras validar la aprobación; listar una herramienta en MCP no concede acceso al modelo ni elimina las reglas del host. El hash vincula datos; no es una credencial ni prueba criptográfica de aprobación ante un servidor independiente.

### Paso 4 conectar el SDK sin cambiar el ejecutor síncrono

El ledger actual tiene una interfaz síncrona y el cliente MCP oficial es asíncrono. Usamos `start_blocking_portal` de AnyIO —ya presente como dependencia del SDK— para ejecutar la conexión en un loop de otro thread. Esto funciona incluso si un consumidor ya tiene un event loop, sin intentar anidar `asyncio.run` dentro de él.

Cada invocación:

1. Valida identidad y argumentos en el host.
2. Inicia el servidor y realiza la conexión MCP.
3. Descubre herramientas y exige exactamente los nombres/schemas esperados.
4. Hace una sola llamada mediante `session.call_tool`, sin un bucle de reintentos ni fallback automático.
5. Cierra la conexión y normaliza el resultado.

El timeout de cuatro segundos cubre conexión, descubrimiento y llamada dentro del contexto asíncrono; también configuramos el timeout de lectura del SDK y desactivamos su caché para este cliente. La cancelación/limpieza de un transporte no demuestra que el proveedor no produjo un efecto: seguimos usando UNKNOWN cuando no hay resultado fiable.

El límite absoluto aprobado/lease se transmite como `not_after`. El proveedor lo comprueba antes de aceptar una solicitud nueva, independientemente de que el cliente siga esperando o ya haya cancelado. No ampliamos los diez segundos de lease ni los treinta minutos de aprobación para acomodar el transporte.

### Paso 5 validar la respuesta, no solo el éxito del protocolo

El cliente exige `schema_version: 1`, un objeto de datos con campos exactos y, para solicitudes/búsquedas, el mismo operation ID y hash enviados. La referencia debe ser exactamente `queue:operation_id`.

Una solicitud nunca puede confirmar éxito con `receipt: null`; una búsqueda sí puede devolverlo para expresar ausencia de evidencia. Un schema cambiado, un hash ajeno o una referencia inventada no actualizan el ledger a SUCCEEDED.

Los errores de aplicación usan `isError: true` y un código estructurado permitido. Un error ambiguo/malformado se convierte en MCP_TOOL_ERROR, sin publicar mensajes crudos. El ledger conserva su clasificación: solo los rechazos con garantía conocida STALE_PRECONDITION/DISPATCH_EXPIRED producen NOT_APPLIED tras entrar al proveedor. Un fallo de transporte o resultado inválido deja UNKNOWN; no lo convertimos en «no pasó nada».

### Paso 6 ejecutar con MCP por defecto

Con PostgreSQL activo y una propuesta aprobada/vigente:

```bash
uv run python scripts/operations.py prepare PROPOSAL_ID
uv run python scripts/operations.py dispatch OPERATION_ID
uv run python scripts/operations.py show OPERATION_ID
```

Los comandos ahora usan MCP por defecto. No necesitás levantar manualmente el servidor: el SDK administra su proceso stdio por conexión. `prepare` consulta revisión por MCP; `dispatch` envía por MCP; `reconcile` consulta por MCP. `show` lee PostgreSQL y no necesita invocar al proveedor.

Para comparar con M4b, el adaptador directo sigue disponible explícitamente:

```bash
uv run python scripts/operations.py --transport direct show OPERATION_ID
```

Cambiar el transporte no cambia IDs, tablas ni las reglas del ledger. El comando `fixture-head` sigue siendo un control local del fixture; no expusimos una herramienta MCP para cambiar revisiones. Tampoco agregamos endpoints ni pantallas nuevas a la app.

### Paso 7 recuperar la interrupción del host

Usá una operación PREPARED nueva, como en la sección 32:

```bash
uv run python scripts/operations.py dispatch OPERATION_ID --interrupt after-send
```

El backend confirmó el efecto y MCP devolvió el recibo, pero el comando se interrumpe antes del commit local. Queda DISPATCHING. Esperá aproximadamente once segundos desde el envío:

```bash
uv run python scripts/operations.py reconcile OPERATION_ID
uv run python scripts/operations.py show OPERATION_ID
```

El ledger pasa por UNKNOWN y el nuevo proceso MCP encuentra la solicitud por el mismo ID/hash. No vuelve a llamar `jenkins_request_tests`. Repetir `dispatch` después de SUCCEEDED devuelve el registro existente.

Ejecutamos ese recorrido con comandos reales en procesos separados: el historial fue PREPARED → DISPATCHING → UNKNOWN → SUCCEEDED, con una sola fila del proveedor para la operación.

### Paso 8 distinguir caída del servidor y caída del host

La prueba de integración también mata el proceso MCP **después del commit SQLite y antes de devolver la respuesta MCP**. Usa una configuración de fallo exclusivamente de laboratorio (`HARNESS_JENKINS_WRITE_FAULT=exit-after-commit` en el entorno del proceso hijo), no un argumento de herramienta ni una opción del modelo.

El host observa la pérdida del transporte y persiste UNKNOWN. Otro cliente inicia un servidor normal, consulta y recupera SUCCEEDED. La prueba bloquea explícitamente `request` durante la reconciliación: si se intentara reenviar, fallaría. El conteo del proveedor termina en uno.

Si el servidor no está disponible al reconciliar, UNKNOWN permanece. Si responde con `null`, UNKNOWN también permanece. La ausencia no habilita un nuevo operation ID, ni desbloquea otras escrituras del recurso. La resolución manual sigue pendiente.

### Paso 9 comprobar fronteras y regresiones

```bash
uv run python -m unittest discover -s tests -p test_write_mcp.py -v
uv run python -m unittest discover -s integration_tests -p test_operations_mcp.py -v
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s integration_tests -v
uv run python scripts/evaluate.py
```

Las seis pruebas nuevas de frontera cubren autorización del servidor sin pasar por la política cliente, campos/job rechazados, identidad distinta, respuestas alteradas, descubrimiento incompatible y timeout de arranque. Las cinco de integración usan PostgreSQL y MCP reales con proveedor SQLite en el proceso hijo: envío/repetición, rechazo tipificado, caída del servidor tras commit, transporte no disponible al consultar e interrupción del host con el lease original.

Las suites anteriores siguen comprobando consumo único, concurrencia, permisos vigentes, vencimiento, comparación de revisión, versiones/eventos y recuperación de lecturas. El core, `Operations`, los stores de runs y los workers no necesitan cambios para usar este adaptador.

### Paso 10 límite y siguiente integración

Todavía no hay un servidor de escritura Jenkins Go, HTTP/TLS para este canal, credenciales Jenkins reales ni autenticación de usuarios. El servidor Python es una fachada stdio confiada del laboratorio. El MCP de lectura Go y la identidad/TLS del canal Jira permanecen como estaban.

La siguiente integración puede exponer el recorrido aprobado desde API/frontend, conservando una identidad efectiva definida por el servidor; la resolución manual de UNKNOWN también necesita un contrato explícito. Las métricas/trazas y evals de escritura se ampliarán al conectar esos recorridos. Cambiar de transporte no cierra por sí solo toda M4.

Validación M4c: **196 pruebas locales, 71 PostgreSQL y 9/9 evaluaciones de lectura aprobadas**. Incluyen seis pruebas nuevas de frontera MCP y cinco de integración ledger/MCP. Se comprobó la caída real del servidor tras commit y la CLI after-send en procesos separados: una solicitud en el proveedor, mismo operation ID y recuperación solo por lookup. No quedaron operaciones de prueba DISPATCHING/UNKNOWN en el entorno. Gitleaks no encontró secretos en los archivos revisados; reporte/base del simulador siguen ignorados.
