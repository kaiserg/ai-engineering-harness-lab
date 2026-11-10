# M3c TLS y emisor de identidad separado

[Índice](../guide.md) · [← Anterior](23-m3c-jira-por-streamable-http-e-identidad-de-servicio.md) · [Siguiente →](25-m3c-renovacion-tls-y-rotacion-de-firma-recuperables.md)


> Esta sección registra el primer despliegue TLS. La sección 25 agrega renovación explícita, firma con solapamiento, retiro de claves y mantenimiento recuperable.

### Paso 1 qué cambia y por qué

En el incremento anterior el worker firmaba sus propios JWT. Sirvió para aprender firma y audience, pero concentraba ejecución y emisión de identidad. Ahora:

```text
API → PostgreSQL ← Worker
                     │
                     ├─ HTTPS + client_id/client_secret → Emisor /token
                     │                                     │ clave privada Ed25519
                     │← JWT corto, audience=jira-mcp ───────┘
                     │
                     └─ HTTPS + Bearer JWT → Jira MCP → política → FakeJira
                                              │
                                              └─ HTTPS → Emisor /jwks (solo claves públicas)
```

**TLS e identidad cumplen funciones distintas.** TLS cifra y autentica al servidor mediante su certificado. El token identifica al servicio solicitante y delimita su alcance. Los permisos del recurso siguen decidiéndose en Jira. No usamos mTLS en este incremento: autenticamos al cliente con su credencial sobre TLS.

Este cambio cubre worker→emisor, worker→Jira y Jira→JWKS. La UI local sigue en HTTP/8002 y la conexión PostgreSQL mantiene su configuración anterior. GitHub/Jenkins siguen por stdio. El modelo continúa guionado y las fuentes son fixtures.

### Paso 2 una CA del laboratorio y certificados por servicio

`harness/service_tls.py` prepara:

- `tls-ca-key.pem`: clave privada de la autoridad certificadora; queda **solo en el host**, nunca en los contenedores.
- `tls-ca.pem`: certificado público que los clientes usan para confiar en esa autoridad.
- Certificado y clave TLS propios de Jira y del emisor.
- Una credencial aleatoria de cliente para el worker y una clave Ed25519 independiente para el emisor.

No instalamos la CA en macOS ni modificamos su trust store. Cada cliente construye un `SSLContext` explícito con esa CA, valida la cadena y el nombre del servidor y exige TLS 1.2 o posterior. No hay `verify=False`, redirecciones automáticas ni proxies heredados del entorno.

**SAN** significa Subject Alternative Name: los nombres/direcciones permitidos por el certificado. Jira incluye `jira-mcp`, el emisor `identity`; ambos incluyen `localhost` y `127.0.0.1` para las pruebas locales. Un certificado firmado por la CA correcta pero emitido para otro nombre sigue siendo rechazado. Incluimos también los identificadores de clave y usos X.509 que exige la validación estricta de OpenSSL/Python actual.

La CA dura un año y los certificados de servicio 30 días. `stack.py up` conserva los materiales existentes y verifica los pares/certificados de servicio; una pareja incompleta, vencida o inconsistente bloquea la preparación. No hay renovación ni rotación automática implementadas: antes de vencer habrá que planificar su renovación explícita. No borres `data/runtime/` como mecanismo de reparación: también contiene contraseñas y materiales que deben conservarse.

Las claves y certificados se guardan con modo `0600`, fuera de Git y del contexto de build. Los certificados son públicos, pero los tratamos uniformemente como material de runtime. Una CA local es apropiada para este laboratorio; en una empresa se integraría la PKI/gestión de certificados aprobada.

### Paso 3 un emisor independiente

`harness/identity_server.py` sirve HTTPS en el contenedor `identity`. Tiene dos endpoints relevantes:

| Endpoint | Función |
| --- | --- |
| `POST /token` | Autenticar al worker y emitir un access token de 60 segundos. |
| `GET /jwks` | Publicar únicamente la clave pública de verificación. |

