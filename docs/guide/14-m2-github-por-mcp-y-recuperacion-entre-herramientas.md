# M2 GitHub por MCP y recuperación entre herramientas

[Índice](../guide.md) · [← Anterior](13-m2-primera-herramienta-por-mcp.md) · [Siguiente →](15-m2-jenkins-en-go-y-comparacion-de-revisiones.md)


### Paso 1 definir el recorrido y conservar el anterior

Ahora construimos esta secuencia:

```text
Modelo guionado → Jira: DEV-123
                  ↓ guardar resultado en SQLite
Modelo guionado → GitHub: demo/atlas, PR 42
                  ↓ guardar resultado en SQLite
Modelo guionado → respuesta con ambas referencias
```

Jira devuelve el vínculo `pull_request`, con `repository` y `pull_number`. El siguiente paso toma esos argumentos del resultado confirmado; no obtiene permisos del vínculo. GitHub devuelve `state: OPEN` y `head_revision: revision-A`.

La revisión es un identificador sintético, no un SHA de Git real. Nos servirá para comparar con la revisión de Jenkins en el próximo incremento. `OPEN` tampoco significa aprobado o listo para desplegar: la respuesta dice explícitamente que CI todavía no fue consultado.

**Decisión:** conservar el recorrido anterior. `--workflow jira` sigue siendo el predeterminado; `--workflow jira-github` activa el nuevo. Los perfiles guardados son diferentes:

| Perfil | Recorrido |
| --- | --- |
| `fake-jira-v1` | Jira falso dentro del proceso. |
| `mcp-jira-fixture-v1` | Una consulta Jira mediante MCP. |
| `mcp-devops-read-v1` | Jira con vínculo al PR, GitHub mediante otro servidor MCP y resumen. |

Un run viejo no recibe pasos nuevos al recuperar. El nuevo recorrido requiere `--tool-backend mcp`; un cambio explícito de configuración en un run pendiente se rechaza con `CONFIG_CHANGED`.

### Paso 2 separar proyectos de repositorios

`Identity` agrega `repositories`, vacío por defecto. `projects` continúa representando proyectos Jira. La identidad sintética de la consola tiene `projects={'DEV'}` y `repositories={'demo/atlas'}`; estas asignaciones son configuración confiable del laboratorio, no texto del modelo.

En `ReadOnlyPolicy.authorize` elegimos la regla según el nombre de herramienta:

```python
if call.name == 'github_get_pull_request':
    arguments = pull_request_arguments(call.arguments)
    if arguments['repository'] not in identity.repositories:
        raise ContractError('FORBIDDEN')
    return
```

Primero se exige un rol lector u operador. Después se verifica el recurso concreto. Un lector de `DEV` sin repositorios permitidos puede consultar Jira, pero no GitHub. La herramienta GitHub tampoco se muestra entre las disponibles si no tiene repositorios asignados; la autorización de ejecución sigue siendo obligatoria aunque un modelo la proponga igualmente.

El host verifica antes de conectar. El servidor GitHub vuelve a aplicar la política y su fixture solo devuelve datos de `workspace-alpha`. Las pruebas conectan también clientes MCP directos para verificar que omitir la política del host no abre acceso.

### Paso 3 definir el contrato GitHub

`pull_request_arguments` en `harness/contracts.py` admite exactamente:

```json
{"repository": "demo/atlas", "pull_number": 42}
```

Exige un nombre `owner/repository`, un número entero entre 1 y 1 000 000 y ningún campo adicional. Rechaza URLs, rutas relativas, roles agregados y booleanos como número de PR; en Python `bool` es subtipo de `int`, por eso verificamos el tipo exacto.

`GITHUB_INPUT_SCHEMA` en `harness/mcp_contract.py` expresa las mismas restricciones para MCP. El catálogo del servidor GitHub solo publica `github_get_pull_request`. El cliente compara el schema descubierto con el esperado antes de enviar la consulta.

**Decisión:** cada servicio tiene un catálogo fijo. No aceptamos URLs elegidas por el modelo ni convertimos todo lo descubierto por MCP en una herramienta autorizada.

### Paso 4 compartir transporte y separar proveedores

Extraemos las partes comunes que ya estaban probadas:

| Archivo | Responsabilidad |
| --- | --- |
| `harness/adapters/mcp_read.py` | `MCPRead`: identidad vinculada, cliente MCP, arranque/cierre, descubrimiento y errores comunes. |
| `harness/adapters/mcp_jira.py` | `MCPJira`: nombre, schema y normalización de Jira. |
| `harness/adapters/mcp_github.py` | `MCPGitHub`: nombre, schema y normalización de GitHub. |
| `harness/mcp_server.py` | Servidor de lectura compartido, configuración sintética y transporte stdio. |
| `harness/jira_mcp_server.py` | Construye el servidor Jira con `FakeJira`. |
| `harness/github_mcp_server.py` | Construye otro servidor con `FakeGitHub`. |

`MCPJira` y `MCPGitHub` heredan de `MCPRead`. Cada uno configura el módulo de su proceso servidor, su herramienta y su schema. Compartir el transporte evita mantener dos implementaciones de timeout, ciclo de vida y sanitización.

El host registra ambos adaptadores por nombre en el diccionario `tools`. Cada invocación sigue abriendo y cerrando su conexión; los servidores se inician automáticamente con el mismo intérprete Python. No hay un servidor HTTP, acceso a github.com ni tokens de GitHub en este ejercicio.

