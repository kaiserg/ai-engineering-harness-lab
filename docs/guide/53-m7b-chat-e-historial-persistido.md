# M7b Chat e historial persistido

[Índice](../guide.md) · [← Anterior](52-m7a-llm-local-de-lectura.md) · [Siguiente →](54-m7c-jira-desde-el-chat.md)

## 1. Objetivo: conversar sobre el mismo motor

Agregamos **Chat** a la consola común. Podés preguntar quién coordina Atlas, recibir una respuesta citada y continuar con «¿y su correo?». El historial permite resolver esa referencia; la respuesta sigue requiriendo una búsqueda nueva y evidencia actual.

El chat usa Qwen3 local y el mismo harness de M7a. No añadimos otro loop de agentes ni una memoria externa del proveedor. El historial se guarda en PostgreSQL y se reconstruye para cada decisión; cerrar el navegador no lo borra. Por ahora es un chat de lectura documental, con respuesta completa al finalizar. Las herramientas DevOps elegidas libremente por el LLM, las escrituras conversacionales y streaming siguen fuera de este incremento.

## 2. Conversación, turno y run

| Objeto | Responsabilidad |
| --- | --- |
| Conversación | Propietario, workspace y fecha; agrupa hasta veinte turnos. |
| Turno | Posición ordenada, clave de reintento y vínculo a un run. |
| Run | Pregunta, contexto seleccionado, herramientas, decisiones, presupuesto, resultado y recuperación. |
| Job | Trabajo del worker con lease, fencing e intentos. |

Cada mensaje crea un run nuevo. Su presupuesto es el de M7a: hasta tres intentos de modelo, dos de herramienta, 30 segundos por decisión, 10 por herramienta y 120 segundos activos. El camino normal sigue siendo dos decisiones y una búsqueda. Un mensaje nuevo tiene su presupuesto propio; recuperar el mismo mensaje conserva los contadores y el tiempo que ya consumió. No incorporamos un presupuesto monetario ni de tiempo acumulado para toda la conversación.

La migración idempotente `harness/migrations/chat_001.sql` crea `conversations` y `conversation_turns`. Las respuestas no se duplican en otra tabla: el turno apunta al estado durable del run. Los scripts existentes y el lector de una pregunta siguen disponibles con su perfil anterior.

## 3. Un solo commit para admitir el mensaje

Cuando enviás un mensaje, bloqueamos la fila de su conversación mientras comprobamos:

1. Que pertenece al usuario y workspace autenticados.
2. Si ya existe esa clave de mensaje: devuelve el mismo run; cambiar texto o pausa con la misma clave da conflicto.
3. Que no haya un turno pendiente, activo o pausado. Esto impide que dos pestañas construyan contextos incompatibles.
4. Que no se excedan veinte turnos ni la cuota de dieciséis trabajos activos del actor.
5. Qué turnos completados forman el contexto autorizado.

Después se insertan run, job y turno dentro de la misma transacción PostgreSQL. Una caída antes del commit no deja mensajes sin trabajo ni trabajos sin turno. La admisión reutiliza el método transaccional del store, con el mismo lock y cuota de la cola anterior; no implementamos una segunda cola.

La creación de conversación también tiene una clave idempotente. Las claves son UUID elegidos por el cliente, pero no conceden acceso: se buscan dentro de la propiedad autenticada. La UI conserva la intención de envío hasta recibir éxito. Si hay error, permite reintentar el mismo mensaje con la misma clave; no promete que esa intención temporal del navegador sobreviva a una recarga antes de recibir confirmación. Los mensajes ya confirmados sí se recuperan del servidor.

## 4. Historial visible y contexto del modelo son distintos

La pantalla puede consultar los veinte turnos de una conversación. Para inferir se seleccionan como máximo los últimos tres turnos completados, con mensajes user/assistant. Los turnos fallidos y cancelados quedan visibles como actividad, pero no se convierten en respuestas del historial del modelo.

Cada run guarda en `read_request` la pregunta, el UUID de conversación y los UUID de los runs seleccionados. Ese snapshot es inmutable. Si se recupera, no se reconstruye con una selección distinta ni se incorpora un mensaje posterior. La API elige esos IDs; el usuario no puede enviar una lista arbitraria de historial, un perfil, modelo, identidad o workspace.

Se usa el nuevo perfil `ollama-qwen3-4b-document-chat-v1`. Reutiliza transporte, digest fijado, schemas, finalizador y herramientas del lector, y añade instrucciones para resolver referencias desde el historial. El historial nunca se usa como evidencia autorizada para sostener una respuesta nueva: el finalizador sigue exigiendo citas y extractos de la búsqueda del turno actual.

El perfil rechaza una solicitud de inferencia si sus mensajes serializados superan 6500 bytes UTF-8, para evitar enviar un contexto creciente sin control. Es un límite conservador por tamaño, no una medición exacta de tokens. Ante `CONTEXT_LIMIT` falla el run sin publicar respuesta; no recorta silenciosamente el historial ni resume con otra llamada al LLM. Una conversación nueva permite continuar sin ese contexto.

La ventana de tres turnos no es memoria ilimitada: referencias más antiguas pueden no resolverse. No hay embeddings de conversaciones, resumen automático ni preferencias globales del usuario.

## 5. La evidencia retirada no vuelve por la memoria

Antes de inferir, al terminar la inferencia y al consultar un run del chat, revalidamos sus resultados y su contexto. No basta con revisar las citas del turno más reciente: una respuesta anterior puede depender de otro mensaje que ya salió de la ventana de tres turnos.

Por eso recorremos también las dependencias históricas transitivas, con detección de ciclos y un límite de veinte runs. Es una comprobación de autorización; **no envía esos veinte turnos al modelo**. Si una dependencia ya no está autorizada o su documento fue retirado, se bloquea la reutilización.

