# M5c API y frontend de documentos e ingestión

[Índice](../guide.md) · [← Anterior](46-m5b-tipo-de-rag-e-ingestion-durable.md) · [Siguiente →](48-m5d-rag-como-herramienta-del-core.md)


### 1. Un frontend sobre la frontera autenticada existente

Abrí [Conocimiento](http://127.0.0.1:8005/knowledge). Esta página pertenece a la API de usuarios, separada de la consola de fixtures de 8002/8003. La pantalla pública no contiene documentos; cada petición de datos exige la credencial y permisos actuales. No cambia la identidad de la consola anterior ni conecta todavía OIDC.

Si la API no está ejecutándose, desde la raíz del proyecto:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
```

Chroma se opera por separado con `uv run python scripts/knowledge.py up`. La API puede admitir cargas y trabajos sin conectarse a Chroma; el worker es quien lo necesita para indexar. No se introduce ingestión en una tarea en memoria de FastAPI: cerrar/reiniciar API o navegador no elimina los jobs.

### 2. Entrar con una credencial local

Emití una sesión usando el mecanismo existente:

```bash
uv run python scripts/users.py issue --actor reader-demo --workspace workspace-alpha
```

El comando informa el `session_id` y el archivo privado bajo `data/runtime/user-sessions/`. En **Archivo de credencial (.json)** seleccioná ese archivo. El navegador lee localmente el JSON y envía el bearer únicamente en Authorization a la misma API. No sube el archivo como documento ni lo envía a Chroma.

La credencial se mantiene en memoria de esta página: no usamos localStorage, sessionStorage, cookies de sesión ni parámetros de URL. Recargar exige seleccionarla otra vez. **Salir de esta pantalla** limpia credencial, archivos seleccionados y contenido visible; no revoca el token del servidor. Para revocarlo:

```bash
uv run python scripts/users.py revoke SESSION_ID
```

Una respuesta 401 limpia la sesión y pide una vigente. La UI deshabilita carga/indexación para lectores; la autorización efectiva siempre ocurre en el backend y se revalida, aunque se manipule un botón. Un usuario sin roles reader/operator no puede consultar conocimiento. No se entrega el bearer al worker: conserva la resolución de permisos actuales por actor/workspace de M5b.

### 3. Cargar primero, indexar después

En **Agregar un documento** seleccioná un `.md` o `.txt` UTF-8 de hasta 5 MiB. Podés usar `examples/knowledge/atlas-release.md`. La primera UI crea documentos privados para el dueño; no añade un editor de ACL. El backend conserva el contrato de lectores explícitos para clientes autorizados.

Separar **Cargar documento** de **Poner indexación en cola** permite cargar varios archivos y después admitir una sola revisión del inventario. Si la carga iniciara una indexación por archivo, cargar el siguiente podría invalidar inmediatamente el trabajo anterior. Una carga aún no significa que el documento esté en el índice activo: la lista muestra **Por indexar**, **En el índice activo** o **Retirado**.

La carga HTTP añade idempotencia que el primer CLI no tenía: la UI genera un UUID para ese intento lógico y conserva la misma clave/payload al repetir tras un error mientras la página siga abierta. El servidor liga la clave al actor/workspace y al hash de nombre, texto y lectores normalizados. Repetir el mismo contenido devuelve el mismo ID sin incrementar la revisión; reutilizar la clave con otro contenido produce CONFLICT. Si el documento fue retirado, repetir la carga original devuelve su estado RETIRED y no lo reactiva.

La clave de carga en memoria no sobrevive a recargar la página. Si se perdió una respuesta y recargaste, consultá la lista antes de cargar otra vez; no interpretamos la falta de respuesta como ausencia de commit. Elegir otra vez un archivo expresa una carga nueva, incluso si tiene el mismo nombre. No hay deduplicación global por nombre que impida conservar dos documentos distintos.

`knowledge_schema_version=3` añade `knowledge_uploads` y la fecha de carga; conserva documentos, índices y jobs previos. Los documentos previos reciben la fecha de migración como referencia para ordenar la lista. Las migraciones del core/ledger no cambian.

### 4. API pequeña y limitada

`harness/knowledge_api.py` instala rutas solamente cuando `--user-auth` está habilitado:

| Método y ruta | Función |
| --- | --- |
| GET `/api/knowledge/documents` | Últimos 100 documentos visibles, estado e indicador de indexación; no entrega el texto original. |
| POST `/api/knowledge/documents` | JSON con `request_key`, `filename`, `text` y opcionalmente `readers`; carga idempotente. |
| POST `/api/knowledge/documents/{id}/retire` | Retirada lógica del documento propio; cuerpo `{}`. |
| GET `/api/knowledge/jobs` | Últimos 50 trabajos del solicitante/workspace, intentos, revisión, plazo y señales de lease/deadline. |
| POST `/api/knowledge/jobs` | Admitir una reconstrucción con `request_key`; devuelve 202 y el trabajo persistido. |
| GET `/api/knowledge/jobs/{id}` | Consultar un trabajo propio por ID. |
| POST `/api/knowledge/search` | Recuperar fragmentos autorizados con cita; cuerpo `query` y opcionalmente `limit` de 1 a 5. |

Las rutas reutilizan `Knowledge` y `KnowledgeQueue`; no reimplementan permisos, retirada ni activación. La capa HTTP no acepta actor/workspace/roles como autoridad en el cuerpo. La pertenencia viene de la sesión verificada y el directorio actual. UUID/contratos y campos extra se validan, y los errores no devuelven texto subido ni tokens.

El upload lee el stream con máximo de 6 MiB antes de parsear JSON, sin confiar en Content-Length; después exige el máximo documental de 5 MiB UTF-8. El margen es para el envoltorio/escapes JSON; un documento cuyo JSON escapeado exceda 6 MiB también se rechaza, aunque el archivo original quepa en 5 MiB. El máximo documental no es una promesa de admitir toda codificación JSON posible. Se mantienen 20 documentos activos y 1000 chunks por workspace.

Se conserva la frontera de navegador existente: cabecera `X-Harness-Client`, comprobación de origen, rechazo de cross-site, TrustedHost, CSP y no-store. Errores de formato devuelven 422, exceso de cuerpo 413, límites de inventario/cola 429, falta de permisos 403 y recurso ajeno 404; sesión inválida 401. PostgreSQL/Chroma no disponibles se traducen a errores sanitizados 503. El origen se fija a la API local; la UI no sigue redirects ni usa un destino proporcionado por documentos.

### 5. Observar un worker independiente

Después de cargar los archivos, pulsá **Poner indexación en cola**. Verás un trabajo **Pendiente** con cero intentos. En otra terminal:

```bash
uv run python -m harness.knowledge_worker --actor reader-demo --workspace workspace-alpha
```

Sin `--once` sigue consumiendo hasta detenerlo. El actor/workspace del proceso deben coincidir con quien admitió el job; no es una credencial que venga del navegador. La pantalla consulta listas cada tres segundos mientras está visible, sin solapar sondeos. **Actualizar** permite consultar manualmente. La lista sale de PostgreSQL y reaparece al entrar otra vez, no de un estado guardado en JavaScript.

| Lo que muestra la UI | Significado |
| --- | --- |
| Pendiente | Job admitido; todavía no lo reclamó un worker o quedó preparado para otro intento. |
| En proceso | RUNNING con lease activo. |
| Esperando recuperación | RUNNING cuyo lease venció; otro consumidor podrá reclamarlo si conserva permisos/plazo/intentos. |
| Plazo agotado · pendiente de cierre | El deadline ya pasó; falta que el consumidor registre el estado terminal. No afirmamos que esté trabajando. |
| Lista | DONE: activación del índice y finalización del job confirmadas juntas. |
| Fallida | FAILED: muestra el código del último error; la versión anterior puede seguir activa. |

La UI no renueva leases, reinicia intentos, amplía deadlines ni activa candidatos. Conservamos el máximo de tres intentos, deadline de cinco minutos, fencing, guard antes de I/O, revisión fija e índice anterior ante fallo. La recuperación real de M5b sigue pasando; nuevas pruebas atraviesan HTTP durante RUNNING, lease vencido y DONE del mismo job.

Un cambio de inventario mientras el job está admitido produce STALE_INVENTORY. Revisá documentos y admití otra reconstrucción; no añadimos un botón que silenciosamente reinicie el mismo trabajo con alcance distinto. Retirar cambia la revisión y excluye el documento de búsquedas inmediatamente.

### 6. Comprobar el resultado con una cita

Cuando el trabajo muestre **Lista**, buscá `responsable release Atlas`. Se verá el fragmento de Marta y una cita que incluye la versión de índice, documento y chunk. El indicador documental cambia a **En el índice activo** únicamente si ese documento aparece en los chunks del puntero activo.

La búsqueda conserva la doble comprobación de ACL y estado documental en PostgreSQL. El frontend usa `textContent` y nodos DOM para nombres, fragmentos y códigos: no interpreta Markdown/HTML del documento ni ejecuta sus instrucciones. Todavía no genera una respuesta con un LLM; los embeddings siguen siendo léxicos de demostración.

### 7. Dividir la guía sin perder el recorrido

`docs/guide.md` tenía más de 5500 líneas. Ahora es el índice del laboratorio y cada paso vive en `docs/guide/NN-tema.md`, dentro del mismo proyecto. Conservamos el contenido histórico y el orden; cada capítulo incluye anterior/siguiente y vuelta al índice. Los enlaces relativos a imágenes y otros documentos se ajustan a la nueva ubicación.

Los encabezados del índice mantienen las anclas de las secciones anteriores. Un enlace viejo a `guide.md#...` llega a la entrada correspondiente y permite abrir su capítulo. El índice destaca el paso más reciente; el estado global se mantiene en `docs/status.md`. No se crean copias de apuntes en otra carpeta de Documents.

### 8. Límites que siguen pendientes

Este incremento añade una UI local autenticada por archivo de credencial, sin OIDC ni login empresarial. Sigue pendiente añadir estados detallados por etapa, cancelación de ingestión, limpieza/retención de candidatos, observabilidad dedicada y la tool RAG en el core con embeddings semánticos/evals. El diseño objetivo no se marca completo por tener una pantalla.

Para repetir las pruebas de frontera:

```bash
uv run python -m unittest integration_tests.test_knowledge_api -v
HARNESS_CHROMA_TESTS=1 uv run python -m unittest integration_tests.test_chroma_knowledge -v
```

Validación M5c: **239 pruebas locales y 181 integraciones PostgreSQL/Chroma aprobadas**, incluidas siete nuevas de frontera HTTP: carga/idempotencia, permisos actuales, aislamiento, límites/CSRF y consulta del mismo job durante recuperación. En navegador real se verificó credencial local → carga Markdown → admisión PENDING → worker independiente → DONE → fragmentos de Marta con cita → salida que limpia la pantalla. La sesión de prueba fue revocada. API 8005 actualizada y consumidor de ingestión del usuario demo ejecutándose; no se reiniciaron servicios de fixtures ni workers previos. La migración v3 conservó los datos anteriores. Se verificó la conservación del contenido al dividir los 47 capítulos y los enlaces locales de los documentos; Gitleaks no encontró secretos en los 75 archivos publicables nuevos/modificados del conjunto M5. Runtime, credenciales y captura de verificación permanecen fuera de Git. No se repitieron las suites de rotación/contenedores ni los tres runners independientes de evals; la suite de integración incluye sus pruebas existentes. Sin commit ni publicación.
