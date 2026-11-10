# M5d RAG como herramienta del core

[Índice](../guide.md) · [← Anterior](47-m5c-api-y-frontend-de-documentos-e-ingestion.md) · [Siguiente →](49-m5e-consola-autenticada-unificada.md)

## 1. Qué conecta este paso

Hasta M5c el usuario buscaba desde la biblioteca. Ahora un perfil del agente puede proponer `knowledge_search` como otra herramienta de lectura. El recorrido didáctico completo es Jira → GitHub → Jenkins → conocimiento → cierre con evidencia CI y fragmentos citados.

El modelo sigue siendo **guionado**, con consulta fija `responsable release Atlas`. Esto conecta el contrato de una herramienta RAG al harness y permite forzar decisiones adversariales reproducibles; todavía no demuestra un LLM real eligiendo libremente si buscar, reformular o buscar otra vez. Los embeddings continúan siendo léxicos. La generación/modelo real y su evaluación siguen reservados al paso correspondiente.

Creamos un perfil nuevo `mcp-devops-rag-v1`. Los perfiles CI anteriores conservan composición, decisiones y límites. `harness/rag_agent.py` contiene política, adaptador, modelo guionado y cierre; `harness/composition.py` los conecta al mismo `Harness` existente. El adaptador documental es nativo del host e implementa `ToolPort`; consulta PostgreSQL y Chroma como servicio. Esta herramienta no requiere un servidor MCP nuevo. Jira/GitHub/Jenkins mantienen sus adaptadores MCP.

## 2. Contrato sin autoridad en los argumentos

La única forma admitida es:

```json
{"query":"responsable release Atlas","limit":3}
```

`query` debe ser texto no vacío de hasta 2000 caracteres; `limit` es un entero de 1 a 3, excluyendo booleanos. No se admiten actor, workspace, roles, rutas, SQL, URLs ni selección de índice. La identidad viene del directorio actual y el índice de la autoridad PostgreSQL.

La política del perfil vuelve a resolver permisos actuales para todas sus llamadas, incluidas las DevOps; exige que coincidan con la identidad de ejecución. El adaptador repite la autorización antes de recuperar contenido. `ReadOnlyPolicy` de los perfiles anteriores no anuncia ni concede la herramienta RAG. Añadirla al conjunto de lecturas recuperables tampoco concede permisos por sí solo: tiene que estar registrada en la composición y autorizada por la política.

El resultado guarda query/limit, versión de índice y hasta tres fragmentos con nombre, cita, distancia y `trust: untrusted_document`. El sobre `ToolResult` conserva `call_id`, nombre/versionado y una referencia de recuperación correlacionada con índice y hash de consulta. Validamos forma, tamaño, citas únicas, distancia finita y procedencia. Cada fragmento debe corresponder exactamente al chunk PostgreSQL autorizado y su hash.

## 3. Presupuesto explícito de cinco decisiones

Una conversación guionada con cuatro herramientas necesita cinco decisiones:

```text
modelo → Jira → modelo → GitHub → modelo → Jenkins → modelo → RAG → modelo → cierre
```

El perfil nuevo se admite con `Limits(model_calls=5)`, persistido en `limits_snapshot`. Conserva ocho intentos de herramientas, timeout de tool de diez segundos, timeout de modelo de treinta y presupuesto activo global de 120 segundos. No aumenta automáticamente un límite al encontrar RAG. Una prueba ejecuta el mismo perfil con el default anterior de cuatro decisiones y exige BUDGET_EXCEEDED antes del cierre, sin respuesta inventada.

La llamada RAG consume una reserva/intento del mismo contador `tool_calls`. Una relectura incierta tras fallo consume otro intento, igual que Jira/GitHub/Jenkins. Las revalidaciones del contexto se incluyen dentro de la reserva/timeout persistidos de la decisión del modelo; no añadimos comprobaciones costosas fuera de esa reserva antes de alimentar el modelo. La autorización de entrada/recuperación sigue siendo una condición para acceder al run.

`Knowledge.asearch()` separa preparación SQL, consulta HTTP asíncrona y comprobación final SQL, reutilizando las reglas de `search()`. Chroma usa un cliente async con timeout y sin redirects/proxies/reintentos implícitos. El timeout del core cancela la consulta HTTP pendiente y no guarda un resultado tardío. Las lecturas PostgreSQL se ejecutan fuera del event loop; una lectura SQL ya iniciada puede terminar tras cancelar la tarea, sin producir una respuesta ni iniciar nueva consulta vectorial desde la tarea cancelada. No hay efectos DevOps en esta tool.

## 4. Evidencia guardada y permisos actuales

Guardar un fragmento para recuperación no lo convierte en contenido permanentemente autorizado. La política verifica la evidencia documental:

