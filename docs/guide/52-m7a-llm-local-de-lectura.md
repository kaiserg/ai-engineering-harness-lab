# M7a LLM local de lectura, respuestas y abstención

[Índice](../guide.md) · [← Anterior](51-m5g-preguntas-sin-respuesta.md) · [Siguiente →](53-m7b-chat-e-historial-persistido.md)

## 1. Qué incorporamos y por qué

Conectamos Qwen3 local a `LLMPort.decide()`, el puerto que el core ya utilizaba con modelos guionados. El usuario eligió Ollama para no configurar una API key ni hacer llamadas pagadas. El diseño previo de OpenAI queda como alternativa futura; no añadimos dos proveedores a la vez.

Este perfil responde preguntas documentales. No sustituye las decisiones de los recorridos CI ni habilita herramientas de escritura. Acotar primero la tarea permite separar fallos de comprensión, recuperación y autorización. Es una pregunta por run: todavía no es un chat con historial entre turnos.

Se añadió el SDK oficial `ollama`, fijado por `uv.lock`. El core conserva su ciclo; no ejecutamos un segundo loop dentro de un framework. El SDK transporta las solicitudes y devuelve la salida; nuestro adaptador valida y traduce decisiones a `ToolCall` o `FinalAnswer`.

## 2. Dos modelos diferentes

| Componente | Modelo | Función |
| --- | --- | --- |
| Embeddings | `qwen3-embedding:4b` | Convertir consulta y fragmentos en vectores. |
| Lector | `qwen3:4b` | Formular una búsqueda, interpretar fragmentos, responder o abstenerse. |

Ambos ya estaban disponibles localmente. No descargamos pesos ni instalamos otro servicio. El lector usa el manifest `359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7`.

La configuración del lector está en `harness/llm_reader.py`: endpoint fijo `127.0.0.1:11434`, modelo y digest permitidos, instrucciones, schemas, temperatura 0, seed 42, contexto 8192 y límite de 700 tokens de salida por decisión. Comprobamos el digest antes y después de inferir. Una etiqueta que cambie falla; no descarga ni elige otro modelo automáticamente. Se presupone un servicio local confiable: la comprobación no demuestra criptográficamente qué pesos ejecutó.

El perfil persistido es `ollama-qwen3-4b-document-reader-v1`. Para cambiar modelo, prompt, schemas u opciones de este contrato después de publicar el incremento, se debe crear otra versión del perfil y conservar la composición anterior para recuperar runs históricos. No se acepta configuración de modelos enviada por un usuario HTTP.

## 3. El recorrido de una pregunta

1. La API autentica al usuario, valida la pregunta y admite el run. No hace inferencia inline.
2. PostgreSQL guarda `read_request.query`, perfil y límites. La pregunta es inmutable durante las actualizaciones y la recuperación. Los payloads históricos sin este campo siguen leyendo `None`; no hace falta una migración SQL porque se guarda dentro del JSON existente.
3. El worker reclama el run con lease/fencing y resuelve sus permisos actuales.
4. Qwen3 devuelve una decisión estructurada para `knowledge_search`, con consulta y límite 3.
5. El core valida y autoriza la llamada. La herramienta consulta el índice activo, con su modelo de embeddings, y entrega fragmentos con cita, ACL y retirada revalidadas.
6. Se guarda el resultado confirmado antes de pedir la respuesta al modelo.
7. Qwen3 produce JSON con estado `answered` o `abstained`, respuesta y evidencia.
8. La política revalida los resultados al terminar la inferencia. El finalizador verifica el contrato antes de guardar la respuesta y su evaluación de evidencia.

La primera decisión usa JSON Schema de salida para describir una llamada de función; no depende del parser nativo `message.tool_calls` de Ollama. El modelo formula la consulta, pero este perfil exige una búsqueda antes de responder y permite solo una búsqueda confirmada. No es planificación abierta entre herramientas ni un agente DevOps autónomo.

En las pruebas iniciales, el modelo podía consumir el límite explicando la llamada en vez de emitirla. Se separaron las instrucciones de ambas fases y se usaron salidas estructuradas en las dos. Una salida truncada o inválida falla con `INVALID_MODEL_OUTPUT`; no se relaja el presupuesto ni se publica texto parcial. `think=False` y `/no_think` solicitan respuesta directa, sin depender de ellos como controles de seguridad.

## 4. Responder y abstenerse tienen contratos distintos

Una respuesta requiere texto no vacío y entre una y tres evidencias. Cada evidencia contiene una cita entregada por la herramienta y una cita textual exacta, no vacía, incluida en ese fragmento. Una cita inventada, texto modificado, propiedades adicionales o ausencia de evidencia bloquean la publicación.

