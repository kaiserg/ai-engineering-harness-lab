# M5a Chroma como servicio e inventario de conocimiento

[Índice](../guide.md) · [← Anterior](44-cierre-autenticado-evaluaciones-y-checkpoint-antes-de-m5.md) · [Siguiente →](46-m5b-tipo-de-rag-e-ingestion-durable.md)


### Objetivo y límite de este incremento

Comenzamos M5 con un recorrido pequeño: registrar un archivo, construir un índice, buscar fragmentos y retirarlo. Primero comprobamos dónde vive la autoridad sobre los datos y qué sucede ante un fallo. Todavía no conectamos estos fragmentos al modelo ni a las herramientas DevOps.

El recorrido es una **consola local de operación**, con acceso PostgreSQL del desarrollador y una sesión de usuario validada. No es una API pública ni una barrera contra quien controla el equipo y sus credenciales de base de datos. La API autenticada 8005, su worker y el frontend existente siguen con su comportamiento anterior. La siguiente integración podrá reutilizar este módulo detrás de esa frontera.

### 1. Chroma vive fuera del proceso Python

```bash
uv run python scripts/knowledge.py up
uv run python scripts/knowledge.py status
```

El perfil optativo `knowledge` de Compose añade `chromadb/chroma:1.5.9`, publicado solo en `127.0.0.1:8012`. El comando usa el contexto Docker ya elegido por el laboratorio y opera únicamente el servicio `chroma`. No levanta de nuevo API/workers ni modifica su configuración.

El volumen `chroma_data` se monta en `/data`. Chroma guarda allí su almacenamiento interno, incluido `chroma.sqlite3` y los segmentos vectoriales; ese SQLite interno **no sustituye PostgreSQL como inventario del harness**. El nombre real del volumen lleva el prefijo del proyecto Compose. Recrear el contenedor conserva sus datos. No se necesita copiar bases de prueba al clonar el repo: los servicios crean su almacenamiento y las migraciones crean las tablas.

Para detenerlo conservando los datos:

```bash
uv run python scripts/knowledge.py stop
```

Chroma no tiene autenticación/TLS en este primer entorno local: no publicar este puerto en una interfaz externa. El bearer del usuario nunca se envía a Chroma. Los vectores también son datos derivados del contenido y requieren protección; omitir el texto original no convierte el almacén en información pública.

