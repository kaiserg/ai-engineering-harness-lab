# M2 Jenkins en Go y comparación de revisiones

[Índice](../guide.md) · [← Anterior](14-m2-github-por-mcp-y-recuperacion-entre-herramientas.md) · [Siguiente →](16-m2-informe-parcial-cuando-falta-jenkins.md)


### Paso 1 incorporar Go con una función concreta

Jenkins es nuestro primer servidor escrito en Go. El host y su core permanecen en Python: el cliente solo ve el contrato MCP, no el lenguaje del proveedor.

```text
Host Python
  → MCP Jira (Python)   → guardar ticket y vínculo al PR
  → MCP GitHub (Python) → guardar estado y revisión del PR
  → MCP Jenkins (Go)   → guardar resultado y revisión del build
  → regla determinista → guardar comparación y respuesta
```

Usamos el [SDK oficial de MCP para Go](https://github.com/modelcontextprotocol/go-sdk), fijado en `v1.8.0` dentro de `services/jenkins-mcp/go.mod`; `go.sum` registra las comprobaciones de sus módulos. El SDK implementa el protocolo: no escribimos JSON-RPC, inicialización o transporte desde cero.

En esta máquina no había Go disponible. Preparamos Go 1.27.3 en `.tools/go`, verificando el SHA-256 publicado para el archivo descargado. `.go-version` registra la versión usada; el directorio `.tools/`, las cachés y los binarios están excluidos de Git. En otra máquina se puede instalar esa versión desde [Go Downloads](https://go.dev/dl/) y tener `go` en PATH. No hace falta repetir la instalación para usar este entorno ya preparado.

**Decisión:** compilar antes de ejecutar el run. Si usáramos `go run` como servidor, la primera compilación y las descargas podrían consumir el timeout de la herramienta. El run inicia un binario listo; el presupuesto sí incluye su arranque, inicialización MCP, consulta y cierre.

```bash
uv run python scripts/build_jenkins.py
```

El script busca Go en PATH o en `.tools/go/bin/go`, mantiene GOPATH y cachés bajo `.cache/go` y crea `bin/jenkins-mcp`. Usa `-mod=readonly` para no modificar silenciosamente dependencias y `-trimpath` para quitar rutas del entorno de compilación. Volvé a compilar después de modificar el servidor Go. Los recorridos anteriores no requieren este binario, pero la suite completa ahora sí prueba Python↔Go.

### Paso 2 definir un contrato de solo lectura

La herramienta es `jenkins_get_build` y admite exactamente:

```json
{"job": "atlas-tests", "build_number": 101}
```

`build_arguments` valida el límite Python. `JENKINS_INPUT_SCHEMA` publica el mismo contrato. El servidor Go declara ese schema y valida nuevamente los argumentos recibidos: nombre simple de job, entero positivo hasta 1 000 000, sin campos extra, rutas, URLs o roles del modelo.

En Go, `json.Decoder.UseNumber()` conserva el número JSON como tal. Después `strconv.ParseInt` verifica que podamos tratarlo como entero. No aceptamos booleanos o strings como números. No tenemos una herramienta para ejecutar builds todavía: esta herramienta solamente consulta evidencia.

La fixture devuelve:

```json
{
  "schema_version": 1,
  "data": {
    "job": "atlas-tests",
    "build_number": 101,
    "repository": "demo/atlas",
    "revision": "revision-A",
    "result": "SUCCESS"
  },
  "source_id": "jenkins:atlas-tests:101:revision-A"
}
```

Son datos sintéticos. No contactamos una instancia Jenkins, no ejecutamos pipelines y no necesitamos credenciales. `revision-A` y `revision-B` son identificadores didácticos, no hashes reales de Git.

### Paso 3 entender las piezas del servidor Go

`services/jenkins-mcp/main.go` contiene:

| Pieza | Función |
| --- | --- |
| `identity` | Contexto sintético de workspace, roles y jobs asignados por el host. |
| `parseArguments` | Valida la forma y los tipos de los argumentos. |
| `newServer` | Registra una herramienta fija con `server.AddTool` y su handler. |
| Handler de lectura | Reautoriza rol/job, verifica workspace y devuelve la fixture. |
| `toolError` | Devuelve `isError` con un código sanitizado, sin detalles internos. |
| `main` | Valida configuración local y ejecuta el SDK sobre `mcp.StdioTransport`. |

`context.Context` transporta la cancelación hasta el handler. La espera artificial selecciona entre el temporizador y `ctx.Done()`, para no ignorar una cancelación del cliente. El SDK maneja los mensajes del protocolo. Los errores de arranque se escriben en stderr; stdout queda reservado para MCP.

La identidad continúa siendo configuración de desarrollo enviada por un host confiable al proceso hijo. No equivale a autenticación remota empresarial ni protege contra un host que falsifique su entorno. OIDC, credenciales de integración y audiencia pertenecen a los pasos posteriores del diseño.

### Paso 4 agregar permisos por job y el cliente Python

`Identity.jobs` es un conjunto vacío por defecto. Tener acceso a Jira o al repositorio GitHub no concede permiso para un job. La identidad de consola recibe `atlas-tests` explícitamente.

El host aplica `ReadOnlyPolicy` antes de conectar. El servidor Go verifica nuevamente rol lector/operador, job permitido y workspace. También probamos clientes MCP directos para comprobar que la protección del servidor funciona aunque no intervenga la política Python.

`MCPJenkins` hereda el transporte y controles de `MCPRead`, pero reemplaza el comando Python por `bin/jenkins-mcp`. Conserva `mcp_connection`, descubrimiento del schema esperado, una solicitud explícita y cierre del proceso. No abrimos una terminal aparte para Jenkins.

El normalizador Python exige el job/build solicitados, campos exactos, repositorio sintético esperado, revisiones/resultados admitidos por esta fixture y una referencia coherente. Una revisión distinta **es una respuesta válida** del proveedor: se acepta como dato y luego se marca como evidencia inadecuada para el PR. Rechazarla como error de transporte impediría explicar el problema real.

La relación `demo/atlas → atlas-tests` y el build 101 están configurados explícitamente para este laboratorio. El modelo no elige un job a partir de instrucciones contenidas en un README. La regla final comprueba además que el repositorio del build corresponda al PR. Más adelante necesitaremos configuración de bindings por repositorio y selección de builds reales.

### Paso 5 comparar evidencia fuera del modelo

`harness/evidence.py` define `finalize_ci`. El core lo ejecuta antes de confirmar la respuesta final del nuevo recorrido. Esta función exige las tres evidencias en orden y comprueba:

1. Que el PR consultado sea el relacionado por Jira.
2. Que el repositorio del build sea el del PR y que su job sea el configurado.
3. Que la revisión del build coincida exactamente con la revisión consultada del PR.
4. Que el resultado del build sea `SUCCESS`.

La parte principal es:

```python
same_revision = build.data['revision'] == github.data['head_revision']
successful = build.data['result'] == 'SUCCESS'
status = 'STALE_BUILD' if not same_revision else 'VERIFIED' if successful else 'BUILD_FAILED'
```

| Revisión del PR | Revisión del build | Build | `assessment.status` |
| --- | --- | --- | --- |
| revision-A | revision-A | SUCCESS | VERIFIED |
| revision-A | revision-B | SUCCESS | STALE_BUILD |
| revision-A | revision-A | FAILURE | BUILD_FAILED |

Si difieren las revisiones, `STALE_BUILD` tiene prioridad sobre el resultado del build. El nombre indica que esa evidencia no corresponde a la revisión consultada; no deducimos una cronología comparando las letras A y B. `VERIFIED` significa solamente que esta comparación de revisiones y resultado pasó.

El resultado se guarda en `Run.assessment`: estado, igualdad de revisiones, éxito del build, ambas revisiones y referencias de evidencia. También generamos la respuesta final de forma determinista. El texto optimista del modelo no puede cambiar esa conclusión. Una prueba fuerza al modelo a pedir desplegar sobre un build de otra revisión y comprueba que el core devuelve `STALE_BUILD`.

**COMPLETED y VERIFIED son cosas diferentes.** `Run.status=COMPLETED` significa que el recorrido terminó de consultar y evaluar. Puede terminar correctamente y detectar `STALE_BUILD` o `BUILD_FAILED`. Ninguna de estas consultas autoriza un despliegue. Tampoco comprobamos antigüedad temporal ni bloqueamos cambios posteriores del PR: esta es una comparación de snapshots guardados.

### Paso 6 conservar perfiles, límites y recuperación

El recorrido nuevo es `--workflow jira-github-jenkins`. Su perfil guardado es `mcp-devops-ci-v1:<escenario>`, por ejemplo `mcp-devops-ci-v1:stale`. El perfil vincula también la fixture: recuperar no puede convertir un build de otra revisión en uno coincidente cambiando un flag.

Los perfiles previos siguen recorriendo Jira solamente o Jira→GitHub. No se agrega Jenkins a un run anterior. `assessment` es un campo nuevo opcional: los registros previos siguen siendo legibles y no reciben una verificación inventada. Una evaluación ya guardada no puede reemplazarse por otra durante una actualización del store.

`READ_TOOLS` incorpora exclusivamente esta lectura Jenkins. `result_call` reconstruye su job/build para volver a autorizar los resultados guardados. Retirar el acceso al job impide devolver la evidencia almacenada, incluso en un run COMPLETED.

Los tres servicios comparten el presupuesto del run. El camino normal consume cuatro decisiones del modelo —Jira, GitHub, Jenkins y respuesta— y tres intentos de herramienta. No reiniciamos el deadline al entrar en Go.

Si un resultado está confirmado, no se consulta otra vez. Si Jenkins quedó pendiente, se conserva su `call_id`; recuperar imputa la reserva temporal huérfana y consume otro intento, igual que las lecturas anteriores. No hay workers concurrentes ni recuperación automática de escrituras.

### Paso 7 probar las tres comparaciones

Desde la carpeta del proyecto, después de compilar:

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins start
```

Esperado: `COMPLETED`, cuatro decisiones, tres herramientas, tres resultados y `assessment.status: VERIFIED`.

Para un build verde de otra revisión:

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario stale start
```

Esperado: `COMPLETED`, `build_successful: true`, `same_revision: false`, `assessment.status: STALE_BUILD`. La respuesta explica por qué ese verde no valida la revisión del PR.

Para un build fallido de la revisión correcta:

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario failed start
```

Esperado: `COMPLETED`, `same_revision: true`, `assessment.status: BUILD_FAILED`. La comparación detectó el problema; el proceso sale con código 0 porque pudo completar la evaluación, no porque CI haya aprobado. Una futura integración como gate necesitará una política explícita sobre este resultado.

### Paso 8 recuperar antes o después de Jenkins

Interrumpir tras Jira y GitHub, antes de consultar Jenkins:

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario stale start --interrupt-at after-result --interrupt-after-results 2
uv run python -m harness resume UUID_DEL_RUN
```

El primer comando sale con código 2 intencional. Reemplazá el UUID. Al recuperar se restaura también `stale`; `tools_this_process` muestra Jira y GitHub con cero conexiones, Jenkins con una. La evaluación sigue siendo `STALE_BUILD`.

Para interrumpir tras guardar los tres resultados:

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins start --interrupt-at after-result --interrupt-after-results 3
uv run python -m harness resume UUID_DEL_RUN
```

Al recuperar, las conexiones e invocaciones de los tres adaptadores son cero: solo falta la decisión final y la comparación determinista.

También se puede dejar Jenkins como lectura pendiente, con su intento y reserva ya registrados:

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins start --interrupt-at before-tool --interrupt-tool jenkins_get_build
uv run python -m harness resume UUID_DEL_RUN
```

En este caso, el hook corta antes de enviar el intento a Go, pero la recuperación aplica la regla conservadora de M1: no puede asumir que un intento sin resultado confirmado no consumió nada. Se espera un total de cuatro intentos de herramienta al terminar. Conservamos el identificador de la llamada pendiente. El proceso previo debe haber terminado antes de recuperar.

### Paso 9 observar un proveedor no disponible

**Registro del primer incremento Jenkins:** el comportamiento FAILED descrito aquí corresponde al perfil `mcp-devops-ci-v1`. Los runs nuevos usan el perfil v2 y el informe parcial explicado en la sección siguiente. El comando conserva su sintaxis.

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario unavailable start
```

La fixture devuelve `UPSTREAM_UNAVAILABLE`. El run termina FAILED con salida 1 y conserva los resultados de Jira/GitHub; `assessment` y `answer` quedan sin una conclusión fabricada. No se reanuda un run FAILED con el mecanismo actual.

Esto aún no entrega el informe parcial de S02: ese comportamiento requiere representar explícitamente evidencia ausente y distinguir un error recuperable del proveedor de una violación de permisos, schema o presupuesto. No relajamos todos esos errores para poder continuar indiscriminadamente.

### Paso 10 verificar y continuar

```bash
uv run python scripts/build_jenkins.py --test
uv run python -m unittest discover -s tests -v
```

**Validación: 82 pruebas Python y 2 pruebas Go pasaron.** Las 15 pruebas Python nuevas incluyen el contrato real Python↔Go, schema idéntico, argumentos inválidos, permisos de job/rol/workspace en el servidor, comparación coincidente/otra revisión/fallo, revocación de acceso a resultados guardados, proveedor no disponible, modelo optimista y respuesta prematura, evidencia de otro repositorio, respuestas inválidas, recuperación entre procesos, lectura Go pendiente y límites compartidos. Las dos pruebas Go cubren rechazo de argumentos y preservación del recurso solicitado, con subcasos de tipos y campos no permitidos.

El primer recorrido con los tres proveedores ya funciona y la comparación de revisiones es reproducible. M2 sigue pendiente de completar informes parciales y el ejercicio adversarial S04. Todavía no conectamos Jenkins real, añadimos escrituras o damos por implementados los requisitos empresariales remotos del diseño objetivo.
