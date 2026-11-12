# M3c OpenTelemetry y Jaeger

[Índice](../guide.md) · [← Anterior](26-m3c-renovacion-programada-y-rotacion-en-vivo.md) · [Siguiente →](28-m3c-prometheus-y-grafana.md)


### Paso 1 separar observación y control

Ya podíamos ejecutar, limitar y recuperar un run. Ahora instrumentamos su recorrido para ver el orden, las duraciones, los fallos y la relación entre procesos.

**OpenTelemetry** aporta las APIs/SDK para producir spans y propagar contexto. **Jaeger** recibe las trazas mediante OTLP y permite explorarlas visualmente. La [configuración all-in-one de Jaeger 2.23](https://www.jaegertracing.io/docs/2.23/getting-started/) reúne recepción, consulta y UI con almacenamiento transitorio en memoria. Para este incremento es una pieza suficiente; un collector separado y almacenamiento persistente quedan para otra evolución.

La observabilidad no participa en decisiones de autorización, CAS, leases ni recuperación. El estado durable sigue en PostgreSQL. Jaeger puede apagarse, perder trazas o reiniciarse sin invalidar un checkpoint.

### Paso 2 instalar y fijar dependencias

Agregamos `opentelemetry-sdk` y el exporter `opentelemetry-exporter-otlp-proto-http` como dependencias de ejecución, compatibles con la serie 1.x y fijadas exactamente por `uv.lock`. La versión resuelta del SDK/exporter es 1.45.2. No agregamos instrumentación automática de FastAPI, HTTP o SQL.

```bash
uv sync --locked
uv run python scripts/stack.py up
```

Compose añade `cr.jaegertracing.io/jaegertracing/jaeger:2.23.0`. La UI está en **http://127.0.0.1:16686** y la consola del harness continúa en **http://127.0.0.1:8002**. El receptor OTLP/HTTP está en `jaeger:4318/v1/traces`, dentro de la red de Compose; no publicamos ese puerto en el host. Las llamadas worker→emisor→Jira mantienen TLS.

Jaeger no es una dependencia de salud para el worker. Los servicios pueden arrancar y ejecutar trabajo aunque el collector no esté disponible. En Compose activamos `HARNESS_TRACING=1`; fuera de ese entorno la exportación queda desactivada por defecto. La CLI/SQLite habitual conserva su comportamiento.

### Paso 3 entender trace, span, parent y link

| Concepto | Significado en el laboratorio |
| --- | --- |
| Trace | Conjunto de operaciones de una solicitud o un intento del worker. |
| Span | Una operación con inicio, fin y metadatos permitidos. |
| Parent/child | Relación dentro de la misma traza, por ejemplo tool → obtención de token. |
| Span link | Relación causal entre trazas, por ejemplo productor API → intento del worker. |
| Trace ID / Span ID | Identificadores de observación, distintos de `run_id` y `call_id`. |
| `traceparent` | Header W3C que transporta esos identificadores y flags. |

Elegimos **una traza por intento del worker** y otra para la solicitud de API que encola o recupera. Cada intento termina su traza al pausarse/completarse. El siguiente crea una nueva y enlaza tanto su productor actual como el intento anterior.

```text
API create [traza A]
    └─ contexto guardado junto al job
           └─ link → worker intento 1 [traza B]
                         ├─ model.decide
                         ├─ policy.authorize
                         └─ tool.invoke Jira
                               ├─ identity.get_token → identity.token
                               ├─ mcp.discover → mcp.http
                               └─ mcp.call → mcp.http → mcp.tool

API resume [traza C]
    └─ links → worker intento 2 [traza D]
                   ├─ link al intento B
                   └─ trabajo pendiente; evidencia confirmada reutilizada
```

`traceparent` **no autentica ni concede permisos**. Los roles/workspace siguen procediendo de nuestras identidades verificadas. Propagamos solo ese header validado; no `baggage`, `tracestate`, tokens o atributos arbitrarios. La API de navegador abre su propia raíz y no acepta que un header externo elija su parent.

El SDK actual usa flags W3C que pueden incluir el bit de identificador aleatorio (`03`), además del muestreo. Admitimos flags hexadecimales válidos y validamos el contexto con el propagador oficial; una versión/forma no admitida se descarta sin bloquear el trabajo.

### Paso 4 instrumentación explícita

`harness/telemetry.py` centraliza configuración, spans, allowlist y middleware ASGI. Los servicios se identifican como `harness-api`, `harness-worker`, `harness-identity` y `harness-jira`.

| Span | Qué mide |
| --- | --- |
| `api.runs.create` / `api.runs.resume` | Admisión o recuperación solicitada a la API. |
| `worker.attempt` | Ejecución de un claim del worker. |
| `model.decide` | Una llamada al puerto de modelo; el modelo sigue guionado. |
| `policy.authorize` | La comprobación de permiso en propuesta y antes de la lectura pendiente. |
| `tool.invoke` | Invocación completa, dentro del timeout del core. |
| `identity.get_token` / `identity.token` | Solicitud del worker y atención en el emisor. |
| `mcp.discover` / `mcp.call` | Discovery y RPC desde el host. |
| `mcp.http` / `mcp.tool` | Atención HTTP y ejecución del backend MCP en Jira. |

El SDK MCP puede hacer varias solicitudes HTTP para negociación, discovery y llamada. Esas solicitudes son spans distintos dentro de una misma invocación lógica: no aumentan por sí solas `tool_calls`. El contexto HTTP se fija para la conexión bajo la operación de herramienta; los spans de transporte y del host muestran ese recorrido sin inventar una llamada de dominio por cada POST.

GitHub y Jenkins mantienen stdio. Sus duraciones se observan desde el adaptador del worker; no agregamos todavía un exporter al proceso Go ni propagación entre procesos stdio. Tampoco instrumentamos el planificador local o cada refresh de JWKS: evitamos llenar la vista con polling de infraestructura.

La relación de herramientas sigue siendo secuencial. Una superposición entre spans de cliente y servidor refleja la solicitud distribuida; este incremento no introduce ejecución paralela de tools.

### Paso 5 conservar contexto a través de PostgreSQL

La migración SQL `003_traces.sql` sube el esquema PostgreSQL a versión 3 y añade columnas de observación en `jobs`:

- `producer_traceparent`: contexto de la solicitud que encoló/reencoló el job.
- `previous_traceparent`: raíz del último intento instrumentado.
- `telemetry_enqueued_at`: instante del encolado/reencolado para medir antigüedad al reclamar.

El contexto productor se escribe en la misma transacción que el job. Al comenzar otro intento, el worker recibe los contextos anteriores y crea links; luego registra el suyo con el mismo guard de lease/fencing. Un worker viejo no puede sustituir el contexto del nuevo.

La escritura del contexto del intento es best effort. Si falla, seguimos pasando por los controles normales de ejecución; ese dato nunca otorga autoridad para guardar resultados. Un link puede faltar si los metadatos no se pudieron registrar.

`job.age_ms` mide el tiempo desde encolado hasta ese claim. En recuperación por lease vencido puede incluir tiempo del intento anterior: hay que interpretarlo como antigüedad del job desde el último encolado, no como espera pura de CPU o cola.

El JSON del `Run`, los checkpoints, sus límites y su versión de contrato permanecen iguales. Estas columnas internas no aparecen en el payload de `job` de la UI. Los jobs existentes pueden tener contextos nulos: se trazan desde su próximo intento sin reconstruir un pasado que no registramos.

### Paso 6 privacidad y errores útiles

La allowlist exporta únicamente metadatos como:

- `run.id`, intento y fencing token.
- Antigüedad del job, número de resultados confirmados y contadores de llamadas.
- Nombre conocido de herramienta y `call_id`.
- Estado/outcome, código de error conocido y status HTTP.

No exportamos prompts, respuestas, argumentos/resultados de tools, descripciones de Jira, repositorios, jobs de Jenkins, identidad humana/workspace, Authorization, JWT, secretos, claves, URLs completas o DSN/SQL. Los nombres de spans son constantes. Los recursos contienen servicio y entorno `lab`, sin detección automática del equipo o usuario.

Desactivamos el registro automático de excepciones: un mensaje o stacktrace puede contener datos sensibles. El span fallido recibe estado ERROR y un código enumerado, sin copiar `str(error)`. Un error de política queda visible en `policy.authorize`; una herramienta que falla puede verse roja aunque el informe general termine con evidencia parcial autorizada.

En este laboratorio los modelos son guionados. Medimos llamadas y duración del puerto; no inventamos tokens de un proveedor real ni enviamos información a un servicio externo de tracing.

### Paso 7 exportar sin convertir Jaeger en control del run

Usamos el [exporter OTLP/HTTP y BatchSpanProcessor de OpenTelemetry](https://opentelemetry.io/docs/languages/python/exporters/): cola máxima de 256 spans, batches de hasta 64, envío periódico de 500 ms y timeout de red de un segundo. El envío ocurre en segundo plano, sin proxies heredados y hacia un endpoint de infraestructura permitido.

Los fallos del tracer al iniciar/cerrar un span no sustituyen resultados ni excepciones de negocio. No hacemos `force_flush` por herramienta ni esperamos a Jaeger antes de confirmar un checkpoint. El overhead de instrumentación existe, pero los awaits, timeouts, presupuestos y verificaciones del core mantienen sus posiciones y reglas.

La exportación es best effort. Un proceso que muere con `os._exit`, una cola llena o un collector caído pueden dejar spans ausentes/incompletos. La última traza enlazada puede no existir en Jaeger. Esto es compatible con recuperar el run desde PostgreSQL: tracing no sustituye el ledger ni una auditoría durable.

Jaeger all-in-one guarda en memoria por defecto. `down` o reiniciar su contenedor pierde sus trazas (verificado con Jaeger 2.23: un volumen en `/tmp` no las conserva); PostgreSQL conserva runs/evidencia. El canal OTLP es interno de Compose y transporta la allowlist de metadatos. Un despliegue remoto requeriría controles propios para acceso/cifrado/retención de telemetría.

### Paso 8 explorar una ejecución en Jaeger

1. Levantá el stack y creá un **run nuevo** desde http://127.0.0.1:8002.
2. Abrí http://127.0.0.1:16686.
3. Buscá el servicio `harness-worker` y la operación `worker.attempt`.
4. Filtrá por el atributo `run.id` usando el UUID de la consola.
5. Abrí el intento y expandí sus spans: modelo, políticas, herramientas, emisor y Jira.
6. Revisá duración, timestamps, estado, atributos y links del span raíz.

La exportación por servicio es asíncrona: puede tardar unos segundos en verse todo y una consulta temprana puede mostrar una traza parcial. Cambiá/refrescá el intervalo de búsqueda si no aparece. Los runs completados antes de instrumentar no reciben trazas retroactivas.

### Paso 9 ver la recuperación sin repetir evidencia

En la consola, elegí `Evidencia coincidente`, pausa después de Jira e iniciá el run.

En el primer intento deberían verse una decisión del modelo y una invocación Jira, con sus requests al emisor/MCP. Al recuperar desde la consola:

- La primera evidencia permanece idéntica.
- Aparece otra traza `worker.attempt`, enlazada a la anterior y a `api.runs.resume`.
- `run.confirmed_results=1` indica la evidencia al entrar al intento.
- El segundo intento ejecuta GitHub/Jenkins y las decisiones restantes, sin otra consulta Jira ni otra obtención de token para ella.
- El run completo conserva cuatro llamadas de modelo y tres herramientas.

Si una lectura quedó pendiente por muerte del worker, se conservan `call_id` y las reglas de nueva reserva/cobro. Las pruebas de PostgreSQL siguen matando procesos y comprobando recuperación/fencing; no inferimos el estado de un run mirando si su span llegó a Jaeger.

Para un rechazo visible, ejecutá `Ataque: job sin permiso`: la política debe registrar `FORBIDDEN` sin una segunda invocación de herramienta. Para informe parcial, `Jenkins no disponible` mantiene su política de fallo opcional.

### Paso 10 pruebas reales y collector fuera de servicio

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s integration_tests -v
uv run python -m unittest discover -s container_tests -v
```

**Verificación final: 171 pruebas del laboratorio, 40 PostgreSQL y 12 de Compose pasaron.**

Las pruebas nuevas verifican propagación W3C, aislamiento async, links, filtrado de secretos, excepciones sanitizadas y un tracer defectuoso sin alterar negocio. Las de PostgreSQL comprueban la migración desde versión 2, fencing de metadatos y recuperación con nueva traza/evidencia intacta. Una prueba mata de verdad al proceso con `os._exit` después de confirmar Jira; el siguiente worker enlaza el contexto persistido y conserva la primera evidencia sin repetir la herramienta.

Las de Compose consultan la API v3 de Jaeger 2.23, comprueban los tres servicios dentro de un intento y la traza productora API por link, validan permisos fallidos y apagan Jaeger durante pausa/recuperación. Después lo levantan y dejan una traza nueva verificable. Su reinicio borra el historial de trazas de memoria y la suite crea runs sintéticos en PostgreSQL. Las pruebas de rotación mantienen su requisito de esperar la ventana entre ejecuciones de la suite.

El siguiente incremento puede añadir Prometheus/Grafana para contadores, histogramas y alertas. Las trazas responden qué ocurrió en una ejecución; las métricas permitirán observar tendencias del conjunto.
