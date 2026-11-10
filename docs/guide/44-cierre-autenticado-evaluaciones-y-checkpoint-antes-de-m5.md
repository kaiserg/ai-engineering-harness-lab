# Cierre autenticado evaluaciones y checkpoint antes de M5

[Índice](../guide.md) · [← Anterior](43-m3d5-resolucion-manual-autenticada-de-unknown.md) · [Siguiente →](45-m5a-chroma-como-servicio-e-inventario-de-conocimiento.md)


### Paso 1 evaluar el recorrido completo por su frontera HTTP

Las suites anteriores conservan nueve casos de lectura y ocho de escritura. Añadimos cuatro casos en `evals/authenticated_cases.json`, ejecutados por `scripts/evaluate_authenticated.py`.

Este cierre atraviesa la API con credenciales emitidas en un directorio temporal, los workers de lectura/escritura y los procesos MCP Python/Go. HTTP se ejercita mediante TestClient/ASGI dentro del proceso, con middleware, validación y rutas reales; **no es una prueba de sockets, proxy o despliegue remoto**.

El objetivo es medir las garantías del recorrido autenticado, no solo verificar que el último estado diga SUCCEEDED.

| Caso | Resultado y evidencia esperados |
| --- | --- |
| AUTH-approved | Otro usuario aprueba; un request, un efecto y job DONE. |
| AUTH-revoked-before-send | Se retiran permisos del aprobador después de admitir; NOT_APPLIED, cero requests/efectos. |
| AUTH-recovery-revoked | Se pierde la confirmación después del efecto y se revocan ambos actores/sesiones; un runtime nuevo consulta el recibo sin reenviar. |
| AUTH-manual-closure | Interrupción antes de enviar, UNKNOWN/NEEDS_REVIEW, cierre revisado por otro actor y NOT_APPLIED/DONE sin efecto. |

Todos los casos comprueban además credencial ausente, otro workspace, otro actor del mismo workspace, rol de lectura insuficiente y autoaprobación incluso con ambos roles. El revisor consulta la propuesta por su API antes de decidir.

### Paso 2 separar llamadas, efectos y estados

Instrumentamos el cliente MCP para contar revision/request/lookup/close sin reemplazar el transporte ni la validación. Medimos los efectos aparte en la SQLite del proveedor. Las expectativas exactas están en el dataset versionado; no se calculan a partir del estado que devuelve Operations.

| Caso | Revision | Request automático | Lookup | Close | Efectos |
| --- | ---: | ---: | ---: | ---: | ---: |
| approved | 1 | 1 | 0 | 0 | 1 |
| revoked-before-send | 1 | 0 | 0 | 0 | 0 |
| recovery-revoked | 1 | 1 | 1 | 0 | 1 |
| manual-closure | 1 | 0 | 1 | 2 | 0 |

La sonda adversarial de envío al ID cerrado se mide aparte como `probe_calls`; debe ser rechazada. No se presenta como un request automático del worker ni como efecto aceptado.

La recuperación admite aplazamientos legítimos por el lease, dentro del máximo persistido de cinco reclamos. No tolera solicitudes extra, búsquedas duplicadas, reset de intentos o un resultado terminal incorrecto. Un SUCCEEDED con dos requests o efectos duplicados falla.

### Paso 3 verificar identidad, evidencia e idempotencia

Cada caso pausa la lectura tras Jira, recupera mediante la API y exige conservar el primer resultado, los conteos 4/3 y el snapshot de límites. Después de la escritura/revisión compara el run completo para detectar cambios en aquella evidencia.

También exige una sola propuesta/operación/job para la intención repetida, vinculación de los requests al ID/hash/argumentos aprobados y actor de decisión derivado de la sesión. La autoaprobación no se supera cambiando headers o acumulando ambos roles.

El caso manual comprueba otra revisión, cierre que bloquea el envío tardío y job terminado en la misma confirmación. El caso de revocación posterior al efecto demuestra que seguir el recibo no depende de conservar habilitado al usuario. Los ciclos posteriores del servicio no deben hacer más I/O.

### Paso 4 aislar y limpiar cada evaluación

Cada caso usa un schema `harness_eval_<uuid>`, un proveedor en un directorio temporal y credenciales que solo existen en memoria y en ese schema. No emite archivos de sesión en el runtime real ni usa los usuarios/historial de las consolas.

La aplicación recibe el directorio de prueba explícitamente y el worker usa ese mismo almacenamiento. Los runtimes de recuperación se reconstruyen a partir de él; no se conserva una autorización ficticia en variables para eludir permisos retirados.

