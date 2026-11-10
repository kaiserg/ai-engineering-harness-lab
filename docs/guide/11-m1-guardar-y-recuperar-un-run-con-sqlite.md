# M1 guardar y recuperar un run con SQLite

[Índice](../guide.md) · [← Anterior](10-lo-que-sigue-en-m1.md) · [Siguiente →](12-m1-deadline-global-y-tiempo-activo-persistido.md)


### Qué agregamos y qué sigue siendo ficticio

`SQLiteRunStore` implementa la misma interfaz que el store en memoria. El loop ahora puede continuar un run de lectura desde un checkpoint guardado por un proceso anterior. El LLM, Jira y la identidad siguen siendo fixtures: este ejercicio no usa claves, APIs, MCP o escrituras externas.

Usamos `sqlite3`, incluido en Python. No instalamos dependencias. Se mantiene el comando sin subcomando para ejecutar el demo M0 en memoria; los comandos `start`, `status` y `resume` usan SQLite.

### Paso 1 guardar el estado en disco

`harness/storage.py` crea una tabla `runs` en `data/runtime/harness.sqlite3`. Cada fila tiene `run_id`, workspace, solicitante, versión y un payload JSON. Los campos de scope y versión están en columnas para filtrar y actualizar de forma condicional; el JSON contiene el snapshot completo.

Guardamos estado, contadores, límites, resultados, respuesta y llamada pendiente. `encode` usa `dataclasses.asdict`; `decode` reconstruye `RunState`, `ToolCall` y `ToolResult`. JSON conserva datos, no objetos ejecutables como un pickle. El payload y la base tienen versiones independientes de schema; una versión no admitida o JSON corrupto se rechaza sin intentar adivinar cómo recuperarlo. No hay migraciones generales todavía.

La base está excluida por `.gitignore`. Contiene evidencia en texto claro, por ahora ficticia: ignorar un archivo en Git no lo cifra ni protege de otros usuarios del sistema. El diseño de acceso y retención deberá ampliarse antes de guardar información real.

### Paso 2 confirmar cada cambio con una transacción

`_database` abre una conexión, inicia `BEGIN IMMEDIATE`, confirma al salir correctamente y hace rollback ante error; después cierra la conexión. En esta primera versión usamos esa transacción también en las lecturas por simplicidad, lo que serializa más de lo necesario y puede producir espera por locks. No es la configuración objetivo para carga concurrente.

Al guardar leemos la versión actual y escribimos con `WHERE version = version_esperada`, dentro de la misma transacción. Si no coincide, devolvemos CONFLICT. Dos instancias con snapshots diferentes no pueden sobrescribirse silenciosamente.

Las reglas compartidas de creación, actualización y ownership sirven tanto a SQLite como a memoria. Rechazan reducción de contadores, modificación de límites ya fijados, eliminación de resultados anteriores y transiciones inválidas. Si el almacenamiento falla, el core puede no alcanzar a guardar un estado final: el error no se convierte en una garantía ficticia de persistencia.

### Paso 3 definir checkpoints explícitos

El run añade tres campos:

| Campo | Para qué sirve |
| --- | --- |
| `checkpoint` | Distinguir qué parte de la ejecución quedó confirmada. |
| `pending_call` | Conservar nombre, argumentos y `call_id` de la lectura pendiente. |
| `limits_snapshot` | Conservar la configuración de límites que debe respetarse al retomar. |

Estados de checkpoint:

| Checkpoint | Qué sabemos al recuperar |
| --- | --- |
| READY | No hay llamada pendiente; los resultados guardados se pueden reutilizar tras revalidar acceso. |
| MODEL_PENDING | Reservamos una llamada al modelo, pero no confirmamos su decisión. Su resultado o consumo remoto podrían desconocerse con un proveedor real. |
| TOOL_PENDING | Guardamos propuesta, argumentos, correlación y reserva de intento; falta confirmar el resultado. |

El core reserva y guarda antes de llamar al modelo o herramienta. Cuando vuelve una tool, guarda en una sola actualización el resultado y la eliminación de `pending_call`, dejando READY. Después pide la siguiente decisión al modelo.

Una interrupción antes del commit no confirma ese cambio; una interrupción después permite recuperarlo desde otro proceso. SQLite no guarda por nosotros el punto de ejecución de Python: el loop decide qué hacer usando estos datos.

### Paso 4 interrumpir después de confirmar una lectura

```bash
uv run python -m harness start --interrupt-at after-result
```

El programa imprime un JSON con el UUID del run y sale con código **2**, esperado para la interrupción controlada. En ese momento:

```text
status: RUNNING
checkpoint: READY
model_calls: 1
tool_calls: 1
saved_results: 1
```

El hook de prueba se ejecuta después de confirmar la transacción y provoca `SystemExit(2)`. No queda un store compartido en RAM entre este comando y el siguiente. El core no convierte ese corte en FAILED porque no es un fallo de dominio del run.

Es una salida controlada en un punto conocido, no una simulación de pérdida eléctrica o corrupción del disco. Las pruebas también ejecutan cada comando en un proceso diferente para comprobar que el estado viene del archivo.

### Paso 5 inspeccionar y retomar el mismo UUID

Reemplazar `UUID_DEL_RUN` por el valor impreso:

```bash
uv run python -m harness status UUID_DEL_RUN
uv run python -m harness resume UUID_DEL_RUN
```

`resume` verifica ownership, límites originales, acceso actual a la evidencia y naturaleza de la llamada pendiente. Si está RUNNING lo devuelve a QUEUED mediante `requeue_read`, que es un camino explícito de recuperación. Un `save` normal sigue sin permitir RUNNING → QUEUED.