Esto verifica procedencia e integridad, **no entailment**: una respuesta puede citar un texto verdadero que no sustente su conclusión. El modelo sigue siendo responsable de interpretar bien; las evaluaciones miden ejemplos de esa interpretación. La presencia de citas no es una garantía de factualidad universal.

Una abstención requiere `answer` vacío y ninguna evidencia. La aplicación publica una frase canónica: «No hay evidencia suficiente en los documentos autorizados para responder esa pregunta». El motivo es no permitir que un supuesto `abstained` incluya una respuesta especulativa.

Una caída de Ollama no se transforma en abstención: da un fallo de servicio. Sin acceso al modelo, cambio de digest, salida inválida y falta de evidencia son situaciones diferentes.

## 5. Controles y recuperación conservados

El registro del lector contiene únicamente `knowledge_search`. Una salida que solicite shell, escrituras, otro modelo, identidad o índice arbitrario es inválida. Las instrucciones del documento se entregan como contenido no confiable; además, carecen de autoridad para expandir el registro de herramientas.

Se mantienen permisos actuales por workspace/documento, reautorización antes de reutilizar contexto y antes de publicar, reservas persistidas, deadline global, CAS, lease/fencing, recuperación y cancelación. El ledger de escrituras y las aprobaciones no se modifican.

El recorrido normal consume dos decisiones y una herramienta. Los límites del perfil son tres intentos de modelo, dos de herramienta, 30 segundos por decisión, 10 por herramienta y 120 segundos activos. El margen permite un intento incierto; no se restablece al recuperar.

Si se interrumpe después de confirmar la búsqueda, recuperar reutiliza esos fragmentos y pregunta original. Si el documento fue retirado o ya no está autorizado, se bloquea antes de reenviarlo al modelo. Si se perdió una respuesta del LLM antes del commit, puede necesitarse otra inferencia; no prometemos exactamente una llamada física. La reserva incierta se cobra como en los perfiles anteriores.

La cancelación del cliente es asíncrona; el servicio local podría terminar cómputo ya recibido. No confundimos cancelar la espera con deshacer trabajo remoto.

## 6. Probar desde la consola

Con Ollama, PostgreSQL, Chroma e índice preparados según M5f:

```bash
uv sync --locked
# Si estás instalando desde cero, prepará el modelo desde la fuente oficial:
ollama pull qwen3:4b
```

El tag descargado debe coincidir con el digest del laboratorio. Si cambia en el futuro, hay que revisar/versionar el perfil; no modificar el digest para esconder un desacuerdo.

