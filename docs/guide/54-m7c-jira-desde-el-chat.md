# M7c Jira desde el chat por MCP

[Índice](../guide.md) · [← Anterior](53-m7b-chat-e-historial-persistido.md) · [Siguiente →](55-m7d-github-desde-el-chat.md)

## 1. Objetivo y alcance

El chat ahora puede leer un ticket de Jira mediante una decisión real de Qwen3. El recorrido es navegador → API → cola/worker → LLM → política → cliente MCP → servidor MCP → Jira. El resultado vuelve al modelo y se publica solo si satisface el contrato de evidencia. No hay un loop alternativo dentro del servidor MCP.

Tenemos tres fuentes para una conversación nueva:

| Fuente | Uso |
| --- | --- |
| Documentos | Chat documental anterior, sin cambios de autoridad. |
| Jira · demostración | Servidor MCP ficticio anterior con DEV-123; sirve para evaluaciones repetibles. |
| Jira Cloud · cuenta conectada | Servidor MCP propio que consulta un sitio real usando su REST API. |

La fuente se fija al crear la conversación. No depende de instrucciones del ticket ni cambia a mitad del historial. Así distinguimos la prueba del control del agente de la integración externa y preservamos recuperación reproducible. Todavía no mezclamos documentos, Jira, GitHub y Jenkins libremente en una misma conversación ni damos herramientas de escritura al LLM.

## 2. ¿Sirve Jira Cloud Free?

Sí, para este laboratorio de lectura por API. La autenticación y los permisos de Jira siguen aplicando. El plan Free tiene limitaciones de configuración de permisos y auditoría, por lo que no lo usamos como demostración de todos los controles empresariales de Atlassian. Los permisos del harness y sus trazas se mantienen por separado.

Atlassian documenta autenticación de scripts con email y API token. Recomendamos tokens con scopes de lectura: usan el gateway `https://api.atlassian.com/ex/jira/{cloudId}`. La opción sin scopes usa el dominio del sitio y se conserva como alternativa explícita, sin asumir que su token tenga privilegios mínimos. OAuth/delegación por usuario siguen pendientes.

## 3. Configurar una cuenta sin publicar credenciales

Desde la carpeta del proyecto, creá primero un ticket de prueba. Necesitás el origen HTTPS del sitio y la clave del proyecto. La URL del tablero permite identificar ambos; el número del tablero no es una clave de ticket.

