# M7d GitHub desde el chat por MCP

[Índice](../guide.md) · [← Anterior](54-m7c-jira-desde-el-chat.md)

## 1. Qué construimos

El chat ahora puede consultar un pull request con Qwen3 real, por el mismo ciclo de API → cola/worker → LLM → política → cliente MCP → servidor MCP → proveedor. Agregamos dos fuentes para conversaciones nuevas:

| Fuente | Proveedor | Necesita token |
| --- | --- | --- |
| GitHub · demostración | Servicio MCP local con demo/atlas#42 | No |
| GitHub · PR público | Servicio MCP propio que lee REST API pública de GitHub | No |

El usuario no tenía un PR de prueba disponible. Verificamos uno público existente: [octocat/Hello-World#1](https://github.com/octocat/Hello-World/pull/1). Leerlo no requiere crear ramas, PRs, comentarios ni modificar otro repositorio. La demostración sigue proporcionando datos estables para evaluaciones repetibles.

La fuente de cada conversación continúa siendo inmutable. Los chats documentales y Jira conservan sus perfiles. Este incremento consulta un PR por turno; todavía no combina automáticamente Jira → GitHub → Jenkins en una planificación abierta ni accede a repositorios privados.

## 2. Qué puede decidir el modelo

El LLM produce JSON para `github_get_pull_request`, con `repository` y `pull_number`. Aceptamos referencias completas:

```text
demo/atlas#42
https://github.com/octocat/Hello-World/pull/1
```

El host extrae pares repositorio/número de la pregunta y del historial seleccionado. El modelo debe elegir un par existente: no puede combinar el repositorio de una referencia con el número de otra. Los repositorios se normalizan a minúsculas en este perfil, manteniendo la comparación coherente con el alcance local.

Si falta una referencia completa, se pide repositorio y PR sin llamar a MCP. Si el modelo inventa otro recurso, se rechaza su salida. Mencionar una referencia no concede permiso: la política todavía debe autorizar el repositorio para el actor y workspace actuales.

La memoria agrega al turno anterior la referencia del PR efectivamente leído, tomada del resultado validado. Así «¿cuál es el head de ese PR?» puede resolverse desde contexto sin usar el texto del modelo como configuración. El historial sigue limitado a tres turnos y reautorizado transitivamente; cada respuesta nueva necesita una lectura nueva y evidencia actual.

## 3. Control de acceso y frontera MCP

El registro del perfil contiene solo `github_get_pull_request`. Solicitudes de merge, comentarios, cambios de archivos, búsqueda en cualquier repositorio o ejecución de código no obtienen una herramienta de escritura. Los adaptadores anteriores de CI y el ledger de acciones aprobadas permanecen separados.

Reutilizamos `MCPRead`: inicio de proceso stdio, initialize, descubrimiento, comparación del schema y llamada MCP. El servidor revalida rol y repositorio antes de acceder al proveedor. El descubrimiento no otorga autoridad al LLM.

La integración pública acepta únicamente GET sobre `https://api.github.com`, con rutas construidas a partir de argumentos validados, HTTPS verificado, sin credenciales, proxies ni redirects. No sigue enlaces del PR ni ejecuta su contenido. Un repositorio renombrado que responde con redirect no redirige silenciosamente la recuperación a otro destino.

El proveedor valida que el número y el repositorio base devueltos correspondan a la solicitud, y que el repositorio sea público. El fork del autor no cambia la autorización sobre el repositorio base. Solo conservamos campos acotados: estado OPEN/CLOSED, revisión head, repositorio/número, título, descripción de texto, merged, draft y updated. No recuperamos diffs, archivos, reviews, comentarios, checks ni resultados de CI.

## 4. Evidencia y significado del resultado

En demostración se conserva la referencia sintética anterior. En público se genera `github-public:` más un hash de 128 bits del snapshot normalizado. Esto mantiene una referencia corta incluso con nombres de repositorio largos; el snapshot conserva el recurso que representa.

El texto del PR se arma con un orden explícito de campos, independiente del orden de diccionarios/JSONB. Una respuesta necesita una cita de ese snapshot y un extracto textual exacto. Los datos del PR y del historial son contenido no confiable, sin autoridad para cambiar reglas o herramientas.

La revisión `head` identifica el código observado. **No demuestra que CI pasó ni autoriza un merge.** La evaluación incluye una pregunta sobre pruebas de CI que estos campos no pueden contestar y exige abstención. Como en los pasos previos, procedencia exacta no equivale a demostrar entailment semántico de toda afirmación generada.

Un error de GitHub no se convierte en «no hay evidencia». NOT_FOUND, FORBIDDEN, indisponibilidad, salida inválida y límite del proveedor conservan su significado como fallos. El chat puede abstenerse cuando el proveedor respondió correctamente pero el campo solicitado no está disponible.

## 5. Recuperación y reautorización

El turno conserva la pregunta, perfil, historial seleccionado, presupuestos y resultado confirmado. Se mantienen admisión idempotente/atómica, un mensaje en curso, CAS, lease/fencing, deadline y cancelación. El recorrido normal consume dos decisiones y una herramienta, dentro de los límites anteriores de tres decisiones intentadas y dos herramientas intentadas.

Si se pausa después del commit de la lectura, recuperar reutiliza exactamente ese resultado y revisión. No repite el `github_get_pull_request` confirmado ni reinicia los contadores. Si el intento quedó incierto antes del commit, otro intento consume otra reserva de lectura, como antes.

En el modo público, reutilizar o entregar evidencia guardada comprueba que el repositorio siga accesible públicamente. Un 401/403/404 impide entregar ese contenido y se representa como evidencia revocada. Los permisos locales actuales se verifican de todas formas, aunque la información esté publicada en Internet.

Para no multiplicar consultas por cada mensaje histórico del mismo repositorio, una vista comparte un lote de comprobaciones de visibilidad. Ese lote vive solo durante esa consulta: **no es un caché de autorización permanente ni con TTL entre requests**. Cada nueva entrega/inferencia vuelve a comprobar. Las dependencias históricas también se reautorizan, incluidas las que ya no se envían al modelo.

Estos probes de visibilidad son GET adicionales, distintos de la lectura de PR decidida por el LLM. No sustituyen el snapshot ni cuentan como otra herramienta del run; sí consumen tiempo y cuota externa. Una recuperación conserva hechos observados, no garantiza que el PR no haya cambiado desde la lectura original.

## 6. Cuota anónima y actualización de la pantalla

GitHub documenta un límite primario de **60 solicitudes por hora e IP para acceso anónimo**. Ese presupuesto lo comparten lecturas y probes de los procesos locales; el límite de herramientas del harness es otro control distinto.

Mientras un turno está activo, la consola sigue su evolución. Al quedar terminal una conversación pública, detiene las consultas automáticas de su detalle; muestra el snapshot ya obtenido y ofrece **Actualizar historial** para volver a pedir una vista reautorizada. El listado de conversaciones y la identidad local siguen consultándose sin llamar a GitHub.

Enviar un mensaje, cambiar de conversación o pedir actualización fuerza una lectura del detalle. Se agregó una prueba para que un envío concurrente con otra consulta de lista no deje el chat detenido en un estado antiguo. Los permisos remotos de una vista ya renderizada no se verifican continuamente en background: no se promete borrar información que el usuario ya recibió.

Si GitHub informa 429 o un 403 identificado como cuota agotada, devolvemos `PROVIDER_RATE_LIMITED`. No esperamos automáticamente, no reiniciamos límites del run ni usamos un token ambiental para evitarlo. Puede ser necesario esperar a que se reponga la cuota. Una conexión autenticada con permisos mínimos será un incremento explícito; no reutilizamos credenciales del conector de Codex dentro de esta app.

## 7. Probarlo

Para habilitar el ejemplo real en la identidad local:

```bash
uv run python scripts/github_public.py allow --repository octocat/Hello-World
uv run python scripts/github_public.py check --repository octocat/Hello-World --pull-number 1
```

`allow` añade solo el repositorio a la pertenencia local del actor/workspace, conservando otros roles y recursos. No concede privilegios en GitHub ni modifica su repositorio. `check` comprueba lectura anónima; no pide token. Los flags `--actor` y `--workspace` permiten indicar otra identidad configurada.

Si API/worker estaban ejecutando el código anterior, detenelos antes de iniciarlos de nuevo en terminales separadas:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
```

```bash
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha
```

En [Chat](http://127.0.0.1:8005/console#chat), seleccioná tu credencial y elegí **GitHub · demostración** o **GitHub · PR público** antes de crear una conversación nueva.

- Demo: «Consultá demo/atlas#42 y decime su estado».
- Público: «Consultá https://github.com/octocat/Hello-World/pull/1 y decime su estado».
- Seguimiento: «¿Cuál es la revisión head de ese PR?».
- Ausencia: «¿Cuántas pruebas de CI pasaron?» debe abstenerse con estos datos.

Para probar recuperación, marcá **Pausar después de la lectura** y luego **Recuperar este mensaje**. Conserva el mismo run y muestra su evidencia y enlace a Jaeger. Una nueva conversación de GitHub no transforma las conversaciones Jira o documentales anteriores.

## 8. Evaluación y límites

```bash
uv run python scripts/evaluate_github_chat.py
```

El ejercicio usa Qwen3, PostgreSQL y el servidor MCP GitHub de demostración reales, con schema temporal y sin acceso a GitHub externo. Exige siete criterios: pedir referencia ausente, pausa, respuesta citada, recuperación sin repetir lectura, seguimiento de revisión, abstención por CI ausente y rechazo de otro repositorio. El reporte privado queda en `data/runtime/evals/github-chat-latest.json`; limpiar el schema es obligatorio para aprobar.

La corrida pasó siete de siete criterios. También se comprobó por API autenticada, worker, Qwen3 y MCP la lectura pública de octocat/Hello-World#1, su pausa/recuperación con dos decisiones/una herramienta/dos reclamos, y un seguimiento que devolvió el head observado. No se hizo ningún cambio en ese repositorio.

Las pruebas de controles inyectan decisiones y respuestas para verificar rechazo de escritura, otro repositorio, cita/extracto inventados, recurso devuelto incorrecto, repositorio privado, cuota agotada y revocación de visibilidad. No dependen de que el modelo coopere para demostrar enforcement. Los resultados de una muestra pequeña no certifican calidad ni seguridad universal.

## Referencias

- [GET pull request y acceso público sin autenticación](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request).
- [Límites REST de GitHub](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

Cierre M7d: 280 pruebas unitarias, 232 integraciones completas con Chroma y un caso adicional de lote/revocación público aprobado (233 integraciones únicas), tres pruebas JavaScript aprobadas. Los siete criterios del evaluador real demo y el ejercicio público por API/worker/MCP pasaron. Jaeger mostró una decisión y tres probes de visibilidad en la recuperación, sin repetir la herramienta confirmada. Se revisaron 297 archivos publicables, sin secretos ni datos de la conexión privada Jira; sesión temporal revocada, cero trabajos activos y cero schemas temporales del evaluador. No se repitieron Compose/rotación ni se hicieron commit/push.