- Al normalizar el resultado, antes de guardarlo como evidencia del run.
- Al autorizar un run recuperado o consultado por la API.
- Antes de entregar resultados guardados al modelo, dentro de su reserva temporal.
- Después de la decisión final del modelo, antes de publicar el cierre.

Comprueba workspace, rol/identidad actual, documento ACTIVE, dueño/ACL, índice histórico READY, chunk, nombre y hash/texto exactos. Retirada o permiso documental perdido producen EVIDENCE_REVOKED y bloquean la entrega. Un payload/cita corruptos producen INVALID_TOOL_OUTPUT. Los endpoints devuelven solo el error sanitizado, sin texto documental.

Si solo cambia el puntero al índice activo y el documento sigue permitido, se conserva la cita histórica que obtuvo ese run. No sustituimos el resultado guardado por el de otra versión durante recuperación. Las búsquedas nuevas sí usan el índice activo; un run recuperado puede usar su snapshot documental todavía autorizado. Esto exige conservar los chunks históricos mientras los runs los referencien; la futura retención tendrá que respetarlo. La comprobación es una decisión de autorización al momento de cada acceso, no una promesa de borrar texto que ya se entregó antes a un cliente.

## 5. Instrucciones del documento y cierre del resultado

Los fragmentos se tratan como datos incluso si dicen «ignora las políticas» o solicitan ejecutar un job. Probamos modelos adversariales que, después de recibir el documento, fuerzan:

1. `jenkins_request_tests`: UNKNOWN_TOOL; este perfil carece de esa capacidad.
2. `jenkins_get_build` sobre un job fuera del alcance: FORBIDDEN.
3. `knowledge_search` con un workspace inyectado: INVALID_ARGUMENT.

No basta con que el modelo guionado ignore el ataque: se fuerza la propuesta prohibida y se comprueba que no llegue a un adaptador autorizado. Estos son tests deterministas de fronteras, no una evaluación de resistencia de un LLM real a prompt injection.

El cierre `finalize_rag()` conserva la regla CI existente sobre revisión de PR/build. Añade extractos literales cortos, marcados como contexto no confiable, y citas tomadas de resultados validados. El texto final propuesto por el modelo no puede inventar un despliegue ni fabricar una cita. Un resultado vacío informa que no hay fragmentos accesibles; no se convierte en evidencia afirmativa. Una caída del servicio produce fallo de la lectura, no una falsa lista vacía.

Este perfil no se usa para crear propuestas M4. La frontera de propuestas existente mantiene su política CI y rechaza la evidencia RAG; los documentos no añaden autoridad de aprobación o escritura. Conectar conocimiento como contexto a futuras propuestas requerirá un contrato propio y evaluaciones, conservando el ledger.

## 6. Probar por API y consola

La biblioteca de M5c sigue sirviendo carga/indexación/búsqueda. Este incremento conecta el perfil mediante API y cliente de consola; no añade todavía un botón de agente a esa pantalla.

Primero comprobá que haya documentos indexados, por ejemplo el Markdown de Atlas del capítulo anterior. API y worker de lectura deben usar el código actualizado. El worker de ingestión y el de lectura tienen funciones distintas:

```bash
# API autenticada, si todavía no está levantada:
uv run python -m harness.web --storage postgres --user-auth --port 8005

# Worker de runs: consulta herramientas y recupera sus checkpoints.
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha

# El worker de ingestión anterior sigue reconstruyendo índices, cuando haya jobs suyos:
uv run python -m harness.knowledge_worker --actor reader-demo --workspace workspace-alpha
```

Emití una sesión local, conservando el archivo privado que informa el comando:

```bash
uv run python scripts/users.py issue --actor reader-demo --workspace workspace-alpha
SESSION_ID=PEGA_AQUI_EL_SESSION_ID
uv run python scripts/rag_agent.py --session "$SESSION_ID" start
```

`start` admite un run en PostgreSQL con 202; no ejecuta herramientas en FastAPI. Copiá su `run_id`:

```bash
uv run python scripts/rag_agent.py --session "$SESSION_ID" status RUN_ID
uv run python scripts/rag_agent.py --session "$SESSION_ID" resume RUN_ID
uv run python scripts/users.py revoke "$SESSION_ID"
```

`status` permite observar la respuesta y `results` del run propio. `resume` sirve para trabajos pausados o con lease vencido; RUN_BUSY protege un consumidor vivo, y COMPLETED devuelve su resultado autorizado sin repetir trabajo. No hace falta `resume` para un run nuevo que el worker atienda normalmente. Como en la admisión original de runs, repetir `start` crea otra ejecución: no tiene clave idempotente ni reintento automático del cliente.

Rutas nuevas, exclusivas de `--user-auth`:

