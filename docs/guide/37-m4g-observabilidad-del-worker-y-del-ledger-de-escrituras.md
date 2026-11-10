# M4g observabilidad del worker y del ledger de escrituras

[Índice](../guide.md) · [← Anterior](36-m4f-worker-independiente-de-escrituras.md) · [Siguiente →](38-m4h-evaluaciones-deterministas-de-escrituras.md)


### Paso 1 decidir qué pregunta responde cada señal

El objetivo es investigar una escritura sin adivinar si un nuevo reclamo volvió a enviar. Agregamos observación al canal existente; la aprobación y el ledger siguen tomando las decisiones.

| Señal | Pregunta que responde | Fuente |
| --- | --- | --- |
| Estado de trabajo/operación | ¿Está pendiente, en curso, terminado o requiere revisión? | Snapshot de PostgreSQL. |
| Intentos MCP | ¿Se consultó revisión, solicitó, buscó recibo o cerró el ID? | Contadores del cliente por proceso. |
| Duraciones | ¿Cuánto tardaron el cliente MCP y el reclamo? | Histogramas del proceso. |
| Trazas | ¿En qué orden ocurrió cada paso y con qué intento se relaciona? | OpenTelemetry, recibido en Jaeger. |

Un contador de solicitudes mide llamadas del cliente, no efectos aceptados. La evidencia durable del proveedor y el ledger siguen siendo necesarias para afirmar qué ocurrió.

### Paso 2 instrumentar las fronteras existentes

`write_observability.py` mantiene métricas y pequeños wrappers para autorización, despacho, consulta y revisión. `Operations` conserva su lógica y los mismos guards. El worker abre `write.worker.attempt` por reclamo; dentro se ven `write.authorize`, `write.dispatch` o `write.lookup`, y `write.mcp`.

El cliente MCP propaga exclusivamente `traceparent` al proceso stdio que inicia para esa llamada. El servidor crea `write.mcp.server` como hijo; esto permite distinguir tiempo del cliente —incluido startup, descubrimiento y transporte— del tiempo de la herramienta en el servidor. No cambiamos el schema MCP ni agregamos argumentos de observación a la operación de negocio.

Los errores MCP devueltos como `is_error` se anotan con códigos permitidos, aunque no lancen una excepción Python. No exportamos excepciones crudas, argumentos, prompts, recibos, hashes, identidades ni credenciales.

### Paso 3 enlazar los intentos después de reiniciar

La migración 005 añade dos campos opcionales a `write_jobs`: el contexto de la petición que encoló y el del último reclamo. Las filas antiguas conservan NULL y siguen funcionando. La versión del módulo de propuestas llega a 5; el esquema del core de lectura sigue en 3.

Cada reclamo inicia una traza nueva con links a la petición productora y al intento previo, cuando existen. No simulamos un span abierto durante toda la espera en cola o una caída del proceso.

```text
API: encolar ──link──► reclamo 1: authorize → request → interrupción
              └─link─► reclamo 2: lookup → SUCCEEDED
                         ▲
                    link al reclamo 1
```

Los contextos se validan como W3C, se guardan usando el guard/fencing vigente y se ocultan en las respuestas públicas del trabajo. No son credenciales, permisos ni evidencia del efecto. Si no se puede guardar esta correlación, el worker continúa con las comprobaciones normales del ledger. Un intento sin span exportado puede seguir habiendo producido un efecto: una traza ausente no prueba ausencia de escritura.

### Paso 4 separar estado durable de contadores de proceso

Los exporters solo escuchan en localhost:

| Puerto | Proceso | Role en Prometheus |
| --- | --- | --- |
| 9102 | Worker de escrituras | `write-worker` |
| 9103 | API local M4 | `write-api` |

Los contadores/histogramas empiezan de nuevo al reiniciar el proceso; usamos `increase`/`rate` cuando consultamos actividad. Los gauges de trabajos y operaciones vuelven a leer PostgreSQL y reflejan estado persistido. La consulta solo lee las filas del operador/workspace autorizado del laboratorio y no ejecuta recuperación.

API y worker leen la misma base. Por eso el dashboard muestra las series separadas por role: **no sumés los dos snapshots**, porque duplicaría el mismo backlog. Las llamadas MCP sí pertenecen a procesos distintos y pueden agregarse para medir intentos.

Si falla la lectura del snapshot, `harness_write_collection_success` vale 0 y se omiten los gauges de estado. Mostrar cero trabajos en ese caso sería engañoso. `up=1` solo demuestra que responde el exporter; no sustituye esa comprobación de lectura de la base.

Las etiquetas son acotadas: acción/resultado y estado. Los UUID solo aparecen en trazas, nunca como etiquetas de métricas; así cada nueva operación no crea series ilimitadas.

### Paso 5 arrancar con observación opcional

Con la infraestructura del laboratorio disponible, actualizá únicamente los monitores desde la raíz del proyecto:

```bash
uv run python - <<'PY'
import subprocess
from scripts.stack import compose_command
subprocess.run(compose_command()+['up','-d','--no-build','--no-deps','--wait','jaeger'],check=True)
subprocess.run(compose_command()+['restart','prometheus','grafana'],check=True)
PY
```