`requeue_read` requiere la misma versión y snapshot; solo cambia el estado. No borra resultados, llamadas pendientes o contadores. `execute` reclama después el run y continúa:

```text
RUNNING, READY, resultado guardado
   ↓ revalidar acceso y reencolar explícitamente
QUEUED → RUNNING
   ↓ modelo falso recibe el resultado ya guardado
COMPLETED
```

Después de `after-result`, el resultado esperado es:

```text
status: COMPLETED
model_calls: 2
tool_calls: 1
saved_results: 1
adapter_invocations_this_process: 0
```

Los contadores son de todo el run. `adapter_invocations_this_process` solo cuenta llamadas al Jira falso de este proceso: cero demuestra que reutilizamos evidencia guardada. `status` muestra ese campo como `null` porque solo inspecciona y no ejecuta un adaptador.

Ejecutar `resume` sobre el mismo run COMPLETED devuelve el resultado sin llamar al modelo o herramienta ni modificar su versión. Revalida el acceso antes de devolver la evidencia. Un run FAILED o CANCELLED no se reactiva mediante este comando.

### Paso 6 probar la lectura pendiente

```bash
uv run python -m harness start --interrupt-at before-tool
```

Aquí queda TOOL_PENDING con `call_id`, argumentos y una reserva, pero sin resultado confirmado. Retomar ese UUID vuelve a intentar **solo la lectura**. Se conserva la correlación y se consume otra reserva de tool, porque sin un resultado no podemos asegurar si el intento previo llegó a ejecutarse.

Con los límites iniciales, el resultado será `tool_calls: 2`, `saved_results: 1` y una invocación de Jira en el proceso de recuperación. Puede haber dos reservas aunque la primera se interrumpiera antes de invocar: es una contabilización conservadora, no una medición de llamadas físicas exactas.

Si se interrumpe después de que Jira responde pero antes de guardar el resultado, también puede repetirse esa lectura. No prometemos «exactamente una vez». Una herramienta con efectos no puede usar esta estrategia sin su contrato específico de recuperación.

### Paso 7 probar una decisión de modelo pendiente

```bash
uv run python -m harness start --interrupt-at before-model
```

La reserva de modelo ya está guardada. Al retomar, el loop conserva ese intento y reserva uno nuevo, luego hace el recorrido normal. Para el fake terminará con tres reservas de modelo y una de tool. El hook se llama antes del adaptador, pero el mismo checkpoint representa también un corte posterior sin decisión confirmada.

Con un proveedor real podría haber consumo remoto aunque no recibamos el resultado. Todavía no contamos tokens ni costo en esta etapa; no se debe interpretar ausencia de resultado como consumo cero.

### Paso 8 no reiniciar límites ni recuperar escrituras ciegamente

El presupuesto de llamadas no se reinicia. Las pruebas comprueban que una recuperación falla con BUDGET_EXCEEDED si ya no queda reserva, incluso antes de reintentar una lectura pendiente. Cambiar la configuración de límites al retomar produce CONFIG_CHANGED en lugar de ampliar silenciosamente el presupuesto.

También se reevalúan permisos sobre los resultados guardados y la llamada pendiente. Si se revoca el proyecto permitido, el core no manda esos datos al modelo. Esta es una comprobación con identidades ficticias; no existe aún un proveedor de identidad ni una política revocable almacenada en servidor.

La recuperación solo admite `jira_get_issue`. Una llamada pendiente como `jenkins_request_tests` produce UNSAFE_RECOVERY y no alcanza el adaptador. `Operation.UNKNOWN`, aprobaciones transaccionales e idempotencia remota siguen siendo trabajo posterior.

### Paso 9 usar otra base sin borrar los ejercicios anteriores

```bash
uv run python -m harness --db data/runtime/otro-ejercicio.sqlite3 start --interrupt-at after-result
uv run python -m harness --db data/runtime/otro-ejercicio.sqlite3 status UUID_DEL_RUN
uv run python -m harness --db data/runtime/otro-ejercicio.sqlite3 resume UUID_DEL_RUN
```

`--db` es una opción global y va antes del subcomando. Para recuperar debe apuntar al mismo archivo. La ruta por defecto se resuelve junto al proyecto, no desde una memoria de sesión del agente.

### Paso 10 ejecutar las pruebas

```bash
uv run python -m unittest discover -s tests -v
```

**Validación M1: 31 pruebas pasaron**, incluidas las 21 de M0. Las nuevas cubren:

- Contratos compartidos del store en memoria y SQLite.
- Dos instancias con versiones en conflicto sin pérdida de la actualización confirmada.
- Contadores que no retroceden y configuración que no cambia al guardar.
- Payload corrupto y versión de base no admitida.
- Interrupción y recuperación en procesos separados en los tres checkpoints.
- Evidencia confirmada reutilizada sin consultar de nuevo.
- `call_id` conservado y nueva reserva para una lectura incierta.
- Presupuesto agotado, acceso revocado y configuración diferente.
- Rechazo de recuperación automática de una escritura.

### Límite operativo y siguiente incremento

Esta recuperación es local y operada manualmente: **el proceso anterior debe haber terminado**. No ejecutar `resume` contra un run que otro proceso sigue trabajando. CAS protege actualizaciones locales, pero no implementa propiedad exclusiva ni cancela una solicitud en vuelo. Leases y fencing multiworker pertenecen a una fase posterior.

No hay reanudación automática al arrancar, migraciones generales de schemas, transacciones distribuidas o autenticación real. SQLite persiste los contratos, no implementa esas funciones por sí solo.

Ese fue el cierre del incremento de almacenamiento. La sección siguiente implementa el deadline global y el tiempo activo persistido; MCP sigue en M2.
