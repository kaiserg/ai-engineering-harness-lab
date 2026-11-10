# M5b Tipo de RAG e ingestión durable

[Índice](../guide.md) · [← Anterior](45-m5a-chroma-como-servicio-e-inventario-de-conocimiento.md) · [Siguiente →](47-m5c-api-y-frontend-de-documentos-e-ingestion.md)


### Qué clase de RAG estamos construyendo

Nuestra base sigue el recorrido simple que suele llamarse **naive RAG** o **RAG en dos pasos**: recuperar fragmentos y después generar una respuesta con ellos. Hoy solo está implementada la recuperación con citas; todavía no se genera una respuesta usando ese contexto. Los vectores actuales son una demo léxica de hashing, no embeddings semánticos. La cola de este incremento mejora la operación de la ingestión, sin cambiar la estrategia de recuperación.

Estos nombres describen dimensiones distintas y se pueden combinar. Por ejemplo, un agente puede elegir una búsqueda híbrida y usar documentos multimodales. «Más avanzado» no implica automáticamente mejor relevancia, menor costo o mayor seguridad.

| Variante | Qué añade | Estado en nuestro laboratorio |
| --- | --- | --- |
| Naive / dos pasos | Un recorrido fijo de recuperación de chunks, seguido de generación con contexto. | Base de recuperación disponible; falta conectar generación y embeddings semánticos. |
| Corrective | Evalúa si lo recuperado es adecuado y aplica una estrategia correctiva cuando no lo es. | No implementado. Validar ACL/hash no evalúa relevancia y no nos convierte en Corrective RAG. |
| HyDE | Un LLM genera un documento hipotético a partir de la pregunta; su embedding se usa para buscar documentos reales. | No implementado. La respuesta hipotética no sería una fuente ni una cita válida. |
| GraphRAG | Usa relaciones/estructura de grafo para recuperar o sintetizar conocimiento; Microsoft GraphRAG añade comunidades y resúmenes. | No implementado; no tenemos grafo de conocimiento. |
| Hybrid retrieval | Combina señales de búsqueda, habitualmente léxica como BM25 y vectores semánticos, fusionando rankings. | No implementado. Usar PostgreSQL y Chroma juntos es separación de almacenamiento/autoridad, no búsqueda híbrida. Nuestro vector léxico es una única señal. |
| Adaptive | Elige estrategias o cantidad de recuperación según la pregunta y la evidencia disponible. | No implementado. Usamos un recorrido fijo con un límite explícito de resultados. |
| Agentic | El agente decide cuándo buscar, qué consultar y si necesita búsquedas adicionales. | Futuro: la búsqueda deberá ser una tool contabilizada y autorizada por el core. Tener workers no hace agentic al RAG. |
| Multimodal | Recupera/utiliza conocimiento de varias modalidades, por ejemplo texto e imágenes. | No implementado: solo texto UTF-8 MD/TXT. |