El flujo se basa en [OAuth 2.0 client credentials, RFC 6749 §4.4](https://www.rfc-editor.org/rfc/rfc6749.html#section-4.4): un cliente confidencial se autentica para obtener su propio access token. Aquí el worker presenta `client_id=harness-worker` y un `client_secret` aleatorio mediante HTTP Basic **dentro de TLS**. El body es un formulario con `grant_type=client_credentials`, `scope=jira:read` y un selector de fixture didáctico opcional. La respuesta lleva `Cache-Control: no-store` y no emitimos refresh tokens: para una conexión nueva se solicita un access token nuevo.

La credencial del cliente **no es la clave de firma**. Si se compromete el worker, puede solicitar el alcance registrado mientras su credencial siga habilitada, pero no puede fabricar JWT con cualquier audience o claims por sí mismo.

El registro local del emisor fija:

- Servicio `harness-worker`.
- Scope `jira:read` y audience `jira-mcp`.
- Actor sintético `reader-demo` y workspace `workspace-alpha`.
- Issuer exacto `https://identity:8443`.

El cliente no puede elegir actor, workspace, audience ni roles. Se rechazan credenciales incorrectas, grants/scopes no admitidos, parámetros duplicados o campos extra. El selector `fixture` solo elige descripción benigna o maliciosa del ejercicio; no concede permisos. La comparación del secreto usa `compare_digest` y no exponemos su valor en mensajes de error.

**Alcance del emisor:** es una implementación educativa y acotada, no un servidor OAuth/OIDC completo ni un producto para desplegar tal cual en una empresa. No implementa discovery, registro dinámico, login humano, federación, revocación, rate limiting o rotación automática. Sirve para entender la frontera que luego ocuparía un proveedor administrado. No elegimos todavía un proveedor corporativo: esa elección depende del entorno de la organización.

### Paso 4 verificar JWKS y tokens en Jira

**JWKS** es un conjunto de claves públicas en JSON. El emisor publica una sola clave Ed25519 con `kid`, `alg=EdDSA` y `use=sig`, sin su componente privado. `kid` identifica la clave concreta.

Al iniciar Jira, `harness/issuer_client.py` descarga JWKS desde un endpoint configurado y permitido, por TLS con nuestra CA. Jira no toma URLs `jku`, `x5u` ni destinos de red desde el token. Si no puede obtener una clave válida, el servicio no arranca; Compose espera su salud antes de iniciar el worker.

Jira fija algoritmo, issuer y audience, exige el `kid` conocido, valida firma y vencimiento y después aplica la política de servicio/recurso del incremento anterior. El token antiguo emitido por el propio worker ya no es válido en el despliegue de Compose.

La clave JWKS queda en memoria hasta reiniciar Jira. Un `kid` desconocido se rechaza sin buscar destinos alternativos. No implementamos todavía refresh periódico ni superposición de claves durante rotación. Reiniciar el emisor conserva la misma clave porque está en runtime persistido del host.

### Paso 5 entregar a cada servicio solo su material

| Servicio | Material disponible |
| --- | --- |
| API | Su contraseña PostgreSQL. |
| Worker | Contraseña PostgreSQL, CA pública y secreto de cliente del emisor. |
| Jira MCP | CA pública, certificado y clave TLS propios; clave JWT pública descargada en memoria. |
| Emisor | Certificado/clave TLS propios, clave privada Ed25519 y secreto de cliente registrado; CA pública para salud. |
| Host | CA privada y copias persistidas de los materiales locales. |

La clave Ed25519 ya no se monta en el worker ni en Jira. Ningún contenedor recibe la CA privada. Jira/emisor no reciben credenciales PostgreSQL. Los procesos siguen abandonando root y sus copias privadas siguen en `tmpfs`; la imagen no incorpora secretos.

Los archivos de la antigua firma local permanecen ignorados en el host si ya existían. Compose no los monta. Conservamos el modo HTTP anterior para las pruebas del incremento 23, aislado de la configuración segura de Compose. `HARNESS_SERVICE_IDENTITY=issuer` exige HTTPS y no hace fallback a HTTP ni a firma local si falla el emisor.

### Paso 6 conservar controles y recuperación

No cambiamos el core, la cola ni el store. La obtención del token ocurre **dentro de la invocación reservada de la herramienta**, por lo que cuenta en su timeout y en el deadline global. No hay reintentos nuevos ni un presupuesto invisible para conseguir identidad.

Si la credencial es rechazada, el run falla con `MCP_UNAUTHENTICATED`. Si no se puede contactar/verificar al emisor, falla con `MCP_ISSUER_UNAVAILABLE`. Un error TLS del MCP también bloquea la herramienta. Estos fallos no se convierten en el resultado parcial permitido para Jenkins.

Se mantienen permisos antes de I/O, schema descubierto exacto, validación del servidor, normalización, límites, tiempo activo persistido, CAS, leases, heartbeat y fencing. HTTP continúa siendo stateless. La cancelación local sigue sin garantizar que una lectura remota haya dejado de ejecutarse.

El perfil HTTP `v4` mantiene su contrato y sus escenarios; endurecemos su configuración de transporte/autenticación en Compose. Un run HTTP ya pausado puede recuperarse usando el emisor actual sin reescribir su checkpoint. Los perfiles stdio anteriores siguen con stdio.

Al recuperar un run:

1. Si Jira ya quedó confirmado, reutilizamos su evidencia y no pedimos otro token para Jira.
2. Si la lectura quedó pendiente, mantenemos su `call_id`, cargamos la reserva huérfana y reservamos otro intento; pedimos una credencial nueva y repetimos la lectura.
3. El checkpoint no contiene access tokens, secretos de cliente, claves privadas ni sesiones remotas.

No cambiamos permisos basándonos en claims guardados por el modelo: tanto el host como Jira siguen revalidando el acceso actual.

### Paso 7 levantar y practicar

Desde la carpeta del proyecto:

```bash
uv sync --locked
uv run python scripts/stack.py up
uv run python scripts/stack.py status
```

La consola continúa en **http://127.0.0.1:8002**. Ahora deberían aparecer `api`, `worker`, `postgres`, `jira-mcp` e `identity`.

- Jira MCP: `https://127.0.0.1:8010/mcp/`.
- JWKS del emisor: `https://127.0.0.1:8011/jwks`.
- Endpoint de tokens: `https://127.0.0.1:8011/token`.

No necesitás abrir ni aprobar certificados en el navegador: el worker/Jira ya tienen la CA explícita. Que el navegador rechace nuestra CA privada es esperable; no instalamos confianza global para evitar ese aviso.

En la consola, ejecutá **Evidencia coincidente**. Debe terminar `VERIFIED`, con cuatro llamadas al modelo guionado y tres tools. Después pausá un run tras Jira, detené/levantá el stack y recuperalo:

```bash
uv run python scripts/stack.py down
uv run python scripts/stack.py up
```

La evidencia Jira confirmada se conserva sin repetir su consulta. No copies tokens al navegador ni al `.env`: el worker los obtiene con su secreto montado. `down` conserva historial, credenciales, claves y certificados.

### Paso 8 pruebas y límites operativos

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s integration_tests -v
uv run python -m unittest discover -s container_tests -v
```

**Verificación: 146 pruebas del laboratorio, 36 PostgreSQL y 7 Compose pasaron.**

Las pruebas agregadas levantan emisor y Jira reales por HTTPS con CA y claves temporales. Verifican confianza, nombre del certificado, prohibición de HTTP en modo issuer, autenticación/scopes, JWKS sin clave privada, rechazo de issuer/audience/kid/vencimiento incorrectos, fallo bloqueante del emisor y recuperación confirmada/pendiente. Las de Compose comprueban el material disponible por servicio y que el trabajo pendiente complete después de reiniciar el emisor.

Las suites PostgreSQL siguen probando interrupciones de proceso y fencing. Las de Compose reinician servicios y crean runs sintéticos: ejecutarlas sin otro worker que consuma la misma cola.

La credencial bearer puede reutilizarse durante su validez. No agregamos detección de replay ni revocación inmediata; el TTL corto limita esa ventana, pero no reemplaza esas capacidades. El secreto del cliente es estático y el emisor no tiene todavía un almacén administrativo de clientes. La siguiente evolución operativa puede cubrir rotación/revocación y trazas distribuidas antes de conectar proveedores DevOps reales.

| Término | Significado |
| --- | --- |
| CA / PKI | Autoridad y sistema que emiten/verifican certificados. |
| SAN | Nombre o dirección que el certificado autoriza al servidor a usar. |
| TLS | Canal cifrado con verificación de certificado y nombre del servidor. |
| Client credentials | Un servicio se autentica para obtener un token de acceso propio. |
| JWKS / kid | Conjunto de claves públicas y selector de una clave concreta. |
| Issuer | Emisor exacto que el receptor acepta como autoridad de tokens. |
| Trust store explícito | CA elegida por la app, sin cambiar la confianza global del equipo. |
