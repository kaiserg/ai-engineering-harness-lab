# M3c renovación programada y rotación en vivo

[Índice](../guide.md) · [← Anterior](25-m3c-renovacion-tls-y-rotacion-de-firma-recuperables.md) · [Siguiente →](27-m3c-opentelemetry-y-jaeger.md)


### Paso 1 definir qué significa sin parada

Hasta aquí recreábamos emisor/Jira/worker para cambiar los mounts. Ahora los servicios recargan materiales validados dentro del mismo proceso. Durante una actualización normal:

- El worker continúa reclamando jobs y ejecutando el core.
- Las conexiones TLS existentes pueden terminar usando su contexto anterior.
- Los nuevos handshakes seleccionan el contexto TLS renovado.
- Jira conoce una pública nueva antes de que el emisor la use para firmar.

Esto es recarga en vivo en una instancia, **no alta disponibilidad** ante caída del proceso, del host o de Docker. Si el emisor está caído o la configuración es inválida, los controles fallan de forma cerrada; no prometemos continuidad frente a cualquier fallo. El despliegue inicial del código de recarga puede recrear contenedores una vez. Las operaciones siguientes no ejecutan comandos Docker.

### Paso 2 entregar materiales sin ampliar permisos

El problema práctico es que las copias del bootstrap en tmpfs no ven los cambios del host, y dar todos los archivos privados del host al proceso rompería el aislamiento por rol.

`harness/live_materials.py` prepara un **bundle cifrado independiente** para emisor y Jira:

| Rol | Contenido de su bundle |
| --- | --- |
| Emisor | Su certificado/clave TLS, clave Ed25519 activa, públicas anteriores y conjunto anunciado. |
| Jira | Su certificado y clave TLS. Las públicas JWT siguen llegando desde JWKS por TLS. |
| Worker/API | No reciben estos bundles ni sus claves de entrega. |

Cada rol tiene una clave AES-GCM aleatoria de 32 bytes, guardada en runtime con modo `0600` y montada como secret solo en ese servicio. El archivo compartido contiene nonce, contador y ciphertext; puede leerse desde el UID 10001 porque no expone su contenido privado. AES-GCM autentica el contenido, y el nombre del rol se usa como dato autenticado adicional: un bundle de Jira no es válido para el emisor.

El contenedor monta **solo su directorio de entrega**, de solo lectura. No montamos el runtime completo, la CA privada ni el socket Docker. La CA privada y el controlador siguen en el host. Las claves de entrega no se regeneran silenciosamente si ya hay un bundle: si se pierden, hay que restaurarlas.

El proceso descifra a su tmpfs privado, verifica secuencia, cadena, vigencia, SAN y par certificado/clave, construye un contexto TLS nuevo y después sustituye su snapshot. Los archivos privados se crean directamente con modo `0600`. Una secuencia anterior o una configuración inválida no sustituye la última validada; salud reporta `LIVE_CONFIGURATION_REJECTED`. Hay copias históricas privadas en runtime/tmpfs: conservamos la necesidad de protección y retención del incremento anterior.

### Paso 3 recargar TLS para nuevas conexiones

`LiveMaterials` observa el archivo cifrado cada 200 ms. El listener sigue abierto y su callback de selección TLS elige el `SSLContext` actual para cada handshake. Las conexiones ya abiertas conservan su contexto; no cancelamos tools ni streams para cambiar un certificado.

