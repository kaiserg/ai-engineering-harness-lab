# Etapa 2 — Modelo de amenazas del harness DevOps

> **Estado actualizado:** el recorrido de lectura usa modelo y backends DevOps simulados. PostgreSQL/API/workers, Jira MCP Streamable HTTP con identidad de servicio/TLS, rotación y observabilidad funcionan en local. M4a–M4h incluyen propuestas/aprobaciones, ledger, despacho MCP, frontend, cierre revisado, worker de escrituras, trazas/métricas y ocho evaluaciones deterministas. Siguen pendientes identidad de usuarios y aislamiento operativo completos, RAG M5 y proveedores/LLM reales de M7. Ver [la matriz de cierre](docs/status.md) y [la guía](docs/guide.md) para evidencia y límites.

**Estado:** amenazas y controles a implementar y comprobar. No equivale a una certificación ni a una revisión de seguridad de producción. El [alcance](01-alcance-y-escenarios.md) define escenarios; la [arquitectura](02-arquitectura.md) ubica componentes; los [contratos](04-contratos-y-estados.md) concretan controles.

## 1. Activos y límites de confianza

Proteger credenciales de integración, documentos/código no públicos, identidad, permisos, aprobaciones, evidencia del PR, estado de acciones externas, disponibilidad y presupuesto. Una ejecución puede acceder a información sensible aunque solo tenga herramientas de lectura.

Consideramos entradas no confiables los mensajes del usuario, tickets, diffs, README, logs de Jenkins, documentos RAG y resultados de herramientas. El modelo también produce datos que deben validarse. Un servidor MCP o una API externa puede fallar, cambiar de versión o devolver contenido malicioso; «viene de nuestra integración» no vuelve confiable el contenido.

Límites principales:

1. Navegador→API: identidad, ownership y validación de solicitudes.
2. API→worker: persistencia, identidad de servicio y versión del trabajo.
3. Harness→LLM: datos permitidos, límites de contexto y exposición al proveedor.
4. Harness→MCP: identidad dirigida al servicio, schemas y autorización por recurso.
5. MCP→DevOps: credenciales limitadas y comportamiento real del proveedor.
6. Ingestión→archivos/Chroma: parsing, aislamiento y ACL del conocimiento.
7. Aplicación→telemetría: redacción y control de acceso operativo.

La red interna o localhost no sustituyen autenticación en el diseño objetivo. Las identidades de prueba se emiten o mapean por un componente controlado; no se aceptan cabeceras `role=admin` como autenticación real.

## 2. Amenazas, controles y evidencia

| ID | Ataque o fallo | Control previsto | Prueba de aceptación |
| --- | --- | --- | --- |
| T01 | Instrucciones en README/RAG piden revelar claves o borrar recursos. | Evidencia separada de instrucciones, herramientas mínimas y autorización obligatoria en ejecución. | Forzar llamada prohibida sin pasar por el LLM; rechazo en host y servidor. S04 fuerza decisiones adversariales con modelo simulado; la eval con LLM real sigue pendiente. |
| T02 | Cambiar run_id, ticket o workspace para leer datos ajenos. | Ownership en API, consultas con workspace validado, ACL de recursos y FK compuestas. | Identidad A no puede leer run, cita, archivo o auditoría de B, aunque conozca sus IDs. |
| T03 | Un servicio privilegiado actúa por un usuario sin permiso. | Autorizar actor y servicio, validar audiencia, scopes y recurso; no reenviar tokens a destinatarios distintos. | Token para harness rechazado por MCP; integración no ejecuta acción fuera de job/repositorio permitido. |
| T04 | Aprobar una propuesta y sustituir argumentos/revisión. | Propuesta inmutable, hash canónico, vencimiento y revalidación de revisión/política al ejecutar. | Cambiar PR head o parámetro invalida la aprobación S06. |
| T05 | Repetir una aprobación o correr dos workers. | Decisión única, actualización condicional y lease/fencing; operation_id estable y estrategia remota explícita. | Dos ejecuciones simultáneas no crean dos despachos locales; caída remota incierta pasa a UNKNOWN. |
| T06 | SSRF o ejecución de comandos mediante URL/job/path. | IDs de recursos, endpoints preconfigurados, sin HTTP genérico/shell, restricción de red y validación de redirects. | URL privada, hostname alternativo o job fuera de allowlist se rechaza antes del transporte. |
| T07 | Documento malicioso, archivo enorme o recorrido de directorios. | IDs internos, cuotas, tipos iniciales TXT/MD, parsing sin ejecución y límites del worker. | `../`, archivo sobredimensionado y tipo no admitido rechazados sin escribir fuera del área asignada. |
| T08 | Un corpus autorizado contamina resultados de otro workspace. | Selección de índice y filtro ACL del servidor antes de devolver texto, caches con scope de identidad y versión. | Un fragmento restringido no sale en resultados, citas ni respuestas; no basta ocultarlo en la UI. |
| T09 | Credenciales o datos sensibles en logs, trazas e informes. | Allowlist de atributos, redacción, sin prompts/diffs por defecto; excepciones sanitizadas. | Secretos canario ausentes en todos los exporters e informes de un fallo. |
| T10 | Loop infinito, avalancha de tools o consumo descontrolado. | Límites de pasos/tokens/deadline, cuotas, concurrencia acotada y reintentos centrales. | LLM falso que repite llamadas se detiene; no se disparan reintentos multiplicados por capas. |
| T11 | Declarar listo un cambio con CI de otra revisión o datos obsoletos. | Identidad de repositorio y revisión en evidencia, reglas deterministas y precondiciones. | S03 nunca produce estado técnico READY para una revisión diferente. |
| T12 | Retirar documento pero conservarlo en búsqueda/caché o reactivarlo desde un job viejo. | Tombstone inmediato, lectura de ACL vigente, versiones y activación condicional. | S11: el job anterior no reactiva la versión retirada. |
| T13 | Worker pierde acceso a base/auditoría pero ejecuta escritura externa. | Fallar cerrado antes del despacho si no se puede guardar intención/aprobación/ownership. | Al cortar PostgreSQL antes de enviar, el adaptador de escritura no recibe la llamada. |
| T14 | Integración/dependencia cambia schemas o herramientas publicadas. | Contratos versionados, inventario permitido, límites de respuesta y lockfiles. | Tool desconocida o schema incompatible se rechaza; nunca se autoriza por descubrimiento automático. |