| Método y ruta | Contrato |
| --- | --- |
| POST `/api/knowledge/agent/runs` | Cuerpo `{}`; perfil y límites fijos del servidor. |
| GET `/api/knowledge/agent/runs/{run_id}` | Snapshot propio, previa autorización actual de la evidencia guardada. |
| POST `/api/knowledge/agent/runs/{run_id}/resume` | Cuerpo `{}`; reencolar según el contrato durable de lecturas. |

Los runs RAG se consultan por estas rutas; la consola/rutas CI anteriores conservan su política y no muestran esos resultados. Se mantiene sesión actual en HTTP, actor/workspace/ownership, límite de 16 admisiones y el worker con directorio actual, leases/fencing y deadlines existentes. El worker no recibe ni conserva el bearer del cliente.

Esperado: COMPLETED, cinco decisiones reservadas, cuatro llamadas de herramientas, evidencia Jira/GitHub/Jenkins y fragmentos de Marta con citas. `knowledge_status` es RETRIEVED o NO_MATCHES, y `knowledge_citations` conserva las referencias documentales. RETRIEVED significa «recuperado y autorizado», no «afirmación semánticamente verificada».

## 7. Recuperación y límites persistidos

Las pruebas cubren dos puntos:

- **Después de guardar el resultado RAG:** recuperar reutiliza los mismos fragmentos/call_id/citas sin consultar Chroma otra vez. Termina con 5 decisiones y 4 herramientas si queda presupuesto.
- **Antes de invocar la tool con un intento reservado:** el nuevo intento conserva call_id, cobra otra reserva de herramienta y descuenta el tiempo incierto. Puede terminar con 5 decisiones y 5 intentos de herramientas.

Los presupuestos no se reinician al recuperar. Una interrupción puede agotar decisiones o tiempo y terminar conservadoramente en fallo; no se garantiza completar si ya no queda presupuesto. La retirada durante la decisión final impide publicar aunque el modelo haya empezado a decidir con una versión previamente autorizada.

## 8. Trazas y próximos pasos

`knowledge_search` se añade a las listas fijas de nombres permitidos para trazas/métricas. El core produce su span `tool.invoke` y la operación contabilizada correspondiente, igual que las tools anteriores. Los nombres de spans de admisión/recuperación HTTP son fijos `api.rag.create` / `api.rag.resume`; el job conserva el enlace productor/consumidor existente. No añadimos query, fragmentos, nombres de archivo, claves ni bearer a atributos o labels. El span de decisión incluye la revalidación de su contexto dentro de su presupuesto. El dashboard/exporter debe estar conectado al proceso que ejecuta el perfil; no se incorpora automáticamente el worker autenticado a un scrape nuevo.

La comprobación automatizada de la traza usa la [API v3 documentada de Jaeger](https://www.jaegertracing.io/docs/2.23/architecture/apis/), disponible en el servicio local.

Para exportar desde el worker del host al Jaeger local se pueden usar las variables existentes `HARNESS_TRACING=1` y `HARNESS_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces`. Se mantiene independiente del procesamiento: un exportador caído no concede ni bloquea autoridad.

```bash
uv run python -m unittest tests.test_rag_agent integration_tests.test_rag_agent -v
```

El siguiente incremento será embeddings semánticos y evaluaciones de recuperación/citas frente a esta línea base, o una pequeña presentación de los runs RAG en la UI. Siguen pendientes LLM real, relevancia/reranking/hybrid según evaluaciones, cancelación/retención y observabilidad dedicada de ingestión. No declaramos completado agentic RAG con un modelo guionado.

Validación M5d: **243 pruebas locales aprobadas y 195 integraciones distintas aprobadas**: suite completa de 194, más una prueba añadida de recuperación HTTP/worker; tras aislar las rutas CI se repitieron las dos integraciones de API RAG y 18 pruebas web/identidad. Los casos nuevos comprueban citas, presupuesto de cuatro decisiones insuficiente, ausencia de documentos, contexto guardado, reserva incierta/call_id, retirada antes de reutilizar y durante el cierre, timeout cancelable, ataques forzados y afirmación falsa. La prueba HTTP/worker pausó después del resultado RAG y recuperó con segundo claim, mismos resultados y 5/4, sin repetir búsqueda. El recorrido real API/CLI con MCP Python/Go y Chroma terminó COMPLETED 5/4; la traza Jaeger mostró cinco decisiones, cuatro tools y enlace al productor. Se actualizaron únicamente API autenticada y worker de lecturas del host; ingestión/ledger y servicios anteriores conservan sus contratos. Las sesiones de verificación se revocaron; quedaron dos runs ficticios COMPLETED, cero jobs de lectura/ingestión activos y cero schemas RAG temporales. Gitleaks no encontró secretos en los 88 archivos publicables nuevos/modificados del conjunto M5 y los enlaces locales revisados existen. No se repitieron las suites de rotación/contenedores ni los runners independientes de evals; la suite PostgreSQL incluye sus pruebas existentes. Sin commit ni publicación.