Jaeger ahora publica OTLP HTTP en `127.0.0.1:4318` para los procesos del host. Recrearlo borra sus trazas en memoria; no borra el ledger, el proveedor ni las bases. Prometheus consulta los puertos del host mediante `host.docker.internal`, disponible en el entorno Docker de este laboratorio. En otro motor Linux puede requerir una ruta equivalente; no ampliamos el bind de los exporters para resolverlo.

En **cada una de dos terminales**, activá:

```bash
export HARNESS_TRACING=1
export HARNESS_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
export HARNESS_WRITE_METRICS=1
```

En la primera terminal:

```bash
uv run python -m harness.web --storage postgres --approvals-demo --write-worker --port 8003
```

En la segunda:

```bash
uv run python -m harness.write_worker
```

No hace falta API key. Estas variables no contienen secretos. `HARNESS_WRITE_METRICS` habilita los exporters nuevos; el flag anterior `HARNESS_METRICS` corresponde a los servicios de lectura/infraestructura. Sin los flags nuevos se conserva el comportamiento anterior y los targets nuevos aparecen apagados.

### Paso 6 mirar el dashboard y seguir la recuperación

Abrí [Grafana](http://127.0.0.1:3003/d/harness-overview): los seis paneles M4 al final muestran disponibilidad de exporters, disponibilidad del snapshot, estados de trabajos, estados del ledger, llamadas MCP recientes y edad del pendiente más antiguo. La edad incluye una espera `not_before` legítima; por sí sola no demuestra un worker bloqueado. Los estados DONE/SUCCEEDED incluyen el historial conservado, no solo actividad de la ventana visual.

Para el ejercicio, usá la [pantalla local](http://127.0.0.1:8003), escenario de revisión desactualizada, propuesta aprobada y fallo «Después de aceptar la solicitud» antes de encolar. El primer reclamo realiza `request`, deja DISPATCHING y espera el lease; el segundo realiza `lookup`. El intervalo entre trazas incluye espera de recuperación y no es duración de la herramienta MCP.

En [Jaeger](http://127.0.0.1:16686), buscá el servicio `harness-write-worker` y el tag `operation.id` de la operación. Abrí los dos `write.worker.attempt` y sus links. El servicio `harness-jenkins-write` aparece dentro de la misma traza para la llamada al proveedor ficticio. La petición que encoló está en `harness-api`; el worker Docker de lectura conserva su instrumentación previa.

También podés consultar en [Prometheus](http://127.0.0.1:9090):

```promql
up{job="harness-writes"}
harness_write_collection_success{job="harness-writes"}
harness_write_jobs_expired_lease{role="write-worker"}
sum by(action,outcome) (increase(harness_write_calls_total[5m]))
harness_write_worker_attempts_total{result="deferred"}
```

Los histogramas `harness_write_call_duration_seconds` y `harness_write_worker_duration_seconds` están disponibles para investigar latencia. No añadimos un umbral de SLA ni alertas automáticas nuevas sin una línea base de carga. Las alertas anteriores siguen vigentes.

### Paso 7 mantener el monitoreo fuera de la decisión

Exportación, actualización de contadores y guardado de correlación son opcionales y toleran fallos. OpenTelemetry usa una cola acotada y timeout del exporter; puede descartar trazas. Si el puerto de métricas está ocupado, el arranque de observación no impide ejecutar el worker. Los monitores no autorizan, reenvían ni resuelven UNKNOWN.

Probamos contadores rotos, snapshot inválido/no disponible y fallo al guardar correlación. El resultado de negocio y los errores originales se conservan. Los tests previos siguen comprobando aprobación, corte remoto, fencing, heartbeat y muertes reales antes/después del efecto.

Jaeger y los exporters son locales de desarrollo, sin autenticación por usuario/workspace. Esta etapa no completa el acceso autorizado empresarial a observabilidad ni el despliegue del worker como servicio administrado.

### Paso 8 evidencia y siguiente incremento

El recorrido real terminó con dos reclamos, ledger SUCCEEDED, trabajo DONE y **una sola solicitud** en el proveedor. Jaeger confirmó `request` en el primer intento y `lookup` en el segundo, con links al productor y al intento previo. El run original conservó cuatro decisiones y tres herramientas. Ambos exporters y sus snapshots respondieron correctamente; Grafana cargó los dieciséis paneles.

Las pruebas locales cubren privacidad y fallos de observación; PostgreSQL verifica persistencia de links, fencing y snapshot sin mutaciones. Las siete comprobaciones Compose de métricas/alertas y las nueve evaluaciones deterministas de lectura siguen aprobadas. Los reportes con IDs quedan en runtime ignorado.

El siguiente incremento propuesto es convertir los casos de escritura en **evaluaciones deterministas**: aprobación rechazada/vencida, cambio de revisión, respuesta perdida, recuperación sin reenvío y revisión manual. Después retomaremos identidad real; no estamos declarando M4 empresarial terminado.

Validación M4g: **204 pruebas locales, 102 PostgreSQL, siete comprobaciones Compose y 9/9 evaluaciones de lectura aprobadas**. Las comprobaciones Compose incluyen reglas/alertas y siete casos virtuales de promtool. La verificación real confirmó el vínculo padre/hijo cliente-servidor MCP y los links entre reclamos. No quedaron trabajos PENDING/RUNNING ni operaciones DISPATCHING/UNKNOWN en el schema principal. Gitleaks no encontró secretos en los archivos publicables nuevos o modificados.
