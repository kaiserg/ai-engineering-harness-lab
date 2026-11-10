# M3c renovación TLS y rotación de firma recuperables

[Índice](../guide.md) · [← Anterior](24-m3c-tls-y-emisor-de-identidad-separado.md) · [Siguiente →](26-m3c-renovacion-programada-y-rotacion-en-vivo.md)


> Esta sección conserva el procedimiento con parada coordinada. La sección 26 agrega el camino en vivo y el planificador; los comandos anteriores siguen disponibles como mantenimiento alternativo.

### Paso 1 distinguir qué estamos renovando

Hay dos tipos de claves independientes:

| Operación | Qué cambia | Qué conservamos |
| --- | --- | --- |
| Renovar certificados | Certificados y claves privadas TLS de Jira/emisor. | CA, clave de firma JWT, secreto de cliente, PostgreSQL y runs. |
| Rotar firma JWT | Clave Ed25519 activa y `kid`; pública anterior durante 90 segundos. | CA, certificados TLS, secreto del cliente y estado del harness. |
| Retirar firma anterior | Elimina la pública anterior del bundle después del plazo. | Clave activa y evidencia de runs. |

El certificado identifica al **servidor de una conexión TLS**. La clave Ed25519 firma **tokens de acceso**. Renovarlos por separado permite comprobar qué frontera cambió y aislar los fallos.

Este incremento cubre las claves TLS de servicios y la firma JWT. Conserva la CA y el secreto estático del cliente. Rotar una CA exige coordinar confianza entre emisores/receptores; rotar la credencial de cliente exige su propio despliegue. No los mezclamos con el ejercicio de hoy.

### Paso 2 generaciones completas y activación atómica

`harness/security_lifecycle.py` administra bundles bajo `data/runtime/security-generations/<id>/`. Cada generación incluye certificados, claves y configuración de las públicas anteriores. Los archivos privados tienen modo `0600`, y cada directorio de generación es privado. Ningún material entra en Git o en la imagen.

`security-current` es un enlace a la generación activa. Preparamos otra generación, comprobamos certificados/pares/claves públicas y sincronizamos sus archivos al disco. Solo después sustituimos el enlace con `os.replace`. Esto evita publicar un certificado nuevo junto a una clave de otro par si el proceso se interrumpe.

Las generaciones anteriores se conservan para diagnóstico/recuperación del despliegue. **Contienen claves privadas históricas**: siguen requiriendo protección y una política de retención. No hay borrado automático ni comando para volver a activar una clave retirada. No equivalen a una copia de seguridad independiente del equipo.

El primer `stack.py up` migra los materiales anteriores copiándolos a una generación; no cambia sus claves. PostgreSQL, contraseñas de base y checkpoints quedan fuera de esta operación. Los archivos iniciales pueden seguir en runtime, ignorados; Compose monta los de `security-current`.

### Paso 3 mantenimiento coordinado con breve interrupción

Elegimos una parada coordinada porque tenemos una instancia de cada servicio y estamos aprendiendo el ciclo de mantenimiento. No implementamos disponibilidad continua ni rolling deployment en este incremento.

`scripts/security.py` hace lo siguiente:

1. Toma un bloqueo de operación compartido con `stack.py up/down`, para impedir cambios concurrentes.
2. Registra la intención en `security-operation.json` sin secretos.
3. Envía SIGTERM al worker: deja de reclamar jobs y permite terminar el actual, con la gracia habitual de Compose. Después detiene Jira y el emisor.
4. Prepara/valida una generación y activa su enlace atómicamente.
5. Recrea emisor, Jira y worker, en ese orden, esperando salud.
6. Borra el registro pendiente solo cuando termina el despliegue.

La API y PostgreSQL permanecen operativos. La API puede seguir encolando trabajo durable; el worker lo consume al volver. Los comandos usan `--no-deps` para no recrear PostgreSQL y `--force-recreate` para volver a montar los archivos nuevos. Un simple `restart` podría conservar el archivo montado anteriormente por su inode.