Los context managers cierran la API y eliminan el schema/proveedor al terminar. Un fallo al ejecutar o limpiar el caso produce EVALUATION_EXECUTION_ERROR y no se devuelve PASS. El reporte omite tokens, session IDs, DSN, rutas temporales y UUID de objetos del caso; conserva hechos y checks sanitizados. Una terminación forzada o una base inaccesible puede impedir la limpieza, como en los evaluadores anteriores; no se agrega borrado masivo de schemas ajenos.

### Paso 5 ejecutar las tres líneas base

Con Python/Go, PostgreSQL local y dependencias de desarrollo preparados:

```bash
uv sync --locked
uv run python scripts/postgres.py up
uv run python scripts/build_jenkins.py
uv run python scripts/evaluate.py
uv run python scripts/evaluate_writes.py
uv run python scripts/evaluate_authenticated.py
```

`uv sync` incluye el grupo de desarrollo que necesita TestClient/httpx. No hace falta iniciar API, workers de la app, Jaeger ni Grafana: el evaluador crea su API ASGI y ejecuta los consumidores dentro de su entorno aislado. Los servicios existentes pueden seguir activos porque sus schemas son distintos.

Los reportes privados son:

- `data/runtime/evals/latest.json`: nueve casos de lectura.
- `data/runtime/evals/writes-latest.json`: ocho casos de escritura.
- `data/runtime/evals/authenticated-latest.json`: cuatro casos autenticados.

Son **21 evaluaciones**, no 21 pruebas unitarias ni evidencia de un LLM real. `checks` explica cada criterio; `observed` muestra lo medido. El tiempo es informativo y no se usa como SLA. El comando termina con código 1 ante fallos, dataset inválido/incompleto o cero casos; con código 0 solo si todo pasa.

### Paso 6 comprobar que el grader puede rechazar

Las pruebas del evaluador alteran estados, efectos, llamadas, claims, evidencia y denegaciones. Verifican que un estado final aparentemente correcto no oculte un error de seguridad o un reenvío.

Otras pruebas fuerzan errores del entorno/dataset y de limpieza, comprueban salida no exitosa y ausencia de excepciones crudas. Las integraciones ejecutan el recorrido real y verifican que su schema ya no exista al terminar.

```bash
uv run python -m unittest discover -s tests -p 'test_authenticated_evaluations.py' -v
uv run python -m unittest discover -s integration_tests -p 'test_authenticated_evaluations.py' -v
```

### Paso 7 guardar el checkpoint y conservar límites

El checkpoint local reúne M3d1–M3d5 y las evaluaciones autenticadas: identidad local, ownership, dos actores, cola, servicio de escritura, recuperación y resolución manual. Antes de guardarlo se revisa el árbol publicable completo, enlaces y exclusión de runtime, credenciales, bases y claves. El autor de Git conserva correo noreply. Publicar en GitHub es una acción separada de guardar este commit local.

Esto cierra el recorrido **local con backends/modelo ficticios** que necesitábamos para avanzar. No declara M3/M4/S09 completos para producción: login web/OIDC, roles administrativos, frontend multiusuario, acceso autorizado a trazas, despliegue y contratos de proveedores reales permanecen pendientes.

### Paso 8 continuar con M5 sin ampliar este cierre

El siguiente hito funcional es **M5: conocimiento/RAG**. Retomaremos el diseño original con Chroma como servicio, registro/versiones del corpus, ingestión y retirada, citas y acceso por workspace/documento.

Los criterios S10 y S11 siguen guiando el trabajo: un ingest fallido no reemplaza una versión activa válida; retirar un documento lo excluye de recuperación y un job viejo no puede reactivarlo. Las mejoras pendientes de login, frontend o monitores no se incorporan como bloqueos nuevos para empezar ese hito.

Seguiremos por incrementos y documentando en esta misma guía del proyecto. No se crean copias del paso a paso fuera del repositorio.

Validación de cierre previo a M5: **235 pruebas locales, 148 PostgreSQL y 21/21 evaluaciones aprobadas** (9 lectura, 8 escritura y 4 autenticadas). El runner nuevo usa HTTP ASGI y procesos MCP reales del laboratorio, con schemas/proveedores temporales. Dos integraciones comprueban ejecución/limpieza y fallo de limpieza sin PASS; siete pruebas del grader/CLI verifican detección de regresiones y errores sanitizados. Las suites Compose/rotación no se repitieron, porque esta parte no modifica infraestructura. Se revisa el árbol completo del checkpoint antes de guardarlo; los reportes, sesiones, claves, certificados y bases siguen fuera de Git. M5 es el próximo hito funcional.
