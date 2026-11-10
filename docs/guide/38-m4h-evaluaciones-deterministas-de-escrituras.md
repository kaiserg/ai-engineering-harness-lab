# M4h evaluaciones deterministas de escrituras

[Índice](../guide.md) · [← Anterior](37-m4g-observabilidad-del-worker-y-del-ledger-de-escrituras.md) · [Siguiente →](39-m3d1-frontera-de-identidad-de-usuarios-y-workspace.md)


### Paso 1 definir qué significa aprobar una evaluación

El objetivo es repetir un escenario completo y verificar sus garantías con criterios explícitos. Un test de una función ayuda a localizar un error; estas evaluaciones siguen la intención desde la lectura y la propuesta hasta el worker, MCP, proveedor y ledger.

Un PASS no significa siempre «se ejecutó la solicitud». En un escenario rechazado o vencido, PASS significa que el sistema impidió el envío y no produjo efecto. Si queda UNKNOWN sin prueba suficiente, fingir NOT_APPLIED sería un fallo, aunque parezca un resultado más cómodo.

Mantenemos separado el estado de negocio del resultado de evaluación. No interviene un juez LLM, no se pagan tokens ni se prueban Jenkins/GitHub/Jira reales. PostgreSQL, el protocolo MCP y sus procesos sí son reales.

### Paso 2 escribir expectativas antes de observar resultados

`evals/write_cases.json` contiene ocho escenarios y sus resultados esperados: estado de propuesta/operación/trabajo, cantidad de reclamos, efectos, llamadas, eventos, código y revisión. `scripts/evaluate_writes.py` ejecuta el recorrido y compara lo observado con ese archivo.

| Caso | Lo que debe ocurrir | Request / lookup | Efectos |
| --- | --- | --- | --- |
| `M4-approved` | Aprobar, consumir y terminar SUCCEEDED/DONE. | 1 / 0 | 1 |
| `M4-rejected` | Rechazo persistido; consumo denegado, sin operación ni trabajo. | 0 / 0 | 0 |
| `M4-expired` | Aprobación vencida no se consume; APPROVAL_EXPIRED. | 0 / 0 | 0 |
| `M4-stale` | Cambia la revisión después de preparar; el proveedor rechaza y termina NOT_APPLIED. | 1 / 0 | 0 |
| `M4-revoked` | Se retiran permisos del aprobador antes del worker; no se envía. | 0 / 0 | 0 |
| `M4-response-lost` | El proceso MCP muere después del commit remoto; recuperar por consulta. | 1 / 1 | 1 |
| `M4-after-send` | Interrupción del host después del envío; esperar y recuperar con otro reclamo. | 1 / 1 | 1 |
| `M4-manual-closure` | Interrupción antes de enviar; ausencia mantiene UNKNOWN, cierre y otro actor confirman NOT_APPLIED. | 0 / 1 | 0 |

También contamos `revision` al preparar y `close` al preparar/confirmar el cierre manual. Un caso SUCCEEDED con dos llamadas request falla, aunque el simulador deduplique y conserve un solo efecto. No confundimos «no duplicó filas» con «el harness no reenvió».

El dataset debe incluir exactamente los ocho escenarios conocidos, con IDs únicos y expectativas completas. Un archivo vacío, incompleto o inválido produce un reporte de fallo. No hay un PASS basado en cero casos.

### Paso 3 aislar las evaluaciones de la app

Cada caso genera su propio schema `harness_eval_<uuid>` en el PostgreSQL local y su propia SQLite de proveedor en un directorio temporal. No aceptamos `--schema` ni una ruta de proveedor: este comando no puede seleccionar el ledger de la app o un Jenkins real.

Creamos un run de lectura con el modelo guionado y los servidores MCP de fixtures existentes. A partir de esa evidencia se crea y aprueba/rechaza la propuesta usando los componentes de producción del laboratorio. El worker usa el mismo `Operations`, guard, lease y cliente MCP; el evaluador no implementa otro algoritmo de despacho.

Un wrapper de observación cuenta llamadas sin reemplazarlas. Los efectos se cuentan aparte en la SQLite del proveedor, después del commit. Para recuperar se abren nuevos objetos de store, proveedor y worker con el mismo almacenamiento; no se ejecuta otra vez la lectura.