Iniciá la API y el worker de lecturas en terminales diferentes. Detené las instancias anteriores antes de iniciarlos de nuevo para cargar el adaptador:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
```

```bash
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha
```

Emití una sesión como en los capítulos anteriores, abrí la [consola](http://127.0.0.1:8005/console), seleccioná el archivo y elegí **Ejecuciones → LLM local · Lectura documental**. Escribí «¿Quién coordina la entrega de Atlas?» e iniciá la lectura. El detalle muestra pregunta, estado ANSWERED/ABSTAINED, respuesta, citas y contadores. Con el documento de ejemplo, debe responder Marta.

Luego preguntá «¿Cuál es el correo electrónico de Marta?». El documento de ejemplo no lo contiene: la conducta esperada es abstenerse, aunque el buscador devuelva el documento correcto por tema.

Para practicar recuperación, marcá **Pausar después de buscar para probar recuperación**. Cuando el worker esté PAUSED, usá **Recuperar ejecución**. El run termina conservando una invocación confirmada de herramienta. La opción reutiliza internamente el primer checkpoint histórico llamado `jira`; aquí significa primera búsqueda, sin ejecutar Jira.

También podés usar la CLI:

```bash
uv run python scripts/reader.py --session TU_SESSION_ID start --query '¿Quién coordina Atlas?' --pause-after-search
uv run python scripts/reader.py --session TU_SESSION_ID status RUN_ID
uv run python scripts/reader.py --session TU_SESSION_ID resume RUN_ID
```

Las respuestas documentales solo se consultan por la ruta del lector, que reautoriza su evidencia. Los endpoints CI anteriores las excluyen. El listado común muestra metadatos, sin preguntas ni contenido documental.

## 7. Evaluación repetible y límites del grader

```bash
uv run python scripts/evaluate_reader.py
```

`evals/reader_cases.json` define doce preguntas positivas y ocho sin respuesta sobre los ocho documentos sintéticos de M5f. Antes de ejecutar fijamos gates: al menos 90 % de positivos correctos, 100 % de negativas con abstención y 100 % de resultados que satisfagan el contrato de salida. No se ajustan gates para convertir un fallo en éxito.

El ejecutor usa PostgreSQL y Chroma reales en un schema/colecciones temporales propios, inferencia real y el mismo core. Guarda hashes de dataset/corpus, modelo/digest y resultado por caso en `data/runtime/evals/reader-latest.json`. Se añade un ejercicio real de interrupción y recuperación. El índice activo y documentos de la aplicación no se reemplazan.

Un positivo exige COMPLETED, ANSWERED, citar un documento relevante y contener al menos uno de los términos esperados del fixture. Una negativa exige COMPLETED, ABSTAINED y la frase canónica. El grader distingue abstenerse incorrectamente de fallar por servicio o contrato, aunque ambos cuentan como fallo de calidad.

`supported_citations` mide la fracción de casos que completaron el contrato estructural de respuesta o abstención; las citas/extractos de las respuestas son verificados por el finalizador. No mide sustento semántico de todas las afirmaciones ni prueba que el corpus sea verdadero. Un caso negativo no necesita cita porque precisamente no hay evidencia suficiente.

El dataset es de desarrollo, pequeño y conocido durante la implementación. No es un holdout ni demuestra rendimiento empresarial o resistencia general a prompt injection. Temperatura 0 y seed ayudan a repetir, pero no garantizan respuestas idénticas en otra versión del servidor o hardware.

### Resultado de este incremento

| Medición | Resultado |
| --- | --- |
| Positivas correctas | 11/12 (91,67 %) |
| Negativas con abstención correcta | 8/8 (100 %) |
| Contrato estructural de salida | 20/20 |
| Recuperación real | Aprobada; dos decisiones, una búsqueda confirmada |
| Limpieza de recursos temporales | Confirmada |

El gate pasó sin modificar sus valores. La pregunta `crash-time` («¿Cómo se contabilizan los segundos que pudo gastar el intento que murió?») produjo una abstención incorrecta, aunque `budget` apareció primero en los documentos recuperados. Queda como fallo de interpretación registrado; no se eliminó el caso ni se cambió su etiqueta.

Durante el desarrollo, la versión inicial con parser nativo de herramientas produjo salidas truncadas. La versión con decisiones JSON resolvió ese problema, pero inicialmente obtuvo 8/12 positivos. Se aclaró en las instrucciones que una regla general puede responder una pregunta procedimental sin repetirla literalmente; la ejecución final alcanzó 11/12. Estos ajustes usan el dataset de desarrollo, por lo que los números no son una evaluación independiente sobre datos nunca vistos.

Pasaron 260 pruebas unitarias y dos de JavaScript. La suite general de integración aprobó 209 y omitió dos pruebas Chroma opt-in; ambas pasaron después con `HARNESS_CHROMA_TESTS=1` (211 integraciones únicas aprobadas). Se verificaron API autenticada, rechazo de configuración extra, recuperación del worker, presupuesto, retirada antes de reusar resultados y bloqueo de escritura. Las pruebas del core inyectan decisiones para comprobar controles determinísticamente; la evaluación anterior sí llama al modelo real.

## 8. Trazas y privacidad

Con las variables de tracing de M6 habilitadas, cada decisión usa `model.decide`, la búsqueda `tool.invoke` y cada intento `worker.attempt`. Una recuperación produce otra traza con links a la anterior. El lector no añade preguntas, documentos, respuesta ni cadena de pensamiento a los atributos de OpenTelemetry.

La pregunta, los fragmentos confirmados, respuesta y extractos sí quedan en PostgreSQL como evidencia del run, protegidos por sus controles de acceso. Los reportes privados de evaluación incluyen respuestas sintéticas. Persistencia y retención son decisiones separadas de la privacidad de trazas; no se promete borrado automático.

## 9. Lo siguiente

Completar calidad/robustez del lector y añadir conversaciones persistidas y chat sobre este mismo motor. Después, integrar lecturas DevOps elegidas por el LLM manteniendo las comparaciones deterministas y las aprobaciones para efectos. Este incremento no cierra M7 ni convierte los conectores simulados en APIs reales.

## Fuentes

- [SDK y llamadas a herramientas de Ollama](https://docs.ollama.com/capabilities/tool-calling).
- [Salidas estructuradas de Ollama](https://docs.ollama.com/capabilities/structured-outputs).
- [Contrato de la API de chat](https://docs.ollama.com/api/chat).
- [Modos thinking y non-thinking de Qwen3](https://qwen.readthedocs.io/en/stable/inference/transformers.html).
