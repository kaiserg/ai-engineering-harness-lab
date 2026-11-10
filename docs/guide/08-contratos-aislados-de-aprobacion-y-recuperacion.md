# Contratos aislados de aprobación y recuperación

[Índice](../guide.md) · [← Anterior](07-demostracion-permitida-y-denegada.md) · [Siguiente →](09-pruebas-y-limites-pendientes.md)


Estos tipos se prueban sin conectarlos al loop, para preparar M4 sin ejecutar escrituras.

### Proposal

Al crearla, copia argumentos mediante JSON canónico con orden estable y tipos restringidos. No admite floats u objetos arbitrarios en los argumentos; exige expiración con zona horaria. SHA-256 produce un identificador del payload, que incluye solicitante, workspace, herramienta, recurso, argumentos, revisión y vencimiento.

Aprobar requiere rol approver en el workspace, un actor diferente del solicitante, hash y versión esperados y vigencia temporal. Devuelve otra instancia con APPROVED y versión incrementada; una segunda aprobación se rechaza. Un hash no es una firma ni concede permisos por sí solo.

Todavía no hay repositorio transaccional de propuestas, consumo de aprobaciones, revalidación remota o vínculo completo con versiones de configuración/política. Las pruebas de duplicación son del objeto de contrato, no una garantía multiworker.

### Lease

Comprueba propietario, fencing token y expiración. La prueba demuestra que un token viejo o un lease vencido no pasan esa comprobación. No hay heartbeat, reclamo de jobs ni reaper reales.

### Operation

Modela el punto de corte:

```text
PREPARED + interrupción → PREPARED
DISPATCHING + interrupción → UNKNOWN
```

Conserva `operation_id`. UNKNOWN no puede despacharse otra vez directamente. `recovery_strategy` selecciona RETRY_SAME_KEY solo si un adaptador confiable declara deduplicación garantizada; LOOKUP si existe consulta fiable; de lo contrario MANUAL_RESOLUTION. Es una decisión de contrato, no una consulta remota ni una prueba de que Jenkins ofrezca esas garantías.

No usamos sleeps ni procesos caídos para esas pruebas puras: representamos estados de entrada y salida. La recuperación durable y las caídas reales se probarán con persistencia y adaptadores posteriores.