Al salir, incluso por una excepción, se elimina únicamente el schema generado por ese caso y su directorio temporal. Si falla la limpieza, el caso no devuelve PASS. Una terminación forzada del evaluador o una caída total de PostgreSQL puede impedir esa limpieza; los schemas temporales usan el prefijo indicado para poder investigarlos. No agregamos un borrado masivo automático.

### Paso 4 simular tiempo y fallos sin debilitar controles

La evaluación de vencimiento inserta una fixture histórica coherente, ya aprobada y con plazo pasado, en el schema temporal. Usa el reloj de PostgreSQL y recalcula su hash. No cambia una fila existente, no desactiva triggers y no acorta el plazo de treinta minutos de la app. Este caso comprueba vencimiento antes del consumo; no simula treinta minutos reales de espera después de encolar.

En after-send y cierre manual se espera el vencimiento real del lease del ledger, consultando el reloj de PostgreSQL con un límite de treinta segundos para la prueba. No asumimos que el reloj del host y el de la VM están sincronizados. Por eso estos casos tardan varios segundos; no son una prueba de rendimiento ni imponen un SLA.

`response-lost` activa el fallo de laboratorio exclusivamente en el proceso MCP del proveedor temporal. El servidor confirma el efecto y termina con código 9 antes de responder. El worker conserva incertidumbre y busca el recibo; no vuelve a enviar. La evaluación también confirma el historial PREPARED → DISPATCHING → UNKNOWN → SUCCEEDED.

`after-send` usa el punto de interrupción controlado del host y reconstruye el runtime antes de otro reclamo. Las muertes reales del proceso worker y SIGTERM siguen cubiertas por las pruebas PostgreSQL anteriores; no afirmamos que este caso por sí solo haya matado un worker del sistema.

### Paso 5 probar la revisión manual y el rechazo de un envío tardío

El caso manual primero comprueba que una búsqueda vacía conserva UNKNOWN y deja NEEDS_REVIEW. Preparar el cierre genera evidencia persistida en el proveedor, pero no basta para cambiar el ledger. Se rechaza la confirmación del propio solicitante y se requiere el otro actor de prueba, con el hash/versión exactos y la comprobación actual del cierre.

Después de confirmar, una sonda adversarial separada intenta enviar al ID cerrado y debe recibir OPERATION_CLOSED. Esa sonda se reporta como `late_request_rejected`; no se cuenta como un request del worker ni forma parte de su recuperación. Se vuelve a contar la SQLite para demostrar que siguió sin efectos.

La repetición de confirmación y los ciclos posteriores del worker deben ser idempotentes: sin otro envío, sin otro lookup y sin nuevos reclamos. El trabajo se sincroniza a DONE y la propuesta sigue CONSUMED. Para pedir otra acción se necesita otra intención, no reutilizar la aprobación consumida.

### Paso 6 detectar errores que un estado final podría ocultar

Cada caso comprueba también:

- La lectura original completa, evidencia y contadores permanecen iguales: cuatro decisiones y tres herramientas.
- El snapshot de límites, tiempo activo y reservas sigue válido.
- Hay cero o una operación/trabajo según el escenario; repetir preparación/admisión no crea otra intención.
- Cada request usa el mismo operation ID, hash y argumentos aprobados.
- Reabrir y repetir el recorrido no genera nuevas llamadas después del resultado terminal.

Los flags de interrupción y revisión solo se califican en los escenarios que realmente los ejecutan. Los IDs, hashes, DSN, rutas temporales, credenciales y argumentos no se incluyen en el JSON del reporte.

La primera ejecución detectó una consulta duplicada: al recuperar UNKNOWN, el worker consultaba y, si seguía UNKNOWN, volvía a consultar dentro del mismo reclamo. No repetía request, pero agregaba I/O innecesario. Corregimos `write_worker.py`: solo intenta la consulta inmediata después de un primer despacho que devolvió UNKNOWN; si el reclamo empezó como recuperación, consulta una vez y aparca cuando no hay prueba. No cambiamos el resultado esperado para ocultar el problema.