Creá el token en [Atlassian](https://id.atlassian.com/manage-profile/security/api-tokens), con caducidad y scopes de lectura de Jira. Para GET issue, la documentación recomienda el scope clásico `read:jira-work`; los scopes granulares deben cubrir el endpoint según su referencia oficial. El código no puede inspeccionar el privilegio efectivo del token: aun con un token más amplio, solo implementa GET.

```bash
uv run python scripts/jira_cloud.py configure --site https://tu-sitio.atlassian.net --project DEMO
```

El comando pide email y token de forma interactiva. El token se captura con `getpass`, sin mostrarse ni pasarlo como argumento del proceso o del shell. El cloud ID se consulta desde el endpoint público documentado del sitio. Para un token sin scopes existe `--unscoped`; no lo elige automáticamente tras un rechazo.

La configuración queda bajo `data/runtime/jira-cloud/`, excluida de Git, en un archivo regular con permisos 0600 y sin seguir symlinks al leerlo. El identificador de conexión deriva de sitio, cloud ID, proyectos, actor y workspace. Alterar esos valores bajo el mismo identificador falla. Email/token no forman parte de ese identificador, para no convertir una futura renovación de credencial en cambio de recurso.

El archivo `current` selecciona la conexión para conversaciones nuevas. Cada conversación Cloud guarda el identificador dentro de su perfil: cambiar `current` no redirige conversaciones ni recuperaciones anteriores hacia otro sitio. El configurador no sobrescribe un archivo existente; una futura rotación administrada requiere un procedimiento explícito, no editar destinos de una conexión en uso.

A continuación, concedé únicamente los proyectos de esa conexión al actor local y probá acceso:

```bash
uv run python scripts/jira_cloud.py grant-local
uv run python scripts/jira_cloud.py check --issue DEMO-1
```

`grant-local` conserva roles y otros recursos existentes; añade los proyectos configurados en la pertenencia del actor/workspace seleccionados. El GET de comprobación no imprime el contenido del ticket. La credencial de Jira es distinta del archivo de sesión de la consola. Ni token ni email se envían al LLM ni se publican en configuración de UI. API/worker y servidor MCP son procesos locales de confianza que pueden leer esa credencial; no afirmamos aislamiento de secretos entre esos procesos.

## 4. Decisiones del modelo y herramientas permitidas

Qwen3 genera una decisión estructurada para `jira_get_issue` con un único `issue_key`. La clave debe aparecer en la pregunta o en el historial seleccionado. Si no hay ninguna clave disponible, el host responde pidiéndola, aunque el modelo haya propuesto un valor inventado. Si propone otra clave no presente, una escritura u otra función, se rechaza la salida antes de abrir MCP.

Esto evita adivinar un ticket como DEV-123 por defecto. Una clave mencionada tampoco basta para autorizarla: la política verifica rol, proyecto, propietario y workspace vigentes. En Cloud se agrega el alcance de la conexión local. La cuenta Atlassian debe tener además acceso real al recurso.

El registro ejecutable contiene solo `jira_get_issue`. No expone creación, transición, borrado, comentarios, scripts, URLs arbitrarias ni lectura de adjuntos. El servidor MCP revalida la identidad y el proyecto antes de llamar a la API; no confía en que el host ya lo comprobó.

El transporte MCP de este incremento es stdio, como los adaptadores locales anteriores. La llamada del servidor a Atlassian usa HTTPS verificado, sin proxies ni redirects, con timeout acotado y sin reintento automático. No sustituimos el ejercicio previo de identidad/TLS del MCP HTTP ni afirmamos que stdio sea una identidad empresarial distribuida.

## 5. Qué información entregamos al LLM

Se consulta un ticket exacto con una lista fija de campos: summary, status, description, updated y project. Se valida que Jira devuelva la misma clave y proyecto solicitado. El cuerpo HTTP y la profundidad/cantidad de nodos ADF tienen límites. La descripción se convierte a texto acotado: no se siguen enlaces, menciones ni adjuntos.

El adaptador limita título, estado y descripción. Por lo tanto, este lector no ofrece el contenido completo de cualquier ticket ni todos los campos personalizados. Un dato truncado o no consultado puede producir abstención; no se interpreta como que nunca haya existido en Jira.

La referencia Cloud contiene conexión, clave y un hash del snapshot normalizado. No incluye el token ni una URL elegida por el modelo. La respuesta debe citar esa referencia y copiar un extracto textual exacto de los datos recuperados. El estado se describe como observado en esa lectura: recuperar un resultado guardado no demuestra que Jira no haya cambiado después.

La representación de los campos usa un orden explícito. PostgreSQL JSONB puede cambiar el orden de un objeto al guardarlo; no debe cambiar por eso el texto que recibe el modelo al recuperar. Se agregó una prueba de esta propiedad.

Los textos de tickets e historial siguen siendo datos no confiables. Las instrucciones del ticket no amplían el catálogo de herramientas ni conceden permisos. La comprobación de fuente/extracto verifica procedencia, no demuestra que toda conclusión del LLM esté semánticamente sustentada.

## 6. Recuperar sin ocultar una revocación

Cada mensaje conserva su run, perfil, historia seleccionada, límites y resultados confirmados. Se mantienen admisión atómica e idempotente, un turno en curso, CAS, lease/fencing, timeouts, deadline global y reservas persistidas. El recorrido normal termina con dos decisiones y una herramienta; pedir una clave ausente termina sin herramienta.

La pausa didáctica ocurre después del commit de la lectura MCP. Recuperar utiliza ese resultado, sin repetir la invocación confirmada de `jira_get_issue`. Si la lectura quedó incierta antes del commit, sigue la regla anterior: otro intento de lectura consume otra reserva. No se reinician contadores al recuperar.

**Las comprobaciones de acceso son lecturas adicionales.** Para evidencia Cloud guardada, el harness hace GET con el campo mínimo `updated` para comprobar que la credencial siga viendo el ticket. Estas consultas no reemplazan el snapshot ni se cuentan como una nueva herramienta decidida por el LLM. Sí generan tráfico y tiempo de validación, incluidos en la reserva del modelo cuando ocurren en ese paso.

Un 401/403/404 al reautorizar impide entregar la evidencia histórica. No se intenta mostrar el dato guardado porque antes estaba permitido. Los fallos temporales y límites del proveedor bloquean la operación con error, sin transformarse en abstención. La vista del historial también reautoriza sus dependencias. Revalidar varios turnos puede generar múltiples probes; el máximo de veinte turnos acota el ejercicio, pero no reemplaza un diseño eficiente de autorización para alto volumen.

El token es una credencial de una cuenta Atlassian local compartida por esta conexión, restringida a un actor/workspace del laboratorio. No hay OAuth por usuario, service account empresarial ni garantía de permisos mínimos en la cuenta Free. El ledger de escrituras no cambia: no conectamos escrituras reales.

## 7. Usar Jira desde el chat

Reiniciá API y worker si estaban ejecutando el código anterior:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
```

```bash
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha
```

En [Chat](http://127.0.0.1:8005/console#chat), seleccioná tu sesión local y elegí **Jira · demostración** o **Jira Cloud · cuenta conectada** antes de pulsar **Nueva conversación**. Una conversación documental existente conserva su fuente.

- Demo: «Consultá DEV-123 y decime su estado».
- Cloud: usá la clave de tu ticket real.
- Seguimiento: «¿Y en qué estado está ese ticket?».
- Sin clave previa: «Revisá un ticket» debe pedir la clave.
- Un dato no presente: debe abstenerse o fallar por un contrato inválido; no publicarlo sin evidencia.

Para recuperar, marcá **Pausar después de la lectura**, enviá la pregunta y, cuando el turno esté pausado, pulsá **Recuperar este mensaje**. El enlace de Jaeger conserva el intento actual y sus vínculos con el anterior. No registramos pregunta, cuerpo del ticket, token o email en atributos de trazas. PostgreSQL sí conserva los snapshots como evidencia autorizada del run; las políticas de retención siguen siendo un pendiente separado.

## 8. Evaluaciones y evidencia de este paso

```bash
uv run python scripts/evaluate_jira_chat.py
```

El ejercicio usa Qwen3 y el servidor Jira MCP ficticio reales, con schema PostgreSQL aislado y sin credencial Atlassian. Comprueba siete criterios: pedir clave, pausa, respuesta citada, recuperación sin repetir herramienta, seguimiento, abstención por dato ausente y rechazo de otro proyecto. El reporte privado está en `data/runtime/evals/jira-chat-latest.json`; la limpieza es obligatoria para aprobar.

La corrida final pasó los siete criterios. Las pruebas deterministas fuerzan llamadas prohibidas y errores de frontera para comprobar controles independientemente de la disposición del modelo a obedecer instrucciones. La suite completa pasó 273 pruebas unitarias y 227 de integración con Chroma opt-in; un caso adicional de integración verificó revocación Cloud e identidad de conexión congelada (228 integraciones únicas verificadas). Las dos pruebas JavaScript de sesión siguieron pasando.

La conexión Cloud del usuario se probó con un ticket real autorizado. La lectura, pausa y recuperación terminaron con dos decisiones, una herramienta confirmada y dos reclamos del worker, conservando el mismo resultado guardado. Un seguimiento real también se completó. Token/email no aparecieron en los payloads del chat. El reporte privado solo guarda metadatos de verificación, sin copiar el contenido del ticket a documentación pública.

Durante las pruebas, dos seguimientos produjeron salidas inválidas que fueron bloqueadas. Se conservaron como turnos FAILED, sin respuesta publicada ni reinicio de contadores; después de estabilizar la representación del ticket, el nuevo seguimiento pasó. No atribuimos esos rechazos a una única causa demostrada ni afirmamos determinismo del LLM. Esta es evidencia funcional de un caso real y un fixture pequeño, no una tasa de calidad empresarial ni una certificación de seguridad universal.

## 9. Próximo incremento

Conectar GitHub y después Jenkins al chat conservando vínculos verificados, comparación determinista de revisiones y presupuestos. Más adelante se podrá decidir entre varias herramientas en el mismo run. El MVP de este paso mantiene una fuente y una lectura por turno para no introducir todo ese problema de planificación a la vez.

## Referencias

- [Jira Cloud Free y sus límites](https://support.atlassian.com/jira-cloud-administration/docs/what-is-the-free-jira-cloud-plan/).
- [Restricciones de permisos en Free](https://support.atlassian.com/jira-cloud-administration/docs/permissions-and-issue-level-security-in-free-plans/).
- [Autenticación REST con email y API token](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/).
- [Tokens con scopes y gateway de Atlassian](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/).
- [Obtener el cloud ID](https://support.atlassian.com/jira/kb/retrieve-my-atlassian-sites-cloud-id/).
- [GET issue y permisos/scopes](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-get).

Cierre de validación: sesión temporal de prueba revocada, cero trabajos activos y cero schemas temporales del evaluador Jira. Jaeger mostró en el intento recuperado una decisión de modelo y tres probes de acceso, sin repetir la tool MCP guardada. Se revisaron 287 archivos publicables: sin secretos ni coincidencias con token, email, sitio o cloud ID de la conexión privada. Enlaces locales válidos; sin commit ni push en este paso.