Fuentes para repasar: [RAG original](https://arxiv.org/abs/2005.11401), [Corrective RAG](https://arxiv.org/abs/2401.15884), [HyDE](https://arxiv.org/abs/2212.10496), [Microsoft GraphRAG](https://www.microsoft.com/en-us/research/publication/from-local-to-global-a-graph-rag-approach-to-query-focused-summarization/), [búsqueda híbrida en Azure AI Search](https://learn.microsoft.com/azure/search/hybrid-search-overview), [Adaptive-RAG](https://arxiv.org/abs/2403.14403), [ejemplo de Agentic RAG de NVIDIA](https://github.com/NVIDIA-AI-Blueprints/rag/blob/main/docs/agentic-rag.md) y [consultas multimodales de NVIDIA](https://docs.nvidia.com/rag/latest/multimodal-query.html). Son referencias conceptuales; este repo no incorpora esos frameworks ni implementaciones.

Para explicar el proyecto en una entrevista: «Construyo desde el core un harness con recuperación documental simple. Primero separé autoridad e índice y probé ACL/versiones/retirada. Después añadí ingestión durable. Incorporaré el retrieval como herramienta del agente con límites y evals; no llamo agentic al componente hasta que esa decisión exista».

### 1. Por qué añadimos una cola

Antes `rebuild` hacía todo dentro del proceso de la consola. Una interrupción mantenía el índice anterior, pero no quedaba una solicitud que otro proceso pudiera completar. Ahora distinguimos:

- **Job:** la solicitud durable de reconstrucción, con solicitante, workspace, revisión admitida, configuración, deadline e intentos.
- **Candidato de índice:** la colección y chunks de un intento concreto; no equivale al job.
- **Índice activo:** la versión que las consultas pueden usar.

Un job puede tener varios intentos y cada intento crea un candidato distinto. Nunca se mezclan escrituras parciales de dos consumidores en una misma colección. Recuperar significa repetir la construcción desde el snapshot lógico admitido, no continuar en mitad de un lote parcialmente confirmado.

`knowledge_schema_version` pasa de 1 a 2 mediante `harness/migrations/knowledge_002.sql`; conserva los documentos/chunks/índice de M5a. No modifica los schemas ni migraciones del core, propuestas, ledger o identidad. `harness/knowledge_queue.py` implementa admisión/claims; `harness/knowledge_worker.py` es el proceso consumidor.

### 2. Admitir la solicitud con una clave repetible

Chroma y PostgreSQL deben estar disponibles; Chroma se levanta con el comando de la sección anterior. Emití una sesión con el comando existente:

```bash
uv run python scripts/users.py issue --actor reader-demo --workspace workspace-alpha
SESSION_ID=PEGA_AQUI_EL_SESSION_ID
```

Si todavía no hay documentos activos, cargá el ejemplo:

```bash
uv run python scripts/user_knowledge.py --session "$SESSION_ID" upload examples/knowledge/atlas-release.md
```

Generá una clave una sola vez y conservála para repetir esa solicitud:

```bash
REQUEST_KEY=$(uv run python -c 'from uuid import uuid4; print(uuid4())')
uv run python scripts/user_knowledge.py --session "$SESSION_ID" enqueue --key "$REQUEST_KEY"
```

El resultado es `PENDING`, con `job_id`, revisión, configuración y `deadline_at`. Repetir `enqueue` con esa misma clave y actor/workspace devuelve el mismo job: no renueva el plazo ni reinicia intentos, aunque esté terminado. Esta operación tiene una única forma fija de reconstrucción; un cambio de inventario exige una clave nueva. `upload` continúa creando documentos nuevos sin clave idempotente.

La admisión valida sesión y rol `operator`, y guarda la revisión del inventario bajo bloqueo. Hay como máximo 16 jobs PENDING/RUNNING por workspace. No se almacena el bearer en el job. Los lectores de estado quedan limitados al solicitante y su workspace.

### 3. Ejecutar un worker independiente

En otra terminal, desde la raíz del proyecto:

```bash
uv run python -m harness.knowledge_worker --actor reader-demo --workspace workspace-alpha --once
```

`--once` procesa a lo sumo un job; sin esa opción el proceso revisa la cola cada segundo. Actor/workspace configuran la partición de un proceso local de confianza, no son una identidad aceptada desde una petición pública. El worker obtiene los permisos actuales del directorio con `resolve`; no necesita copiar ni conservar la sesión del cliente. Si la sesión expira o se revoca después de admitir un trabajo, eso no cancela el trabajo por sí solo; retirar el rol `operator` o deshabilitar al usuario sí impide trabajar/activar. Esta es la misma separación entre sesión y autoridad durable que usamos en los workers anteriores.

Cada claim incrementa `attempts` y `fencing_token`. `FOR UPDATE SKIP LOCKED` permite consumidores simultáneos sin entregar un job vivo a ambos. El lease fijo dura como máximo 90 segundos, sin renovación en este primer worker de ingestión; el trabajo tiene un deadline persistido de cinco minutos desde la admisión y máximo tres intentos. El lease nunca supera el deadline. Los lotes pequeños y el presupuesto cooperativo de M5a mantienen acotado el recorrido local esperado; si un intento tarda demasiado, pierde autoridad para activar.

Antes de cada petición HTTP a Chroma se comprueban permisos actuales, dueño/fencing del claim, lease, deadline, configuración y revisión del inventario. Una petición ya enviada puede terminar después de expirar el claim: sus escrituras solo afectan a su colección candidata. No puede cambiar el índice activo.

### 4. Activación y DONE en la misma transacción

Reutilizamos `Knowledge.rebuild()` con un guard de cola; no copiamos el algoritmo de indexación al worker. El candidato y su vinculación al job se guardan juntos al comenzar. Después de escribir/verificar los vectores, la transacción final:

1. Bloquea y valida el job, su dueño/fencing, lease y deadline.
2. Bloquea el inventario y exige la revisión admitida; una carga/retirada invalida ese trabajo.
3. Marca el candidato READY y actualiza el puntero activo.
4. Marca el job DONE y elimina su lease.

Los pasos finales se confirman o revierten juntos. El guard comprueba también que se completa el candidato vinculado al claim. No hay un estado durable «índice activado, pero job todavía en ejecución» causado por confirmar estas escrituras por separado. Si se pierde el resultado de un commit, se consulta el estado persistido; la aplicación no supone que la falta de respuesta equivale a ausencia de commit.

### 5. Qué pasa si se interrumpe

```text
PENDING → RUNNING (intento 1, candidato A)
                ↳ proceso interrumpido: lease vence
          RUNNING (intento 2, candidato B) → DONE
```

Tras una muerte real, otro consumidor reclama el mismo job cuando vence el lease. Marca FAILED el candidato BUILDING anterior, incrementa fencing y construye otro. Un proceso anterior que vuelva a ejecutarse falla el guard y no puede activar ni cambiar el estado del nuevo dueño. Se conservan el request key, el deadline y los intentos acumulados.

Un fallo de transporte `CHROMA_UNAVAILABLE` o de presupuesto cooperativo `INDEX_DEADLINE` devuelve el job a PENDING mientras queden intentos y plazo. No hay reintento HTTP oculto; el siguiente claim es otro intento contabilizado. Otros errores, como falta de permisos, inventario obsoleto o configuración incompatible, lo terminan FAILED. Un claim perdido no permite siquiera actualizar bookkeeping con autoridad obsoleta. Al agotar intentos o plazo no hay más claims.

Una retirada cambia la revisión y sigue filtrándose en las consultas del índice anterior. El worker nunca la revierte. Si aparece `STALE_INVENTORY`, revisá el inventario y admití una nueva solicitud con otra clave; no ampliamos silenciosamente el alcance del trabajo anterior.

El deadline es una condición durable para iniciar I/O y activar, no una promesa de matar un proceso o cancelar una petición remota exactamente en ese instante. Las colecciones candidatas que no se activen siguen siendo reemplazables; no asumimos rollback distribuido. La limpieza/retención automática sigue pendiente.

### 6. Consultar y buscar

Copiá el `job_id` de `enqueue`:

```bash
uv run python scripts/user_knowledge.py --session "$SESSION_ID" job JOB_ID
uv run python scripts/user_knowledge.py --session "$SESSION_ID" search "responsable release Atlas"
uv run python scripts/users.py revoke "$SESSION_ID"
```

Esperado: job DONE, un `index_id` activo y el fragmento de Marta con su cita. La información de estado no incluye bearer, dueño del lease ni fencing internos. Si no hay jobs, `--once` termina sin inventar trabajo. El comando `rebuild` de M5a sigue disponible para el ejercicio síncrono, pero la ruta nueva es `enqueue` + worker.

La cola se crea sola a partir de migraciones: no hay que versionar bases, sesiones ni colecciones de prueba. Los servicios anteriores pueden seguir funcionando durante este recorrido; no se reiniciaron ni se cambió su configuración.

### 7. Pruebas y siguientes límites

```bash
uv run python -m unittest integration_tests.test_knowledge_worker -v
HARNESS_CHROMA_TESTS=1 uv run python -m unittest integration_tests.test_chroma_knowledge -v
```

Las pruebas cubren clave repetida, cola llena, aislamiento por actor/workspace, cambios de inventario, retirada durante I/O, revocación de permisos, deadline, intentos agotados, fencing y rollback al fallar la finalización. La prueba optativa de Chroma inicia un proceso real, lo termina después de escribir vectores y espera el lease antes de recuperar en otro consumidor; conserva el índice anterior hasta la activación final.

Esto no completa M5: siguen pendientes API/UI de documentos/jobs, estados detallados de extracción/embedding, cancelación, limpieza/retención, métricas/trazas de ingestión, embeddings semánticos y la tool RAG conectada al core con evaluaciones. Un objetivo razonable después de esa línea base será comparar búsqueda híbrida y reranking con un dataset: elegiremos la técnica por evidencia, sin convertir cada variante de RAG en un requisito del laboratorio.

Validación M5b: **239 pruebas locales y 174 integraciones PostgreSQL/Chroma aprobadas**. Incluye 13 pruebas de cola/worker nuevas y una nueva prueba con proceso real que termina tras escribir vectores, espera el lease y recupera con otro candidato. El recorrido real por consola confirmó enqueue idempotente → worker independiente → job DONE → búsqueda con la versión activa. Migración de conocimiento v2 aplicada sin perder el ejemplo de M5a; quedó un job de demostración DONE, sin jobs de ingestión pendientes ni schemas RAG temporales. Se revocó la sesión del ejercicio. No se modificaron core, ledger ni configuración de los servicios anteriores y no se reiniciaron esos servicios. No se repitieron las suites de rotación/contenedores ni los tres runners de evals independientes; la regresión de integración sí incluye sus pruebas existentes. Gitleaks no encontró secretos en los 21 archivos publicables nuevos/modificados del conjunto M5a–M5b, y los enlaces Markdown locales comprobados existen. Sin commit ni publicación en esta fase.