### Paso 7 ejecutar y leer el reporte

Desde la raíz del proyecto, con dependencias instaladas, PostgreSQL del laboratorio activo y el binario MCP Go de lectura construido:

```bash
uv run python scripts/postgres.py up
uv run python scripts/build_jenkins.py
uv run python scripts/evaluate_writes.py
```

No hace falta arrancar la API, el frontend, el worker de la app, Jaeger o Grafana para evaluar. Pueden seguir activos; el evaluador opera en sus propios schemas y proveedores. No cambia el contexto Docker global ni reinicia los servicios de la app.

El comando imprime cada caso y los criterios que fallaron. Escribe el reporte en `data/runtime/evals/writes-latest.json`, separado de las evals de lectura. Ambos reportes quedan ignorados por Git.

```text
PASS M4-approved
...
PASS M4-manual-closure
8/8 casos aprobados.
```

En el JSON, `checks` muestra los criterios y `observed` los hechos medidos; por ejemplo `calls.request`, `calls.lookup`, `effects`, `claims` y `events`. `duration_ms` es informativo. Si no se puede iniciar PostgreSQL/MCP o ejecutar/limpiar un caso, se registra EVALUATION_EXECUTION_ERROR sin volcar excepciones crudas, y el comando termina con código 1. El éxito completo termina con código 0.

Para repetir también la línea base de lectura:

```bash
uv run python scripts/evaluate.py
```

### Paso 8 verificar al evaluador y mantener el alcance

```bash
uv run python -m unittest discover -s tests -p 'test_write_evaluations.py' -v
uv run python -m unittest discover -s integration_tests -p 'test_write_evaluations.py' -v
uv run python -m unittest discover -s integration_tests -v
```

Las pruebas del grader alteran expectativas/cuentas, evidencia de revisión y contadores de lectura para demostrar que los criterios pueden fallar. Otras pruebas comprueban dataset incompleto, errores del entorno sanitizados, salida no exitosa con cero casos y limpieza del schema ante una excepción. Las integraciones ejecutan muerte real del servidor MCP y cierre manual, y verifican que el schema temporal ya no existe al terminar.

Esto establece una línea base de ocho casos del proveedor ficticio. No evalúa calidad de un LLM, carga, ataques nuevos, Jenkins real ni acceso de usuarios reales. No reemplaza las pruebas de carrera, fencing y recuperación del resto del laboratorio. La incorporación a CI queda para el siguiente paso de automatización; este incremento deja primero un comando reproducible y criterios revisables.

La siguiente brecha del diseño que vamos a abordar es **identidad de usuarios y autorización por workspace**, sustituyendo progresivamente las identidades fijas de desarrollo. La identidad de servicio MCP/TLS que ya tenemos cumple otra función y se conserva.

Validación M4h: **213 pruebas locales, 104 PostgreSQL, 8/8 evaluaciones de escritura y 9/9 de lectura aprobadas**. Las integraciones incluyen muerte real del servidor MCP tras el commit, revisión manual, rechazo de envío tardío y eliminación del schema temporal. No quedaron schemas `harness_eval_*`, trabajos PENDING/RUNNING ni operaciones DISPATCHING/UNKNOWN en la app. El worker local se reinició con la corrección; ambos exporters continuaron disponibles. Las suites Compose/rotación no se repitieron: su configuración no cambió. Gitleaks no encontró secretos en los archivos publicables revisados.

### Checkpoint antes de identidad de usuarios

El checkpoint público reúne el avance local hasta M4h. Para reproducirlo, instalá las dependencias del lockfile, prepará PostgreSQL y el binario Go según el README, y ejecutá `scripts/evaluate.py` y `scripts/evaluate_writes.py`. No se distribuyen bases ni materiales privados: las migraciones y fixtures crean el estado necesario. La [matriz de estado](../status.md) registra resultados, pendientes y el alcance de la revisión de publicación.

El siguiente paso continúa con autenticación de usuarios y autorización por workspace. No cambiaremos la identidad de servicio MCP por una identidad enviada libremente desde el navegador; ambos controles cumplen funciones distintas.