Las guías MCP describen riesgos de confused deputy, token passthrough y SSRF; estos controles adaptan esas categorías al laboratorio. [Fuente primaria de MCP](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices).

## 3. Política de acciones sensibles

Una solicitud del usuario permite evaluar una acción, no eludir políticas. La aprobación de una persona tampoco crea credenciales ni derechos que la integración no tenga.

La política combina actor, workspace, recurso, herramienta, revisión y contexto. Una acción requiere tanto autorización del solicitante como aprobación de un actor habilitado distinto. En el laboratorio se rechaza autoaprobación. En producción la separación de responsabilidades y excepciones debe validarse con el equipo propietario.

La aprobación incluye vencimiento y hash de la propuesta. Tras un rechazo, vencimiento, modificación o revocación de permisos no se reutiliza. La ejecución revalida derechos del solicitante y del aprobador; una versión nueva de política que afecte la acción obliga a volver a evaluarla y, para la primera implementación, a solicitar otra aprobación.

## 4. Identidad y credenciales

El modo local usa identidades sintéticas y tokens de desarrollo fuera de Git; no se presenta como OIDC productivo. Al conectar servicios reales se integrará un proveedor OIDC/OAuth existente y una biblioteca mantenida, sin diseñar criptografía ni un servidor OAuth propio.

El servicio valida firma o token opaco por el mecanismo elegido, emisor, audiencia y vencimiento. El workspace se verifica contra asignaciones del servidor, no solo contra un campo enviado por el cliente. La política no confía en parámetros generados por el modelo como prueba de identidad.

Cada conector tiene credenciales separadas, limitadas a un sandbox. Las credenciales no entran al contexto del LLM, la respuesta MCP, el navegador o el baggage de trazas. El diseño no permite al administrador de conocimiento editar endpoints de modelos, URLs de conectores o secretos.

## 5. Autorización documental y exposición al modelo

Antes de recuperar se valida workspace, versión activa y ACL. Antes de responder se revalidan las fuentes elegidas si el run permaneció pausado o hubo cambios de permisos. Si se revoca acceso antes de enviar fragmentos al LLM se detiene ese paso. Una vez enviados a un proveedor no podemos «retirarlos» mediante un tombstone local.

Las cargas iniciales se limitan a TXT y Markdown. PDF, archivos de Office, OCR, ZIP y URLs remotas requieren una decisión posterior sobre parsers, consumo, aislamiento y red; no se habilitan por aceptar extensiones en un formulario.

Retirar es distinto de purgar. El tombstone impide nuevas recuperaciones; una política de retención decide eliminación física en archivos, índices antiguos y copias de seguridad. Los runs anteriores que contengan evidencia tienen acceso restringido y una retención explícita; no se promete borrado instantáneo de todas las copias.

## 6. Fallos distribuidos y garantías honestas

Un timeout o una cancelación no demuestran que Jenkins no haya aceptado el job. Si no conocemos el resultado, registramos UNKNOWN. Sin una garantía remota de deduplicación o consulta correlacionada concluyente no reintentamos automáticamente.

Un lease en PostgreSQL limita propiedad local; no revoca solicitudes en vuelo. Un worker reanudado después de perder el lease no actualiza estado con un fencing token viejo. Para una acción externa dudosa, el nuevo worker reconcilia antes de tomar cualquier otra decisión.

Un circuit breaker puede evitar saturar una dependencia caída, pero no deduplica acciones. Un retry con backoff no es un mecanismo de recuperación suficiente para escrituras. Cada control responde a una amenaza distinta.

## 7. Operación y secretos de desarrollo

Mantener secretos en variables o almacenamiento de secretos externo al código; versionar solo plantillas vacías. Ignorar logs, bases, archivos cargados y reportes locales; revisar también índice, historial y metadata de commits antes de publicar. Los documentos de diseño usan nombres y recursos ficticios, sin datos laborales o personales.

Compose inicial expone UI/API solo en loopback. Los puertos de bases, conectores y telemetría no deben quedar abiertos a la red por comodidad. TLS y autenticación de servicio serán requisitos al salir del entorno aislado de desarrollo. Desactivar un control para una prueba debe ser una configuración explícita de fixtures, no el comportamiento predeterminado del modo real.

## 8. Riesgos residuales y condición de conexión real

El LLM puede producir conclusiones incorrectas aun con referencias válidas. Los datos de un proveedor pueden estar incompletos. Un administrador de máquina o base puede superar controles del laboratorio. La instrumentación puede perder eventos; la disponibilidad total del sistema no está garantizada.

Antes de conectar escrituras reales se requieren S05–S09, pruebas de interrupción y contratos del proveedor verificados; credenciales de sandbox; política revisada; forma de pausar escrituras y runbook para UNKNOWN. Las herramientas prohibidas siguen ausentes o bloqueadas aunque las evals de prompt injection pasen.

Cumplimiento significa poder aportar evidencia de controles y retención definidos para un contexto concreto. Este laboratorio no presume estándares, certificaciones ni requisitos legales de un cliente desconocido.