Usamos la API de [SSLContext y su callback SNI de Python](https://docs.python.org/3/library/ssl.html#ssl.SSLContext.sni_callback). El callback también contempla clientes por IP, cuyo `server_name` es `None`. No dependemos de recargar Uvicorn, de cambiar permisos del proceso ni de desactivar la verificación.

La CA y los nombres permitidos siguen iguales. `renew-certificates` continúa generando certificados/privadas TLS nuevos para emisor y Jira. Las comprobaciones de salud devuelven serial y secuencia; el controlador espera que ambos servicios confirmen el material nuevo antes de activar el puntero durable del host.

### Paso 4 rotar firma en dos fases

`harness/live_security.py` usa un journal separado, `security-live-operation.json`:

```text
prepared → announcing → switching → switched → delivered → activar puntero
                │             │
                │             └─ el emisor firma con la nueva privada
                └─ JWKS publica la nueva pública; Jira confirma que ya la conoce
```

1. Preparamos una generación y guardamos sus identificadores `base`/`target`.
2. El emisor anuncia la pública nueva junto a la actual, mientras sigue firmando con la actual.
3. Esperamos que Jira confirme ese `kid` en su cache fresco.
4. Persistimos la intención de cambiar de firmante **antes** de publicar la nueva privada activa.
5. El emisor cambia su snapshot de firma; Jira observa también el plazo de retiro de la pública anterior.
6. Confirmamos el estado servido y activamos `security-current`.

El plazo de retiro se fija una sola vez en el journal. Los tokens duran 60 segundos y mantenemos el margen de 90 segundos. **Recuperar no reinicia ese plazo ni revive una clave retirada.** Si una interrupción supera toda la ventana, preservamos ese límite aunque una solicitud antigua pueda fallar; el worker obtiene tokens nuevos en las siguientes invocaciones autorizadas.

`kid` sigue siendo solo un selector dentro de las claves confiables, como describe [RFC 7517](https://www.rfc-editor.org/rfc/rfc7517.html#section-4.5). No aceptamos destinos `jku`/`x5u` del token y no ampliamos algoritmo, issuer, audience, scope, actor o workspace.

### Paso 5 refrescar JWKS con un límite de antigüedad

Jira refresca el endpoint fijo de JWKS por TLS cada segundo. No espera a un `kid` desconocido para hacer consultas de red arbitrarias. Salud expone los `kid` conocidos, plazos públicos y si el cache está fresco; esos son los acknowledgements del controlador.

La comprobación de salud responde `503` si el cache está vencido o hay una actualización local rechazada; Compose espera preparación real al arrancar. El rechazo de autenticación registra solo una razón genérica y la edad del cache, sin JWT ni claves.

Una consulta de metadatos fallida conserva el último conjunto validado. Cuando el cache cumple **15 segundos sin actualizarse**, Jira rechaza la autenticación hasta poder actualizarlo. Así no conserva una autoridad vieja indefinidamente durante un fallo del emisor. El plazo `lab_retire_at` se comprueba además en cada solicitud, incluso con cache fresco.

Este polling de claves es control de infraestructura: no añade llamadas al modelo ni reintentos de una herramienta. El host sigue reservando presupuesto antes de I/O y obteniendo su access token dentro del timeout de la invocación.

### Paso 6 comandos para operar en vivo

Desde la carpeta del proyecto:

```bash
uv run python scripts/security_live.py status
uv run python scripts/security_live.py renew-certificates
uv run python scripts/security_live.py rotate-signing-key
# Una vez finalizada la ventana de 90 segundos:
uv run python scripts/security_live.py retire-signing-key
```

No hay `stop`, `restart` ni `force-recreate` en este controlador. El retiro elimina la pública anterior por JWKS y espera que Jira observe el conjunto activo. La CA y el secreto de cliente permanecen iguales: sus transiciones requieren otro procedimiento.

El procedimiento anterior de `scripts/security.py` sigue disponible para mantenimiento coordinado. Actualiza también las entregas cifradas antes de recrear servicios. Ambos caminos comparten el bloqueo de infraestructura y se rechazan si el otro tiene una operación pendiente.

### Paso 7 programar el mantenimiento dentro del laboratorio

`scripts/stack.py up` inicia un **planificador local del proyecto** y `down` solicita su parada ordenada. Su estado está en `data/runtime/security-schedule.json`:

| Política inicial | Valor |
| --- | --- |
| Comprobar | Cada 60 segundos. |
| Renovar TLS | Cuando falta una semana o menos para vencer. |
| Rotar firma | Cada 30 días; una rotación manual en vivo reinicia esa referencia. |
| Retirar pública anterior | En una comprobación posterior al fin del solapamiento. |
| Recuperar transición pendiente | Tiene prioridad sobre iniciar otra. |
| Fallo operativo | Guardar error genérico y reintentar con backoff de 60 a 900 segundos. |

El planificador llama al mismo controlador en vivo. No tiene un segundo mecanismo de emisión, no usa la IA para decidir políticas y no llama Docker para cambiar claves. Persistimos `next_check`, última comprobación, acción y error, para que reiniciar el proceso no repita una operación inmediatamente.

```bash
uv run python scripts/security_live.py schedule-start
uv run python scripts/security_live.py schedule-stop
uv run python scripts/security_live.py schedule-once
uv run python scripts/security_live.py status
```

`start` evita duplicados mediante un bloqueo de archivo; `stop` escribe una solicitud y espera que termine la operación actual. No dependemos de inspeccionar procesos ni de enviar señales a un PID que pueda haberse reutilizado. Los locks de tick, daemon e infraestructura cumplen responsabilidades diferentes.

El proceso corre en el host mientras operamos este laboratorio. No instalamos un servicio del sistema ni una tarea de Codex. Si se apaga el equipo, se cierra Docker o se suspende macOS, no puede renovar durante ese período; al volver a iniciar el stack comprueba la política persistida. Si una hoja ya venció y no puede usarse el camino en vivo, el mantenimiento coordinado anterior permite renovarla mientras la CA siga válida. No hay avisos externos ni integración de alertas implementados.

### Paso 8 recuperación de una transición en vivo

```bash
uv run python scripts/security_live.py status
uv run python scripts/security_live.py recover
```

El planificador intenta esta misma recuperación antes de hacer otra operación. Si habíamos anunciado, termina la transición de esa generación; si habíamos comenzado el cambio de firmante, continúa hacia la nueva y nunca vuelve a anunciar la anterior como activa. Las fases e identificadores se validan antes de actuar.

El puntero del host se cambia después de confirmar los servicios. Por eso durante una transición parcial el estado servido puede ir por delante de ese puntero; el journal explica esa diferencia. `stack.py up` bloquea mientras haya un journal pendiente y requiere terminarlo, para no reiniciar desde una configuración anterior. No borres el journal para esquivar ese control.

El core, las tablas y los contratos de checkpoint no cambian. Una respuesta Jira confirmada se reutiliza. Una lectura pendiente conserva `call_id`, cobra otro intento y obtiene un token del firmante activo. Ningún bundle, privada, clave AES ni JWT se guarda en el run. Los límites, tiempo activo, permisos, schemas, normalización, CAS, leases, heartbeat y fencing siguen aplicándose.

### Paso 9 practicar y verificar continuidad

En **http://127.0.0.1:8002**, pausá un run después de Jira. Ejecutá una renovación y una rotación en vivo, y recuperalo. Debe conservar su evidencia y completar con cuatro decisiones y tres tools.

Las pruebas de continuidad hacen algo adicional:

- Mantienen una conexión HTTPS abierta durante la renovación y comprueban que es el mismo socket al terminar.
- Consultan tokens y MCP continuamente y registran errores.
- Comprueban que una conexión nueva recibe el serial renovado.
- Comparan IDs de contenedores antes/después: emisor, Jira y worker no se recrean.
- Verifican un JWT anterior todavía válido, un JWT nuevo y la recuperación del run pausado tras rotar.

La comprobación manual del planificador usó un umbral temporal de 31 días para hacer vencer la política de un certificado de 30 días, ejecutó una renovación real y restauró inmediatamente el umbral normal de siete días. No se adelantó el reloj del sistema ni se dejó un `next_check` futuro de semanas.

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s integration_tests -v
uv run python -m unittest discover -s container_tests -v
```

**Verificación final: 165 pruebas del laboratorio, 36 PostgreSQL y 9 Compose pasaron.** El planificador actualizado quedó activo, sin transición pendiente.

Las pruebas de unidades cubren tampering/cambio de rol/replay, snapshots válidos, anuncio antes de firma, recuperación sin extender retiro, timeout de acknowledgement, política por vencimiento, exclusión de ticks y backoff persistido. Las de contenedores actualizan materiales reales y crean runs sintéticos. Conservamos la restricción de esperar el solapamiento antes de repetir una prueba de rotación.

| Término | Significado aquí |
| --- | --- |
| Hot reload | Sustituir el snapshot validado sin reiniciar el proceso. |
| Anuncio previo | Distribuir una pública antes de usar su privada para firmar. |
| Acknowledgement | Confirmación del estado que un servicio ya está usando. |
| Antigüedad máxima del cache | Tiempo tras el cual dejamos de confiar en metadatos no renovados. |
| AES-GCM | Cifrado autenticado para entregar material privado por rol. |
| Backoff | Esperar progresivamente antes de reintentar un fallo operativo. |
| Planificador persistido | Política y próxima comprobación guardadas fuera de la memoria del proceso. |

Este incremento opera cambios rutinarios sin parada. Una CA nueva, revocación de emergencia, múltiples réplicas y alertas de producción siguen necesitando procedimientos específicos.
