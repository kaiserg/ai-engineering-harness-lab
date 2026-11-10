# M2 primera herramienta por MCP

[Índice](../guide.md) · [← Anterior](12-m1-deadline-global-y-tiempo-activo-persistido.md) · [Siguiente →](14-m2-github-por-mcp-y-recuperacion-entre-herramientas.md)


### Paso 1 agregar el protocolo sin mover los controles

Agregamos `mcp>=2.3.0,<3`; el lockfile fija la versión 2.3.0 y sus dependencias. Se instala con:

```bash
uv sync --locked
```

Usamos el [SDK oficial de MCP para Python](https://github.com/modelcontextprotocol/python-sdk) para transporte, inicialización y mensajes RPC. El core conserva su propio ciclo: el modelo propone, el host valida y autoriza, el adaptador ejecuta y SQLite confirma el resultado. No delegamos presupuestos o permisos al protocolo.

```text
ScriptedLLM → core: validar y autorizar → MCPJira (cliente)
                                           ↕ stdio / MCP
                                    servidor Jira → fixture
                    core ← ToolResult validado ← respuesta MCP
```

Es MCP real entre dos procesos, con un proveedor ficticio. Esta separación permite aprender el límite del servicio sin depender todavía de una cuenta Jira o un modelo pagado.

### Paso 2 definir una herramienta pequeña

`harness/mcp_contract.py` contiene `JIRA_INPUT_SCHEMA`: solo admite `issue_key`, exige su formato y rechaza propiedades adicionales. El nombre autorizado es `jira_get_issue`.

`harness/jira_mcp_server.py` publica ese contrato mediante `tools/list` y atiende `tools/call`. Usamos los callbacks explícitos del servidor del SDK para mantener visible la validación de argumentos. La implementación consulta la misma fixture de M0: `DEV-123`, estado `IN_REVIEW`, evidencia `jira:DEV-123:v1`.

El servidor vuelve a verificar rol, proyecto y workspace. Un cliente que omita la política del host tampoco obtiene acceso. Ambos puntos reutilizan la política del laboratorio; no son dos motores de políticas independientes.

### Paso 3 ubicar el cliente MCP

El primer incremento ubicó el cliente en `harness/adapters/mcp_jira.py`. Al incorporar GitHub extraemos ese transporte común a `harness/adapters/mcp_read.py`; Jira conserva su contrato y normalizador propios:

```python
async with self.client_factory(self.parameters) as mcp_connection:
    listing = await mcp_connection.list_tools()
```

`client_factory` es `Client` del SDK. `parameters` especifica el intérprete Python actual, el módulo del servidor y su entorno. Entrar al contexto inicia el proceso e inicializa la conexión. Salir cierra sus recursos. No necesitás levantar otra terminal.

Después comprobamos que el schema descubierto coincide con el contrato esperado. Descubrir una herramienta no significa autorizarla: la política del host ya tuvo que permitir esta llamada.

La ejecución usa `mcp_connection.session.call_tool(...)`: una solicitud explícita, sin el bucle de callbacks ni reintentos automáticos del cliente de alto nivel. Así los intentos permanecen bajo control del harness. Por ahora abrimos una conexión por llamada; reutilizar conexiones será una decisión posterior con su propio ciclo de vida.

`stdio` conecta entrada y salida de los dos procesos. La salida estándar del servidor transporta el protocolo: no se agregan mensajes de depuración allí. Los diagnósticos deben ir a stderr.

### Paso 4 validar también la respuesta

El adaptador recibe `CallToolResult` y valida `structured_content`: versión 1, campos exactos, ticket solicitado, estado permitido por esta fixture y referencia de evidencia. No intenta reconstruir datos leyendo texto libre.

Después crea el `ToolResult` del dominio con el `call_id` original. MCP correlaciona su solicitud RPC; nuestro `call_id` identifica la llamada dentro del run. Son identificadores de capas distintas.

Los errores del proveedor se reducen a códigos permitidos. Un fallo de transporte se convierte en `MCP_UNAVAILABLE`, un contrato descubierto distinto en `MCP_SCHEMA_MISMATCH`, una respuesta inválida en `INVALID_TOOL_OUTPUT` y un error remoto desconocido en `MCP_TOOL_ERROR`. No persistimos mensajes internos o excepciones crudas como respuesta.

### Paso 5 conservar presupuesto, recuperación e identidad

El core sigue envolviendo **todo `invoke`** en el timeout individual y el presupuesto global: arranque, inicialización, descubrimiento, llamada y cierre. No empezamos a medir recién al llamar a Jira. La cancelación y limpieza siguen siendo cooperativas y pueden hacer que el tiempo observado supere ligeramente el límite.

La política del host se evalúa antes de conectar. El adaptador verifica además la identidad con la que fue creado. El servidor recibe un contexto sintético del proceso padre, nunca roles o workspace enviados en los argumentos del modelo. Solo se pasan esas variables del laboratorio; no se carga `.env` ni se envían claves de proveedores.

**Este contexto local no es autenticación empresarial.** El host controla el entorno del hijo; para servicios remotos necesitaremos identidad verificable, credenciales por servicio y validación de audiencia. Aquí demostramos el punto de aplicación del permiso, no una frontera contra un host comprometido.

Guardamos `execution_profile` en el run: `fake-jira-v1` o `mcp-jira-fixture-v1`. Al recuperar seleccionamos ese backend automáticamente. Cambiarlo explícitamente en un run pendiente devuelve `CONFIG_CHANGED`; no continuamos silenciosamente con otro ejecutor. Los registros previos sin ese campo se interpretan como el único backend que existía entonces: el falso.

Los contadores y reservas mantienen las reglas de M1. `mcp_connections_this_process` cuenta arranques intentados y `adapter_invocations_this_process` cuenta solicitudes de herramienta enviadas por el adaptador; no certifica que el proveedor haya terminado. Los contadores persistidos del core representan intentos reservados y no se reinician al recuperar.

### Paso 6 ejecutar y recuperar

```bash
uv run python -m harness --tool-backend mcp start
```

Esperado: `COMPLETED`, dos decisiones del modelo guionado, una llamada de herramienta, una conexión MCP y la respuesta `DEV-123: IN_REVIEW [jira:DEV-123:v1]`.

```bash
uv run python -m harness --tool-backend mcp start --interrupt-at after-result
uv run python -m harness resume UUID_DEL_RUN
```

Copiá el UUID del primer comando. La salida 2 de la interrupción es intencional. Al recuperar no hace falta repetir `--tool-backend`: el perfil está guardado. Como el resultado ya estaba confirmado, las conexiones y las invocaciones de herramienta de este proceso son cero. Solo falta producir la respuesta final.

Con `before-tool`, en cambio, la lectura pendiente puede repetirse con su correlación original y un nuevo intento reservado, según los límites guardados de M1. El proceso anterior debe haber terminado; todavía no coordinamos workers concurrentes.

```bash
uv run python -m harness --tool-backend mcp --scenario denied start
```

Esperado: `FAILED`, `FORBIDDEN`, cero conexiones y cero invocaciones al adaptador; salida 1 intencional. La política detiene la propuesta antes de abrir el servidor.

### Paso 7 comprobar y continuar

```bash
uv run python -m unittest discover -s tests -v
```

**53 pruebas pasaron.** Las 11 nuevas cubren comunicación MCP entre procesos, permisos del servidor ante clientes directos, denegación previa a la conexión, cambios de schema, respuestas malformadas, errores sanitizados, deadline durante el arranque, timeout de la herramienta y recuperación con el perfil persistido sin conexión innecesaria.

Este es el primer incremento de M2. Sigue ampliar las integraciones de lectura antes de agregar escrituras y aprobaciones. Go, GitHub y Jenkins se incorporarán en pasos separados; no damos por terminada toda la arquitectura objetivo con esta primera herramienta.