Si el worker excede su gracia y muere, los controles existentes de lease/heartbeat/fencing recuperan el job al vencer el lease. El mantenimiento no fuerza estados en la base ni inventa evidencia. Operar la misma cola con otros workers requeriría drenarlos también: este ejercicio supone solo el worker de Compose.

### Paso 4 inspeccionar y renovar TLS

Desde la carpeta del proyecto:

```bash
uv run python scripts/security.py status
uv run python scripts/security.py renew-certificates
```

`status` muestra solo metadatos públicos: generación, `kid`, seriales, vencimientos, si corresponde renovar dentro de siete días y si hay una operación pendiente. Nunca muestra PEM, tokens, secretos ni claves.

`renew-certificates` emite certificados nuevos de 30 días y genera nuevas claves privadas para Jira/emisor, firmados por la misma CA. Conserva SAN, uso de servidor TLS e identificadores X.509. Puede renovar hojas vencidas porque no necesita contactar un servidor usando su certificado viejo; la CA debe seguir siendo válida y disponer de al menos 30 días de vigencia.

No instalamos la CA en el sistema, no relajamos la validación TLS y no cambiamos a HTTP durante el mantenimiento. Si la CA está próxima a vencer, el comando bloquea: su transición de confianza requiere otro procedimiento. No hay renovación programada automática; el comando explícito y la señal `renewal_due` permiten practicar primero la operación.

### Paso 5 rotar firma con solapamiento

```bash
uv run python scripts/security.py rotate-signing-key
```