`FakeJira(include_pr=True)` agrega el vínculo solo en el nuevo recorrido. `FakeGitHub` devuelve el PR sintético autorizado. El normalizador de Jira valida la forma del vínculo; el de GitHub exige que repositorio y número coincidan con la solicitud, campos exactos, estado/revisión admitidos por esta fixture y referencia de evidencia coherente. Una respuesta con instrucciones o campos extra tampoco cumple el contrato. Cuando usemos una API real ampliaremos ese contrato deliberadamente.

### Paso 5 identificar cada resultado y reautorizarlo

`ToolResult` agrega `tool_name`. Sus datos ya no tienen todos la forma de Jira. El core exige que el nombre y el `call_id` del resultado coincidan con la llamada pendiente antes de guardarlo.

`result_call` reconstruye únicamente la identificación del recurso guardado:

- Jira: `issue_key`.
- GitHub: `repository` y `pull_number`.

Al recuperar, el core vuelve a autorizar cada resultado con los permisos actuales. Esto también ocurre antes de devolver un run COMPLETED almacenado. Haber obtenido evidencia ayer no permite entregarla después de retirar el acceso al repositorio.

Los payloads previos no tenían `tool_name`; el valor predeterminado `jira_get_issue` conserva su significado original. No se reinterpretan como GitHub ni se borran datos. La tabla SQL y el schema JSON del run permanecen en sus versiones actuales; el campo es una ampliación compatible con un valor anterior conocido. Los permisos de repositorio no se congelan en SQLite: se vuelven a obtener de la identidad suministrada por el host.

### Paso 6 conservar las reglas de recuperación y presupuesto

`READ_TOOLS` enumera las dos lecturas que se pueden recuperar. El core y el store sustituyen la restricción exclusiva de Jira por esa lista cerrada; no permitimos repetir cualquier herramienta descubierta ni escrituras.

Si el resultado de Jira ya está confirmado, se reutiliza y solo falta GitHub. Si el resultado GitHub también está confirmado, solo queda la respuesta final. Si GitHub quedó pendiente, se conserva su `call_id`, se imputa la reserva temporal huérfana y un nuevo intento consume otro cupo de herramienta, como en M1.

Seguimos usando un único contador de modelos, uno de herramientas y un presupuesto temporal para todo el run. No se crean límites nuevos por servidor. El timeout de cada invocación cubre todo el ciclo MCP, incluido arranque y cierre. Una cancelación durante GitHub conserva la evidencia confirmada de Jira y marca el run CANCELLED; no se recuperan automáticamente runs cancelados o fallidos.

**La ejecución sigue siendo secuencial.** GitHub depende del vínculo que aporta Jira; todavía no agregamos paralelismo ni coordinación entre workers.

### Paso 7 ejecutar el recorrido completo

Desde la carpeta del proyecto:

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github start
```

Esperado:

```text
status: COMPLETED
model_calls: 3
tool_calls: 2
saved_results: 2
mcp_connections_this_process: 2
execution_profile: mcp-devops-read-v1
```

Las tres decisiones son: proponer Jira, proponer GitHub y producir la respuesta. `tools_this_process` desglosa conexiones e invocaciones de Jira y GitHub. Los contadores globales del run siguen siendo acumulados; los del proceso se reinician en cada comando.

La respuesta contiene ambas referencias y `revision-A`. Todavía no afirma que las pruebas o el build hayan pasado.

### Paso 8 interrumpir entre Jira y GitHub

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github start --interrupt-at after-result
uv run python -m harness status UUID_DEL_RUN
uv run python -m harness resume UUID_DEL_RUN
```

Copiá el UUID del primer comando. La interrupción intencional sale con código 2 y deja un resultado guardado. El proceso anterior debe haber terminado antes de recuperar.

`resume` restaura perfil y límites sin repetir flags. Al terminar, `tools_this_process` debe mostrar Jira con cero invocaciones/conexiones y GitHub con una. El run acumula tres decisiones del modelo y dos llamadas de herramientas: recuperar no repite Jira ni reinicia sus contadores.

Para interrumpir después de ambas consultas:

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github start --interrupt-at after-result --interrupt-after-results 2
uv run python -m harness resume UUID_DEL_RUN
```

En este segundo ejercicio ambas conexiones e invocaciones del proceso de recuperación deben ser cero. La decisión final usa los dos resultados almacenados.

### Paso 9 verificar lo construido y elegir lo siguiente

```bash
uv run python -m unittest discover -s tests -v
```

**67 pruebas pasaron: las 53 previas y 14 nuevas.** Cubren comunicación real por MCP con el servidor GitHub, rechazo de argumentos inválidos, permisos por repositorio, aislamiento por workspace, cambios de schema, respuestas de otro recurso/revisión, errores sanitizados, recuperación entre procesos tras uno o dos resultados, recuperación de GitHub pendiente con el mismo `call_id`, revocación de acceso a evidencia guardada, contadores compartidos, timeout, deadline global y cancelación durante la segunda herramienta.

M2 sigue abierto: el próximo incremento es Jenkins en Go y la comparación determinista entre la revisión del build y la del PR. Después comprobaremos evidencia obsoleta y fallos parciales. La identidad empresarial remota, el modelo real y las escrituras requieren sus propios incrementos.