La vista conserva la posición del turno, pero reemplaza su contenido por un aviso de evidencia no disponible. Tampoco entrega la respuesta histórica ni su pregunta en ese elemento redactado. La información original sigue almacenada en PostgreSQL: ocultarla por permisos o retirada no equivale a borrarla. Si el contexto quedó revocado, la UI indica iniciar una conversación nueva; no ignora la revocación para continuar.

Los cambios de permisos durante una inferencia pueden detener la publicación, pero no deshacer cómputo o datos que el servicio local ya recibió. Mantenemos las revalidaciones en las fronteras; no afirmamos que sean una transacción distribuida con Ollama.

## 6. Probar desde la consola

Con los servicios, modelos e índice de M7a preparados, iniciá la API y el worker de lecturas en terminales separadas. Si estaban corriendo con el código anterior, detenelos antes de iniciarlos de nuevo:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
```

```bash
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha
```

Emití una sesión si necesitás una:

```bash
uv run python scripts/users.py issue --actor reader-demo --workspace workspace-alpha
```

1. Abrí [la consola en Chat](http://127.0.0.1:8005/console#chat) y seleccioná tu archivo de credencial.
2. Pulsá **Nueva conversación** y preguntá «¿Quién coordina la entrega de Atlas?».
3. Con el documento de ejemplo debe responder Marta y mostrar una cita.
4. Continuá con «¿Y cuál es su correo electrónico?». La respuesta esperada es abstenerse porque ese dato no está en el documento.
5. Recargá la página, seleccioná nuevamente la credencial y abrí la conversación de la lista. La credencial solo estaba en memoria; los mensajes estaban en PostgreSQL.
6. Para probar recuperación, marcá **Pausar después de buscar**, enviá otro mensaje y usá **Recuperar este mensaje** cuando esté pausado.

El chat muestra por turno estado, run, decisiones, herramientas y tiempo activo. El enlace a Jaeger corresponde a la última traza de ese job. Los mensajes de chat tienen su propia vista; los endpoints CI y del lector independiente no entregan estos payloads sin su política de contexto.

## 7. API y UI sin duplicar autoridad

Las rutas son `/api/chat/conversations`, su detalle, `/messages` y `/runs/{run_id}/resume` dentro de cada conversación. Reutilizan la sesión local y las protecciones de origen, respuestas no cacheables y contenido de documentos. Otra identidad, aunque conozca un UUID, no puede listar, leer, continuar ni recuperar esa conversación.

La UI renderiza preguntas y respuestas como texto, sin ejecutar HTML del modelo. No guarda credenciales, mensajes ni conversaciones en localStorage. Una respuesta HTTP de una sesión anterior se descarta por el cliente compartido. Cambiar de conversación incrementa además una generación local para no pintar una respuesta tardía en el chat equivocado. Al salir de la sesión se limpia la pantalla y el formulario.

Mientras un turno sigue en curso, el formulario espera su cierre o recuperación. No cancela automáticamente una ejecución al cambiar de sección ni al cerrar el navegador. No añadimos borrado, edición, bifurcaciones, compartición ni retención automática de chats; el historial pertenece al actor/workspace y la lista muestra las treinta conversaciones más recientes.

## 8. Evaluación repetible

```bash
uv run python scripts/evaluate_chat.py
```

El ejercicio usa un corpus ficticio, Qwen3 y embeddings reales, Chroma, PostgreSQL y el worker del harness. Crea schema y colecciones temporales propios; no cambia los documentos ni el índice de la aplicación. Comprueba creación/envío idempotentes, respuesta inicial, referencia al turno anterior, abstención por correo ausente, pausa, recuperación sin repetir búsqueda, respuesta de seguimiento, historial desde otro store y ocultación tras retirar evidencia. La salida privada queda en `data/runtime/evals/chat-latest.json` y se exige limpieza para aprobar.

La corrida real pasó sus diez comprobaciones. La consulta de seguimiento incluyó Marta; esto demuestra resolución de esa referencia en el fixture, no calidad general de reformulación ni memoria a largo plazo. No sustituye la evaluación de doce positivas/ocho negativas de M7a ni elimina su falsa abstención conocida.

La suite completa pasó 264 pruebas unitarias y 220 de integración, con Chroma opt-in habilitado. Dos casos de integración añadidos después de esa corrida también pasaron: restauración desde otro store/retirada durante inferencia y reautorización de una dependencia fuera de la ventana del modelo. Son 222 integraciones únicas verificadas. Las dos pruebas JavaScript de sesión siguieron pasando. Las pruebas deterministas inyectan decisiones para verificar controles; el ejercicio y la prueba visual sí usan el modelo real.

## 9. Qué sigue

El siguiente incremento puede mejorar las evaluaciones conversacionales y conectar lecturas DevOps al LLM desde este chat, manteniendo comparaciones deterministas y el flujo de propuestas/aprobaciones separado. Este chat documental ya usa el mismo harness; no es todavía un agente autónomo con todos los conectores empresariales reales.

El historial se envía como mensajes de conversación siguiendo el [contrato de chat de Ollama](https://docs.ollama.com/api/chat). En esta implementación PostgreSQL conserva el estado; Ollama recibe el contexto que el host autoriza en cada decisión.

Validación visual final: tres turnos COMPLETED; cada uno consumió dos decisiones y una herramienta. El tercero terminó tras pausa/recuperación con dos intentos de worker, conservando su búsqueda. Sesión temporal de prueba revocada y cero trabajos activos al cerrar. La revisión de 278 archivos publicables no encontró secretos; reportes, credenciales e historial de ejecución permanecen excluidos de Git.