Referencias del modelo cliente/servidor: [documentación oficial de Chroma](https://docs.trychroma.com/docs/run-chroma/client-server) y [clientes](https://docs.trychroma.com/docs/run-chroma/clients). El contrato REST v2 se verificó contra el OpenAPI del servicio fijado a 1.5.9. `harness/chroma_vectors.py` usa el transporte HTTP existente con timeout de cinco segundos, sin proxies, redirects ni reintentos implícitos. Así evitamos incorporar un SDK con descargas de modelos o conexiones sin un límite explícito.

### 2. Dos almacenes, responsabilidades distintas

| PostgreSQL, schema `harness_users_lab` | Chroma |
| --- | --- |
| Documento original, nombre, hash, dueño, workspace, lectores y estado. | Vectores, IDs de chunks y metadato `document_id`. |
| Fragmentos exactos, configuración/versiones y puntero al índice activo. | Una colección nueva por candidato de índice. |
| Decide qué versión y qué contenido pueden entregarse. | Propone IDs cercanos a la consulta. |

`harness/knowledge.py` crea una migración independiente `knowledge_schema_version=1`. No cambia las versiones de runs, usuarios, propuestas o ledger. Los documentos y fragmentos históricos se conservan para poder identificar lo citado.

Un documento recién registrado está `ACTIVE` en el inventario, pero no es buscable hasta que una construcción lo incluya en el índice activo. `ACTIVE` no significa «ya indexado». Retirarlo cambia su estado a `RETIRED` y lo excluye de consultas inmediatamente, sin esperar un borrado físico en Chroma.

### 3. Embeddings pequeños y reproducibles

En esta fase usamos `lexical-sha256-v1`, de 256 dimensiones. La función `embed()` normaliza mayúsculas y acentos, divide en palabras y acumula cada palabra en una posición determinada por SHA-256. Finalmente normaliza el vector a longitud uno; Chroma compara con distancia coseno.

**Es una demostración léxica con colisiones de hash, no un modelo de embeddings semánticos.** Palabras compartidas pueden acercar dos textos; sinónimos sin palabras comunes no tienen por qué hacerlo. Un resultado cercano no garantiza relevancia. Esto permite probar controles sin API keys, llamadas pagadas ni descarga de modelos. No estamos midiendo calidad de RAG todavía.

`split()` genera fragmentos de hasta 800 caracteres con 100 de solapamiento, para no perder todo el contexto en una frontera. Son caracteres, no tokens del modelo. La configuración completa persistida es `lexical-sha256-v1:256:cosine:chunk800-overlap100`. Cambiar familia/dimensión/chunking exige un índice nuevo; las consultas rechazan una configuración incompatible.

### 4. Registrar un Markdown con identidad actual

Desde la raíz del proyecto, emití una sesión local con el comando existente:

```bash
uv run python scripts/users.py issue --actor reader-demo --workspace workspace-alpha
```

Copiá el `session_id` que imprime, no el token. Usalo en una variable de tu terminal:

```bash
SESSION_ID=PEGA_AQUI_EL_SESSION_ID
uv run python scripts/user_knowledge.py --session "$SESSION_ID" upload examples/knowledge/atlas-release.md
uv run python scripts/user_knowledge.py --session "$SESSION_ID" list
```

La credencial sigue en un archivo privado de runtime, ignorado por Git. La consola autentica antes de inicializar conocimiento y vuelve a comprobar sesión/permisos en las fronteras sensibles. No acepta actor, workspace o roles desde el archivo. `operator` puede registrar, retirar documentos propios y reconstruir el índice del workspace; `reader` u `operator` pueden buscar lo permitido.

El dueño puede leer su documento. `--reader otro-actor` concede lectura explícita a ese actor **dentro del mismo workspace**. Este primer incremento no tiene editor de ACL; agregar uno deberá incrementar la revisión del inventario y conservar la comprobación final. No hay lectura pública por defecto. La operación de reconstrucción es una capacidad del operador sobre el inventario del workspace; no devuelve texto de otros dueños al operador.

Aceptamos solo UTF-8 `.md`/`.txt`, hasta 5 MiB por archivo; conservamos un nombre sin ruta, validado, y generamos IDs internos. No se aceptan URLs, rutas del servidor ni parsers PDF. El archivo se lee localmente con un límite de tamaño. Máximo 20 documentos activos y 1000 chunks por workspace, comprobados dentro del bloqueo de inventario. Cada `upload` crea un documento nuevo; no tiene todavía clave de idempotencia para repetir cargas automáticamente.

### 5. Construir y activar una versión

```bash
uv run python scripts/user_knowledge.py --session "$SESSION_ID" rebuild
```

`rebuild()` hace lo siguiente:

1. Bloquea el inventario brevemente y toma su revisión junto con los documentos activos.
2. Persiste un candidato `BUILDING` y sus fragmentos en PostgreSQL. Cada candidato usa una colección Chroma distinta; el índice anterior no se modifica.
3. Fuera de la transacción PostgreSQL, calcula vectores y los escribe en lotes de 64. Chroma recibe embeddings e IDs, sin el texto original. Verifica que el conjunto de IDs escrito coincida con el esperado.
4. Revalida sesión y rol. Bloquea de nuevo el inventario y compara la revisión actual con la inicial.
5. Si coinciden, marca `READY` y cambia el puntero activo en una sola transacción. Si hubo carga o retirada, marca `STALE` sin activar ese candidato.

Esto es una activación mediante **compare-and-swap**: «activar solo si la revisión sigue siendo la que leí». No intentamos fingir una transacción distribuida entre PostgreSQL y Chroma.

Un error normal deja `FAILED`; una muerte/interrupción puede dejar `BUILDING`. Ninguno es consultable. El puntero anterior permanece intacto y `rebuild` permite construir otra versión desde el inventario actual. Todavía no reanudamos el trabajo de ingestión interrumpido ni recogemos automáticamente sus colecciones huérfanas: ese será el incremento de cola/worker de ingestión. La construcción comprueba un presupuesto cooperativo de 30 segundos entre lotes; cada petición HTTP tiene timeout de cinco segundos por fase y no usa reintentos ocultos. No es todavía un deadline de proceso: una petición en curso puede terminar después del presupuesto. El worker de ingestión deberá persistir y hacer cumplir su propio deadline sin reutilizar ni reiniciar el presupuesto de un run del agente.

La retención de versiones fallidas/antiguas y sus copias de chunks no está acotada automáticamente todavía. Los límites de 20/1000 son del inventario activo, no una cuota total de disco. Esta consola de desarrollo no debe convertirse en un endpoint multiusuario sin admisión, cuotas de versiones y limpieza.

### 6. Buscar un fragmento con cita

```bash
uv run python scripts/user_knowledge.py --session "$SESSION_ID" search "responsable release Atlas"
```

Esperado: un fragmento que contiene «Marta», una cita `[index_id:document_id:ordinal]`, el nombre del documento y una distancia. La cita identifica una versión de índice y su chunk persistido; no implica que un modelo haya validado la afirmación.

`search()` lee el índice activo y los IDs de documentos permitidos. Envía a Chroma el vector de consulta y ese filtro. Después de recibir candidatos, vuelve a autenticar y comprueba el puntero activo. Para cada ID busca el texto **en PostgreSQL**, exigiendo workspace, documento todavía `ACTIVE`, dueño/ACL actuales y hash del fragmento. Un ID inventado por el almacén vectorial no concede acceso. Si cambia el índice durante la consulta, devuelve `STALE_INDEX` y permite repetir.

El resultado lleva `trust: untrusted_document`: el contenido recuperado es dato, incluso si contiene instrucciones como «ignora tus políticas». Por ahora se devuelve literalmente; no se incorpora al prompt ni habilita llamadas. La defensa frente a prompt injection con un modelo que consume RAG sigue pendiente de integración y evaluación. El límite es cinco resultados y 2000 caracteres de consulta.

### 7. Retirar sin esperar un nuevo índice

Copiá el `document_id` de `upload` o `list`:

```bash
uv run python scripts/user_knowledge.py --session "$SESSION_ID" retire DOCUMENT_ID
uv run python scripts/user_knowledge.py --session "$SESSION_ID" search "responsable release Atlas"
uv run python scripts/users.py revoke "$SESSION_ID"
```

El fragmento retirado ya no debe aparecer, aunque la colección anterior conserve sus vectores. Si había una construcción en curso, su revisión ya no coincide y no puede reactivar el documento. El comando `retire` es repetible; no borra evidencia histórica. Si retiraste todos los documentos, `rebuild` devuelve `EMPTY_INVENTORY`; las consultas del índice anterior siguen filtrando todos los documentos retirados.

### 8. Qué conservamos y qué sigue

No cambiamos core, límites del agente, perfiles MCP, aprobación, ledger, leases/fencing ni los mecanismos de recuperación de lecturas/escrituras. RAG aún no es una tool del agente: no consume su presupuesto de llamadas y tampoco puede ejecutar operaciones DevOps. Antes de conectarlo habrá que añadir un contrato de tool, autorización, timeout y contabilización dentro de ese mismo core.

El próximo incremento será ingestión durable con estados de trabajo y recuperación; después conectaremos API/UI, recuperación como herramienta, embeddings semánticos y evaluaciones de citas/relevancia/inyección. M5 no queda completo con este paso: comenzamos por el inventario y la sustitución segura de versiones.

Para repetir las pruebas nuevas:

```bash
uv run python -m unittest tests.test_knowledge integration_tests.test_knowledge -v
uv run python scripts/knowledge.py up
HARNESS_CHROMA_TESTS=1 uv run python -m unittest integration_tests.test_chroma_knowledge -v
```

Las integraciones usan schemas temporales y eliminan solo sus propias colecciones; la prueba real de Chroma es optativa en la suite general. No contienen documentos privados ni requieren las bases de prueba de quien desarrolló el laboratorio.

Validación M5a: **239 pruebas locales aprobadas**; suite completa de **158 integraciones PostgreSQL/Chroma** aprobada y comprobación final de las **11 integraciones de inventario**, incluidas dos añadidas después de iniciar la suite (160 integraciones distintas en total). Las **21 evaluaciones existentes** también pasaron: 9 lecturas, 8 escrituras y 4 autenticadas. La prueba Chroma real verificó ausencia de texto original en su campo documents. Una verificación adicional recreó solo su contenedor y conservó índice/cita; la consola real verificó carga/búsqueda, aislamiento de workspace y revocación. Quedó un documento ficticio de Atlas indexado en workspace-alpha; las sesiones del ejercicio se revocaron y los schemas/colecciones temporales se eliminaron. Compose válido; no se repitieron las suites de rotación ni de contenedores del flujo anterior. Gitleaks no encontró secretos en los 17 archivos publicables nuevos/modificados y los enlaces Markdown locales comprobados existen. No se hizo commit ni publicación.
