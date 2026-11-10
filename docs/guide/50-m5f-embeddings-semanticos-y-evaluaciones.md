# M5f Embeddings semánticos y evaluaciones de recuperación

[Índice](../guide.md) · [← Anterior](49-m5e-consola-autenticada-unificada.md) · [Siguiente →](51-m5g-preguntas-sin-respuesta.md)

## 1. Objetivo y elección del proveedor

La búsqueda léxica de los pasos anteriores agrupaba palabras por hashing. Ahora incorporamos un modelo de embeddings aprendido, para recuperar fragmentos cuando la consulta usa paráfrasis o incluso otro idioma.

Encontramos Ollama local con `qwen3-embedding:4b` ya descargado y reutilizamos ese modelo. No se añadieron dependencias Python, descargas de pesos ni llamadas pagadas. Ollama atiende la inferencia en `127.0.0.1:11434`; los documentos y consultas se envían a ese servicio local. El modelo de generación del agente sigue guionado: embeddings reales y generación de respuestas son componentes distintos.

La ficha de [Qwen3-Embedding-4B](https://huggingface.co/Qwen/Qwen3-Embedding-4B) documenta el modelo multilingüe, dimensión de hasta 2560 y consultas con instrucciones de tarea. Usamos 2560 dimensiones y una instrucción fija en inglés; los documentos se envían sin ese prefijo. La [API de embeddings de Ollama](https://docs.ollama.com/api/embed) permite entradas por lotes y rechazar truncamiento. No estamos incorporando Hybrid, HyDE ni GraphRAG: seguimos con recuperación vectorial textual simple dentro del harness.

El modelo de 4B consume memoria y tiene costo de arranque. En este entorno ya estaba disponible; usar otro modelo, incluido uno menor, será otra configuración y exigirá construir/evaluar otro índice.

## 2. Dos espacios vectoriales, sin mezclar dimensiones

`harness/embeddings.py` registra dos perfiles autorizados:

| Perfil | Modelo/algoritmo | Dimensión | Uso |
| --- | --- | --- | --- |
| `lexical` | `lexical-sha256-v1` | 256 | Baseline determinista anterior. |
| `semantic` | Ollama `qwen3-embedding:4b` con manifest fijado | 2560 | Recuperación semántica local. |

El perfil semántico persiste esta configuración completa en cada candidato/job:

```text
ollama:qwen3-embedding:4b@sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907:2560:cosine:instruct-v1:chunk800-overlap100
```

Incluye modelo, digest del manifest de Ollama, dimensión, distancia, receta de consulta y chunking. La etiqueta mutable del modelo no basta para identificar el espacio. El proveedor compara el digest publicado por el servicio antes y después de cada inferencia y rechaza un cambio con EMBEDDING_MODEL_CHANGED. Esta verificación presupone un servicio local confiable; no es una atestación criptográfica de su ejecución.

`provider_for()` admite únicamente esos registros. Un usuario puede elegir un perfil conocido; no puede aportar modelo, digest, dimensión, URL o instrucciones de embedding desde una petición. Una configuración desconocida falla con INCOMPATIBLE_INDEX.

Cada construcción conserva su colección nueva. Los índices léxico y semántico pueden coexistir en Chroma con diferentes dimensiones. No se sobrescribe la colección de 256 dimensiones ni se reinterpretan sus vectores como si fueran de 2560.

## 3. La configuración se congela en el job

`KnowledgeQueue.enqueue()` ahora admite una configuración registrada. El job conserva esa elección desde la admisión. Repetir una request key con otro perfil da CONFLICT; no cambia en silencio la intención original. Los clientes anteriores que omiten el nuevo campo siguen admitiendo jobs léxicos.

El claim incluye la configuración persistida. Aunque el worker se haya construido con la configuración léxica por defecto, crea el motor del intento usando la del claim. El guard exige que siga coincidiendo y conserva revisión de inventario, rol actual, owner/fencing, lease, deadline y máximo de tres intentos.

La construcción mantiene el contrato anterior: BUILDING → verificación → activación + DONE en una transacción. Si falla el servicio, cambia el digest o aparece una salida inválida, el candidato no reemplaza el índice anterior. EMBEDDING_UNAVAILABLE es reintentable dentro de los intentos/plazo existentes; cambios de modelo o vectores inválidos requieren corregir la causa y una nueva solicitud.

Los jobs son intenciones independientes; el índice activo corresponde a la última activación válida. El selector no establece una precedencia global entre trabajos concurrentes de perfiles distintos.

No se añadieron migraciones: los campos configuration existentes ya eran texto y podían guardar el nuevo identificador. Los jobs/índices anteriores conservan exactamente su configuración.

## 4. Inferencia local acotada

El proveedor usa el endpoint fijo `/api/embed`, sin proxies, redirects ni bearer de usuario. Antes de inferir comprueba que el modelo local esté instalado; no dispara pulls ni elige otro modelo como fallback. Cada respuesta debe tener la cantidad esperada de vectores, 2560 componentes finitos y norma cercana a uno. Se rechazan dimensión equivocada, NaN/infinito, vector cero o modelo de respuesta distinto.

Las consultas llevan el prefijo versionado:

```text
Instruct: Given a question, retrieve relevant passages that answer the question
Query: <consulta>
```

Los documentos conservan su texto original. `truncate: false` evita perder contexto silenciosamente si un input excede el límite del modelo. En este laboratorio seguimos fragmentando por 800 caracteres, con 100 de solapamiento; todavía no sustituimos ese contrato por chunking basado en tokens.

Los lotes semánticos son de ocho fragmentos. Antes de cada llamada de inferencia de ingestión se comprueba la autoridad del job. El presupuesto cooperativo de indexación se vuelve a comprobar después de calcular embeddings y antes de escribirlos a Chroma. Un lote que termina fuera de plazo no publica sus vectores; una inferencia ya iniciada puede terminar después del corte.

HTTP tiene timeout de ocho segundos por fase. La consulta semántica usa transporte asíncrono y se incluye dentro del timeout/reserva de diez segundos de la herramienta RAG. Cancelarla impide continuar con la consulta vectorial o guardar un resultado tardío. El servidor de inferencia puede terminar trabajo ya recibido; el harness no puede deshacer ese cómputo remoto.

Ollama conserva el modelo caliente hasta diez minutos según `keep_alive`. Las llamadas a este modelo de embeddings forman parte de la tool RAG, sin sumarse como nuevas decisiones del modelo guionado. Las cinco decisiones y cuatro herramientas del perfil CI+RAG se mantienen.

## 5. Consultar usa la versión activa

La preparación de búsqueda obtiene identidad, índice activo y documentos permitidos en PostgreSQL. El vector de consulta se calcula con el proveedor de **esa configuración de índice**, no con el default del proceso ni con el valor actual del selector de la pantalla.

Si no hay documentos elegibles se devuelve una lista vacía sin enviar la consulta al modelo. Cuando existen candidatos, se conserva la doble comprobación de identidad, puntero, estado documental, ACL, chunk y hash antes de devolver texto. No se convierte un error del modelo en «sin resultados» ni se consulta una colección semántica con un vector léxico.

Los resultados históricos de un run siguen sujetos a la política de M5d. Una recuperación puede reutilizar su cita antigua si el documento sigue permitido, aunque se haya activado otro índice; no vuelve a inferir embeddings para un resultado ya confirmado. Retirada o permisos retirados bloquean su entrega. Los límites del core y el ledger de escrituras no cambian.

## 6. Preparar y usar el modelo

En este equipo Ollama y el modelo ya estaban disponibles. Para repetir el laboratorio en otro equipo, instalá Ollama desde su fuente oficial, iniciá su servicio local y prepará el modelo:

```bash
ollama pull qwen3-embedding:4b
uv run python scripts/embeddings.py warmup
```

`warmup` comprueba el digest y realiza una inferencia pequeña; informa dimensión y tiempo, sin imprimir vectores ni leer documentos. El comando no descarga ni modifica modelos. Si el tag descargado en el futuro tiene otro digest, requiere revisar/versionar el proveedor y reindexar; el código no acepta automáticamente ese cambio.

En [Conocimiento](http://127.0.0.1:8005/console#knowledge), con una sesión de operador:

1. Cargá los documentos si todavía no existen.
2. Elegí **Semántico · Qwen3 local (Ollama)**.
3. Pulsá **Poner indexación en cola** y observá su job hasta Lista/DONE.
4. Consultá **Configuración** para ver la configuración e ID realmente activos.
5. Probá una pregunta como «¿Quién coordina la entrega de la nueva versión de Atlas?» y revisá el fragmento de Marta con su cita.

El selector determina la configuración del próximo job; no cambia la búsqueda actual hasta que el candidato se active. Cada job muestra qué familia usó. La pantalla ofrece semántico por defecto; el API/CLI previo conserva lexical como default de compatibilidad. Elegir lexical permite construir una nueva versión baseline con el mismo mecanismo; no borra historia.

Por consola, con una sesión vigente:

```bash
REQUEST_KEY=$(uv run python -c 'from uuid import uuid4; print(uuid4())')
uv run python scripts/user_knowledge.py --session SESSION_ID enqueue --key "$REQUEST_KEY" --embeddings semantic
uv run python scripts/user_knowledge.py --session SESSION_ID search "¿Quién coordina la entrega de Atlas?"
```

La ruta POST `/api/knowledge/jobs` añade `embedding_profile: "semantic" | "lexical"`. El worker de ingestión existente procesa ambas opciones. API, worker de ingestión y worker de runs deben ejecutar el código actualizado antes de activar el nuevo espacio; siguen siendo procesos separados del host. Ollama es un servicio local del host en este incremento, no un contenedor nuevo ni un endpoint apto para usar `127.0.0.1` desde cualquier contenedor.

## 7. Una evaluación con respuestas esperadas, no una impresión visual

`evals/retrieval_cases.json` versiona ocho documentos ficticios y doce consultas. Hay coincidencias exactas, paráfrasis en español y dos preguntas en inglés sobre documentos españoles. Cada pregunta define sus documentos relevantes esperados. Los umbrales se guardan con el dataset: top-1 semántico al menos 0,75 y recall@3 al menos 0,90.

El runner `scripts/evaluate_retrieval.py` crea un schema PostgreSQL temporal, carga ese corpus privado para el actor fixture y construye dos índices reales en Chroma. Consulta el baseline antes de activar el semántico; ambos usan el mismo texto, chunking, límites y preguntas. Comprueba texto/nombre/cita exactos, otro workspace, otro lector sin ACL y retirada inmediata. Elimina solo su schema/colecciones y falla si la limpieza no se puede confirmar. No usa el historial ni documentos reales de la app.

```bash
uv run python scripts/evaluate_retrieval.py
```

El reporte ignorado queda en `data/runtime/evals/retrieval-latest.json`, con hash/versionado del dataset, configuración, resultados por caso, ranking y latencia. No almacena embeddings ni credenciales. La salida termina con error si falla un umbral o alguna comprobación de integridad/alcance/limpieza.

| Medida | Qué responde |
| --- | --- |
| Top-1 | ¿El primer documento recuperado era relevante según el label? |
| Recall@3 | ¿Qué fracción de los documentos esperados apareció entre los tres primeros? |
| MRR@3 | ¿Qué tan arriba apareció el primer documento relevante? |
| Latencia p50/p95 | ¿Cuánto tardó el recorrido de consulta, incluida inferencia, Chroma y comprobaciones PostgreSQL? |
| Integridad/aislamiento | ¿Las citas correspondían al texto exacto y se respetaron acceso/retirada? |

Resultado de la ejecución local del **10 de noviembre de 2026**, con el modelo caliente:

| Perfil | Top-1 | Recall@3 | MRR@3 | p50 | p95 |
| --- | --- | --- | --- | --- | --- |
| Léxico | 50,0 % | 83,33 % | 0,6389 | 33,45 ms | 38,25 ms |
| Semántico | 100 % | 100 % | 1,0 | 145,34 ms | 172,53 ms |

La construcción de ese corpus pequeño tardó unos 93 ms en baseline y 603 ms en semántico. La comprobación inicial del modelo había tardado unos 2,46 s; caliente, una consulta directa rondó 0,15 s. p50 usa la mediana y p95 el rango más próximo; con doce consultas p95 corresponde al mayor tiempo observado. La tabla no mide el arranque en frío ni una carga concurrente.

Estos son doce casos sintéticos, no una estimación de rendimiento empresarial ni una garantía de respuestas correctas. Los fragmentos válidamente citados pueden contener afirmaciones falsas. Tampoco evaluamos generación/factualidad, porque el agente sigue guionado. El retriever siempre puede devolver vecinos aunque una pregunta no tenga respuesta: no se añadió un umbral de abstención ni se usa distancia coseno como probabilidad. Se necesitará ampliar el dataset con preguntas sin respuesta y calibrar ese comportamiento.

## 8. Pruebas y próximo incremento

```bash
uv run python -m unittest tests.test_embeddings integration_tests.test_semantic_knowledge -v
```

La regresión habitual no necesita inferencia Ollama: valida contrato/pin, vectores malformados, cancelación, grado de relevancia, perfil congelado en job, conflicto de clave y conservación del índice anterior ante cambio de modelo. La evaluación anterior y la verificación de la consola sí usan el modelo real y Chroma. Una prueba adicional evita escribir en Chroma si el presupuesto expiró durante el embedding.

Quedan pendientes ampliar los casos reales/no respondibles, evaluar la respuesta generada con un LLM real y completar cancelación/retención/observabilidad de ingestión. Hybrid o reranking se incorporarán si la evaluación ampliada muestra un problema que resuelvan. El siguiente paso razonable es hacer visible el reporte de calidad en la consola y ampliar preguntas sin respuesta, para enseñar cuándo recuperar no basta para responder.

Validación M5f: **249 pruebas de Python, 203 integraciones PostgreSQL/Chroma y 2 pruebas JavaScript aprobadas**. Tras ajustar lotes/plazo se repitieron doce pruebas focalizadas, incluidas inferencia simulada, selección persistida de proveedor y las dos integraciones Chroma reales. La evaluación real con ocho documentos/doce consultas pasó los umbrales versionados: top-1/recall@3 de 50 %/83,33 % léxico y 100 %/100 % semántico; p50 final 33,45/145,34 ms. Citas exactas, cero fugas en las comprobaciones de alcance, retirada excluida y limpieza de schema/colecciones confirmadas. En navegador real se admitió el índice semántico, quedó DONE con un intento, se buscó la paráfrasis de Atlas con cita y se confirmó configuración activa; CI+RAG terminó COMPLETED 5/4 con citas del nuevo índice. Se conservaron los índices léxicos y se revocó la sesión del ejercicio. API/worker de runs/ingestión actualizados; servicio de escrituras y ledger sin cambios. Cero jobs de lectura/ingestión activos y cero schemas temporales de evaluación. Gitleaks no detectó secretos en los 260 archivos del árbol publicable; enlaces locales comprobados. Pesos, vectores, credenciales, reportes y captura de verificación quedan fuera de Git. No se repitieron las suites de rotación/contenedores ni los tres runners independientes de evals anteriores; sus pruebas de integración sí forman parte de la regresión. Sin commit ni publicación en este paso.