Con el emisor detenido, el operador crea una nueva clave Ed25519. El emisor firma los tokens siguientes con el nuevo `kid`; la clave privada anterior no se monta en el emisor nuevo. JWKS publica la pública activa y, temporalmente, la anterior. `kid` selecciona la pública concreta dentro del conjunto, como contempla [RFC 7517 §4.5](https://www.rfc-editor.org/rfc/rfc7517.html#section-4.5).

Los tokens duran como máximo 60 segundos. Damos **90 segundos de solapamiento desde la preparación posterior a detener el emisor**: cubre la vigencia de los últimos emitidos y un margen operativo. No significa que extendamos `exp`: un token vencido sigue siendo rechazado.

La pública anterior lleva `lab_retire_at`, una extensión didáctica del JWK. Jira comprueba ese plazo en **cada request**, además de firma, issuer, audience, scope y vencimiento. Al llegar el plazo, rechaza la clave anterior aunque siga en memoria y aunque alguien con su vieja clave privada fabrique un JWT con un `exp` futuro. El emisor deja de publicar esa pública al vencer el plazo sin necesitar otro reinicio.

No descargamos claves desde URLs del token. Jira carga el conjunto configurado durante su recreación y exige `kid` conocido, algoritmo fijo y metadatos públicos válidos. El conjunto está limitado a dos claves y rechaza `kid` duplicados o componentes privados. Este mantenimiento coordinado evita necesitar todavía refresh periódico de JWKS.

Una segunda rotación se bloquea mientras la anterior esté en su ventana, para mantener acotados el conjunto y la operación. Los relojes de estos contenedores comparten el host; una plataforma distribuida necesita sincronización y márgenes definidos.

### Paso 6 retirar la pública anterior

Cuando hayan transcurrido los 90 segundos:

```bash
uv run python scripts/security.py status
uv run python scripts/security.py retire-signing-key
```

El retiro elimina la pública anterior del bundle activo y recrea los consumidores. No genera otra clave. Antes del plazo se rechaza la operación; consultar `status` permite ver `retire_at` como timestamp Unix. La verificación por plazo ya protegía a Jira: este comando completa la limpieza del conjunto activo.

Retirar una pública no borra las generaciones históricas del disco y no es una herramienta de respuesta a compromiso con revocación inmediata. Esta es una rotación rutinaria con ventana de compatibilidad. La revocación de emergencia y la retención segura requieren una política operativa adicional.

### Paso 7 recuperar un mantenimiento interrumpido

```bash
uv run python scripts/security.py status
uv run python scripts/security.py recover
```

El registro tiene fases `stopping`, `prepared` y `activated`:

- Si solo se había registrado intención/parada, `recover` completa esa misma acción.
- Si ya había una generación preparada, la valida y activa; no crea otra clave.
- Si ya estaba activa, recrea los servicios con esa misma generación y comprueba salud.

Un fallo de validación no cambia el puntero activo. Un fallo después de la activación deja el registro pendiente y no vuelve automáticamente a una clave anterior. Corregir el problema operativo y ejecutar `recover` permite terminar el mismo despliegue. Un destino de journal inválido se rechaza.

`stack.py up` bloquea si existe una operación pendiente y señala el uso de recuperación. Tampoco iniciamos otra renovación/rotación mientras quede el registro: así evitamos perder la intención original. No borres el journal manualmente para saltar ese control.

### Paso 8 comprobar que el run se recupera igual

La recuperación de infraestructura y la de runs son dos mecanismos distintos:

1. En la consola de **http://127.0.0.1:8002**, iniciá `Evidencia coincidente` con pausa después de Jira.
2. Ejecutá `renew-certificates` y luego `rotate-signing-key`.
3. Recuperá el run pausado desde la consola.
4. Debe conservar su primera evidencia y completar con cuatro decisiones del modelo y tres tools.

No guardamos certificados, JWT, secretos ni claves en el checkpoint. Si Jira ya estaba confirmado, se reutiliza esa evidencia; si seguía pendiente, se cobra otro intento y el worker obtiene un token de la clave activa. Permisos, schemas, presupuestos, tiempo activo, CAS, leases y fencing mantienen las reglas anteriores.

La prueba real de Compose también emite un token **antes** de rotar y comprueba que Jira todavía lo acepta dentro de su validez. Después comprueba un token nuevo y la recuperación del run. Así verificamos el solapamiento, no solo que el emisor se reinició.

### Paso 9 pruebas del ciclo completo

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s integration_tests -v
uv run python -m unittest discover -s container_tests -v
```

**Verificación: 155 pruebas del laboratorio, 36 PostgreSQL y 8 de Compose pasaron.** También verificamos el retiro real y el rechazo de un JWT nuevo firmado con la clave retirada.

Las pruebas nuevas cubren renovación preservando CA/identidad, publicación validada, bloqueo concurrente, firma anterior dentro/fuera del plazo, filtrado de JWKS, claves inválidas y recuperación del mantenimiento en cada fase. Las pruebas PostgreSQL conservan muerte real de procesos y fencing. Las de Compose cambian materiales del laboratorio, recrean servicios y crean un run sintético: no ejecutarlas con otro worker del host consumiendo la misma cola.

Después de ejecutar la suite de Compose, esperá el fin del solapamiento antes de volver a ejecutar su prueba de rotación. La prueba rechaza iniciar otra rotación si la ventana sigue vigente; no la salta ni espera escondiendo el motivo. Las pruebas de retiro usan reloj controlado para verificar el límite exacto sin hacer dormir la suite 90 segundos.

| Término | Significado aquí |
| --- | --- |
| Renovación | Emitir otro certificado con nueva vigencia y, aquí, nueva clave TLS. |
| Rotación | Cambiar la clave activa usada para firmar tokens. |
| Solapamiento | Aceptar dos públicas por un plazo limitado para terminar tokens anteriores. |
| Retiro | Dejar de aceptar una clave anterior y quitarla del conjunto activo. |
| Generación | Bundle completo validado antes de activarlo. |
| Publicación atómica | Cambiar el puntero activo sin exponer un bundle parcialmente escrito. |
| Journal de mantenimiento | Registro durable de intención y fase para completar una operación interrumpida. |
| Inode/mount | Un contenedor puede conservar el archivo viejo aunque el host sustituya su ruta. |

La siguiente evolución puede sumar métricas/alertas de vencimiento, rotación sin parada y trazas distribuidas. Hoy ya podemos renovar y rotar explícitamente con estado recuperable y sin cambiar las reglas del core.
