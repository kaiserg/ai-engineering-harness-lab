# 59. RAG agéntico mínimo en el chat documental

[Índice](../guide.md) · [← Anterior](58-m7g-jira-cloud-a-ci-desde-el-chat.md) · [Siguiente →](60-entorno-colima-aislado.md)

## 1. Una búsqueda inicial y una decisión basada en evidencia

En **Documentos · RAG agéntico**, Qwen3 local formula la primera consulta. Después de verla, elige responder, reformular o abstenerse. La primera búsqueda es obligatoria para que el modelo consulte el índice antes de concluir que falta evidencia. Este perfil se elige al crear la conversación y conserva el historial y los límites del chat persistido.

Si un fragmento ya contiene la respuesta, el modelo puede citarlo y cerrar. Si falta el dato y aparece una pista concreta, como un código o el nombre de otro documento, puede pedir una segunda búsqueda usando esa pista. Si la evidencia sigue siendo insuficiente, debe abstenerse. El modelo elige la segunda consulta. Usa razonamiento al evaluar los fragmentos y prioriza una consulta corta centrada en la pista para que los términos de la primera búsqueda no dominen el ranking. El harness conserva la autoridad para aceptarla o rechazarla.

## 2. Qué valida el harness

- **Herramienta y presupuesto.** Solo está disponible `knowledge_search`, con hasta tres fragmentos por consulta. La segunda consulta debe ser distinta de la primera. Después de dos búsquedas confirmadas, el esquema solo admite respuesta o abstención y el host rechaza otra búsqueda. Hay presupuesto para un intento adicional de herramienta si el worker se interrumpe con una lectura pendiente. El límite global sigue en 120 segundos activos.
- **Permisos y recuperación.** Cada búsqueda pasa por las reglas de actor, workspace y documento. Chroma devuelve candidatos y PostgreSQL comprueba qué fragmentos se pueden entregar. El checkpoint guarda cada resultado antes de pedir la siguiente decisión. Al retomar un turno, el worker revalida permisos y usa los resultados confirmados. Si retiraste un documento que sustenta el turno, su contenido no se vuelve a entregar al modelo ni al historial.
- **Citas.** La respuesta puede citar cualquiera de las dos búsquedas del turno. Cada cita debe existir y la cita textual debe coincidir con un fragmento autorizado. El historial sirve para resolver referencias. La comprobación de citas valida su origen textual. Determinar si una cita realmente sostiene toda la respuesta sigue siendo parte de la evaluación.

## 3. Tres casos para repetir

Con PostgreSQL, Chroma, Ollama y los modelos del [capítulo 50](50-m5f-embeddings-semanticos-y-evaluaciones.md), ejecutá:

```bash
uv run python scripts/evaluate_agentic_rag.py
uv run python -m unittest integration_tests.test_agentic_rag
```

El evaluador crea un inventario sintético y un esquema temporal. Guarda un reporte local con las consultas, los documentos recuperados y las decisiones, y elimina el esquema y el índice temporal.

| Pregunta | Resultado esperado |
| --- | --- |
| ¿Quién es responsable de la entrega de Atlas? | Responde Marta con cita después de una búsqueda. |
| ¿Cuál es el nombre exacto del canal de guardia del producto Atlas? Si solo encontrás una referencia al directorio, seguí esa referencia. | La primera búsqueda encuentra la ficha con el identificador. Tras pausar y recuperar, la segunda encuentra el canal en el directorio y lo cita. |
| ¿Cuál fue el presupuesto aprobado para Atlas en 2022? | Se abstiene. La presencia de otros documentos sobre Atlas no aporta ese dato. |

Las pruebas de integración también fuerzan una tercera búsqueda, una cita inventada, la retirada de un documento entre pausa y recuperación y el reintento de una segunda búsqueda con el mismo identificador de llamada.

## 4. Probarlo con tus documentos

Reiniciá la API y el worker si ejecutaban código anterior. Abrí la consola, entrá en **Chat**, elegí **Documentos · RAG agéntico** y creá una conversación. El indicador **Búsquedas** cuenta resultados confirmados. **Tools** cuenta intentos y puede incluir un reintento de recuperación. Para ver el checkpoint, marcá **Pausar después de la lectura** antes de enviar y después elegí **Recuperar este mensaje**.
