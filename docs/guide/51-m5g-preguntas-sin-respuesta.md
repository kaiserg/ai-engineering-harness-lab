# M5g Preguntas sin respuesta y camino al chat

[Índice](../guide.md) · [← Anterior](50-m5f-embeddings-semanticos-y-evaluaciones.md)

## 1. Qué estamos construyendo

El objetivo es una aplicación de agentes para DevOps. El harness es el motor que controla el ciclo modelo → herramienta → modelo: permisos, presupuestos, persistencia, recuperación, aprobación y observabilidad. La auditoría permite reconstruir lo ocurrido; es una capacidad del sistema, no su única finalidad.

Hoy el ciclo tiene decisiones guionadas y herramientas DevOps simuladas. Los embeddings semánticos sí usan un modelo real local, pero este modelo convierte texto en vectores: no conversa ni decide herramientas. La interfaz actual permite investigar escenarios y consultar fragmentos, sin un chat libre.

El diseño inicial ya contemplaba un adaptador LLM con function calling; quedó pendiente en M7. Una interfaz conversacional con historial no estaba definida explícitamente. No se debe presentar la consola actual como un agente conversacional terminado.

El recorrido siguiente deberá llevar a esa experiencia: conectar un LLM real de lectura al mismo core, evaluar respuestas y abstención, y añadir un chat con conversaciones persistidas y runs identificables. Las herramientas propuestas por el LLM deberán atravesar las políticas existentes; el chat no autorizará escrituras ni sustituirá las aprobaciones. La elección del proveedor/modelo, cuotas y contrato de conversación se documentará en ese incremento, antes de modificar el core.

## 2. Por qué agregar preguntas sin respuesta

El corpus dice que Marta coordina Atlas. Eso permite responder quién coordina la entrega, pero no cuál es su correo o cuándo se publicará la próxima versión. Un fragmento puede tratar el tema correcto y no contener el dato solicitado.

Agregamos `evals/unanswerable_cases.json`, separado de los doce casos positivos de M5f para preservar su versión y sus gates originales. Incluye ocho preguntas con una justificación manual de por qué el corpus sintético no contiene la respuesta: presupuesto monetario, fecha de entrega, correo, SHA concreto, minutos de solapamiento, historial de aprobaciones, clima y receta. La ausencia se refiere a este corpus; no significa que el dato sea imposible de conocer por otros medios.

No metemos esas justificaciones en los documentos ni se las enviamos al buscador. Son etiquetas de la evaluación, no un detector disponible durante una consulta real.

## 3. Qué hace la evaluación

```bash
uv run python scripts/evaluate_retrieval.py
```

Requiere PostgreSQL, Chroma y Ollama con el modelo fijado ya preparados como en M5f. El ejecutor crea ocho documentos ficticios en un schema y colecciones temporales propios. Para cada perfil consulta las doce preguntas positivas y luego las ocho negativas. Conserva las comprobaciones de cita, alcance y retirada, y elimina sus recursos al terminar. No modifica el índice activo de la aplicación.

Para cada pregunta negativa registra `expected_action: abstain`, cantidad de candidatos y documentos recuperados. Esa acción esperada viene del fixture; no significa que la aplicación haya detectado la ausencia. `queries_with_candidates` cuenta cuántas consultas negativas devolvieron vecinos, sin llamarlos respuestas ni falsos positivos de un clasificador que aún no existe.

El reporte queda en `data/runtime/evals/retrieval-latest.json`, excluido de Git. Incluye hashes de ambos datasets y declara `answerability_evaluated: false` y `runtime_abstention_implemented: false`. `passed_scope` aclara que el PASS corresponde a los gates positivos, citas, acceso y limpieza; no certifica abstención.

## 4. Resultado observado

| Perfil | Top-1 positivo | Recall@3 positivo | Negativas con candidatos |
| --- | --- | --- | --- |
| Léxico | 50 % | 83,33 % | 8/8 |
| Semántico | 100 % | 100 % | 8/8 |

Las citas corresponden a los textos originales, pero esos textos no contestan las preguntas negativas. El buscador ordena vecinos del corpus elegible: no comprueba por sí solo si una afirmación está sustentada.

No añadimos un umbral de distancia arbitrario. Su calibración necesitaría separar ejemplos de ajuste y prueba, medir abstenciones incorrectas en preguntas respondibles y repetir la evaluación al cambiar modelo, corpus o fragmentación. Una distancia no equivale a probabilidad de verdad.

## 5. Qué cambia y qué sigue pendiente

Este incremento añade casos negativos y medición; no cambia la búsqueda productiva ni promete abstención automática. No toca permisos, ledger, leases, recuperación, límites o workers. Pasaron ocho pruebas focalizadas de embeddings/evaluación y el ejercicio real con PostgreSQL, Chroma y Ollama, con limpieza confirmada y las comprobaciones de alcance/retirada vigentes. No se repitieron las suites completas de integración, contenedores o rotación porque sus implementaciones no cambiaron.

Para probar abstención real falta un componente que produzca una decisión o respuesta. El próximo incremento debe conectar el LLM de lectura bajo los controles existentes y usar estas preguntas para comprobar cuándo devuelve «no hay evidencia suficiente». Luego el chat debe exponer ese mismo recorrido y permitir seguirlo en sus trazas, sin crear un segundo motor fuera del harness.
