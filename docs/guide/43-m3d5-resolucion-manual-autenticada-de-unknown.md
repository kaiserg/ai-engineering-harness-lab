# M3d5 resolución manual autenticada de UNKNOWN

[Índice](../guide.md) · [← Anterior](42-m3d4-cola-y-worker-de-escrituras-autenticadas.md) · [Siguiente →](44-cierre-autenticado-evaluaciones-y-checkpoint-antes-de-m5.md)


### Paso 1 investigar sin convertir ausencia en permiso

M3d4 deja UNKNOWN/NEEDS_REVIEW cuando no puede demostrar el efecto. Este incremento conecta el cierre revisado al modo de usuarios 8005. El solicitante prepara evidencia y otro actor approver la confirma; el servidor no ofrece un botón libre para forzar SUCCEEDED o NOT_APPLIED.

La regla sigue siendo: una búsqueda vacía no prueba que una solicitud tardía jamás pueda aceptarse. El simulador primero guarda un cierre durable del ID, que devuelve un recibo existente o impide aceptar uno nuevo. Solo después una revisión válida puede concluir NOT_APPLIED.

Reutilizamos `Resolutions`, sus hashes/versiones y los triggers SQL anteriores. No cambiamos el contrato del proveedor ni agregamos una ruta de reenvío de UNKNOWN.

### Paso 2 limitar la capacidad de cierre

`harness/authenticated_resolutions.py` construye un contexto de servicio para la operación, después de autorizar la vista de su propuesta con la identidad de la petición. El scope conserva al dueño original y al workspace; conocer un UUID no concede acceso.

| Acción | Autoridad y condición |
| --- | --- |
| Preparar cierre | Dueño operator autenticado, grants actuales y trabajo aparcado. |
| Consultar evidencia | Dueño o approver autorizado para la propuesta/workspace. |
| Confirmar | Otro actor approver autenticado con permisos actuales y hash/versión exactos. |
| Concluir sin efecto | Evidencia de cierre durable sin recibo, confirmada en el mismo ledger. |
| Reenviar UNKNOWN | No permitido. |

La capacidad de cierre no permite request ni lookup. `ClosureProvider` rechaza ambos y solo permite `close_request` con un autorizador vinculado a la petición. `ManualResolutionOperations` también rechaza dispatch/reconcile. Así el guard de revisión no se convierte en una forma de saltar los reclamos del worker.

El worker normal no obtiene por defecto capacidad para cerrar IDs: requiere una autorización explícita para esa acción. Su seguimiento automático por recibo sigue siendo el mecanismo de M3d4, separado de esta intervención humana.

### Paso 3 exigir un trabajo aparcado y preservar los locks

La revisión usa un guard que bloquea primero el job y luego la operación, con el mismo orden que el worker. Solo admite NEEDS_REVIEW, o DONE para consultar/repetir una confirmación terminal. Rechaza PENDING/RUNNING; no arrebata un lease al consumidor, aunque un job RUNNING ya esté vencido.

Si los cinco reclamos se agotaron y quedó DISPATCHING con lease vencido y job NEEDS_REVIEW, preparar investigación puede pasar el ledger a UNKNOWN antes de cerrar. Usa el reloj de PostgreSQL, conserva el contador y no llama request. Esto evita necesitar un reset manual de intentos para investigar ese caso.

Si el ledger aún tiene lease vigente, se devuelve OPERATION_BUSY. Una operación PREPARED o terminal no se transforma arbitrariamente en una incertidumbre nueva para permitir el cierre.

### Paso 4 preparar evidencia, no el resultado final

El solicitante debe conservar su rol operator y los grants del recurso al iniciar el cierre. La API los revalida contra el directorio, sin confiar en el body ni en roles antiguos de la sesión. Antes y después de la llamada MCP se comprueban otra vez los permisos del actor que la autorizó.

El proveedor guarda un cierre asociado al mismo operation ID, dueño/workspace y hash de la propuesta. La resolución persistida añade versión del ledger, evidencia, identidad del proveedor, marca temporal y conclusión propuesta:

```text
recibo existente → propuesta de conclusión SUCCEEDED
cierre sin recibo → propuesta de conclusión NOT_APPLIED
```

Preparar devuelve PENDING_REVIEW y mantiene UNKNOWN. No da por aprobada esa conclusión. Repetir prepare devuelve la misma evidencia/hash sin duplicarla ni alterar su contenido.

El cierre remoto no forma una transacción distribuida con PostgreSQL. Si el proveedor cerró y después falla la persistencia local o se retiran permisos, el cierre permanece. Es seguro volver a consultar/preparar ese mismo cierre; no se reabre el ID ni se crea otro UUID para escapar de la incertidumbre.

### Paso 5 confirmar con un revisor actual

El revisor usa su propia credencial y el hash/versión de la **resolución revisada**, no los de la aprobación original. El servidor comprueba actor distinto al solicitante, workspace, rol approver y grants. Un actor con ambos roles tampoco puede revisarse a sí mismo.

`Resolutions` ahora admite un resolver de reviewer opcional. En el modo autenticado lo consulta antes de tomar los locks, después de adquirirlos y antes de confirmar. Vuelve a comprobar además la evidencia del proveedor mediante el mismo cierre idempotente. Si se retiran permisos durante esa comprobación, se rechaza la confirmación.

Para finalizar una revisión ya preparada no se exige que el solicitante siga habilitado: la autoridad actual corresponde al revisor y al servicio que verifica el hecho remoto. Deshabilitar al dueño o revocar su sesión no borra el cierre ni vuelve a habilitar un envío.

Si el dueño ya estaba deshabilitado antes de preparar, esta etapa no incorpora un rol administrativo que prepare en su nombre. Se requiere restaurar explícitamente los permisos adecuados o una futura capacidad operativa de investigación; no se omiten controles ni se infiere ausencia por la revocación.

### Paso 6 confirmar resolución, ledger y job juntos

La confirmación local hace en una transacción PostgreSQL:

1. Guardar CONFIRMED, reviewer y timestamp en la resolución.
2. Cambiar UNKNOWN a SUCCEEDED o NOT_APPLIED con versión esperada y evidencia comprobada.
3. Marcar el job DONE, guardar su resultado e incrementar el fencing token.

El trigger SQL sigue impidiendo UNKNOWN → NOT_APPLIED sin resolución confirmada que corresponda a la versión anterior del ledger. El evento del ledger identifica al usuario revisor; no aparenta otra ejecución del worker.

El callback `on_confirm` permite terminar el job dentro de esa misma transacción. Si falla, se revierten las tres actualizaciones locales. El proveedor sigue cerrado y una confirmación posterior puede repetir la verificación segura. No queda una resolución confirmada con un job a medio terminar por esa ventana.

Dos confirmaciones concurrentes se serializan por el job. Un reintento idéntico del mismo revisor devuelve el resultado persistido, sin otro evento ni otro incremento del token. Un hash/versión incorrectos o un intento de cambiar el revisor ya confirmado producen conflicto.

### Paso 7 producir el caso UNKNOWN para practicar

Usá la API 8005 y los workers de lectura/escritura autenticados de las secciones 40–42. Emití sesiones operator y approver, creá un run nuevo, proponé, aprobá y prepará una operación vigente.

Admitila con el fallo antes del envío:

```bash
uv run python scripts/user_proposals.py --session SESION_OPERADOR dispatch UUID_DE_OPERACION --interrupt before-send
uv run python scripts/user_proposals.py --session SESION_OPERADOR operation UUID_DE_OPERACION
```

Esperá a que el worker haga la consulta tras el vencimiento y muestre UNKNOWN con write_job NEEDS_REVIEW. No alcanza con ver DISPATCHING/PENDING durante la espera: el contexto manual no toma un job activo del consumidor.

La prueba de reclamos agotados cubre el otro caso elegible: DISPATCHING vencido con NEEDS_REVIEW por llegar a cinco reclamos. No reinicia el contador para cerrar.

### Paso 8 preparar y revisar el cierre

Con la sesión del dueño:

```bash
uv run python scripts/user_proposals.py --session SESION_OPERADOR prepare-resolution UUID_DE_OPERACION
```

La salida contiene `resolution`, con payload, payload_hash, version y status. Después, con la otra sesión:

```bash
uv run python scripts/user_proposals.py --session SESION_APROBADOR resolution UUID_DE_OPERACION
```

Revisá operation ID/versión, workspace, solicitante, hash de propuesta, recibo o ausencia y el cierre del proveedor. Copiá el hash y la versión de esta resolución. El hash no es el de la propuesta inicial: ahora se está revisando otra evidencia y otra decisión.

La consulta/revisión de la propuesta y su bandeja también muestran la resolución asociada a quienes ya tienen autorización para verla. No concede acceso al historial completo de runs de otro actor.

### Paso 9 confirmar sin enviar otra solicitud

```bash
uv run python scripts/user_proposals.py --session SESION_APROBADOR confirm-resolution UUID_DE_OPERACION --payload-hash HASH_DE_LA_RESOLUCION --version 0
```

Usá la versión devuelta al revisar; para una resolución recién preparada es cero. No se envía `reviewed_by`, evidencia ni conclusión desde el cliente. Solo hash y versión; el servidor determina identidad y resultado.

Para el caso sin efecto deben quedar:

```text
resolution: CONFIRMED
operation: NOT_APPLIED · MANUAL_PROVIDER_CLOSURE
write_job: DONE · NOT_APPLIED
proposal: CONSUMED
```

Con un recibo aceptado previamente, queda SUCCEEDED sin otro request. Esa aprobación consumida no vuelve a estar disponible, y el ID cerrado rechaza un envío tardío.

Los rechazos de auto-revisión/permisos producen 403, otro workspace recibe 404, hash/versión incorrectos dan 409 y campos añadidos como `conclusion` dan 422. El cliente devuelve código 1 para esos rechazos. Una credencial revocada devuelve 401 antes de llegar a esta lógica.

### Paso 10 evidencia y pruebas

El ejercicio HTTP real dejó UNKNOWN/NEEDS_REVIEW mediante before-send. Preparó el cierre, rechazó auto-revisión, otro workspace, hash incorrecto y un revisor cuyos permisos se retiraron. La confirmación se completó con otro actor después de deshabilitar al solicitante, y el reintento devolvió el mismo resultado.

Se comprobó NOT_APPLIED/DONE, un incremento del fencing token, evento final approver-demo y cero solicitudes aceptadas para ese ID. Una sonda adversarial separada intentó enviar al ID cerrado y recibió OPERATION_CLOSED; no forma parte de la recuperación del worker. La lectura original conservó cuatro decisiones y tres herramientas. Se restauraron los grants y se revocaron las credenciales del ejercicio.

Las integraciones cubren ausencia y recibo existente, cierre que impide un envío tardío, dos actores/roles, aislamiento, rollback de confirmación, concurrencia, permisos retirados durante MCP, rechazo de jobs activos y reclamos agotados. La prueba de rollback fuerza un fallo al finalizar el job: resolución PENDING_REVIEW, ledger UNKNOWN y job NEEDS_REVIEW quedan intactos hasta un reintento autorizado.

```bash
uv run python -m unittest discover -s tests -p 'test_authenticated_resolutions.py' -v
uv run python -m unittest discover -s integration_tests -p 'test_authenticated_resolutions.py' -v
uv run python scripts/evaluate.py
uv run python scripts/evaluate_writes.py
```

Una ejecución completa mostró una prueba anterior que suponía lookup en el siguiente reclamo inmediato; aislada pasó. La prueba ahora admite espera sin reclamo y el aplazamiento permitido por OPERATION_BUSY, con plazo acotado y sin resetear intentos. Sigue exigiendo exactamente un lookup, ningún reenvío, un efecto y resultado terminal correcto; cualquier otro error falla. No se añadieron reintentos ocultos al cliente MCP ni se modificó el comportamiento de producción por esa prueba.

### Paso 11 cierre de esta parte y preparación de M5

Con esto, el recorrido autenticado ya cubre propuesta, aprobación, cola, envío, recuperación y cierre manual revisado para el proveedor ficticio. La siguiente parte será verificar el recorrido autenticado como conjunto y preparar su checkpoint; después pasaremos a **M5: conocimiento/RAG**.

Login web/OIDC, refinamiento del frontend, nuevos exporters y roles administrativos no se agregan como condiciones nuevas antes de M5. Siguen registrados en el alcance pendiente. Las garantías de cierre/deduplicación corresponden al simulador y no se atribuyen automáticamente a Jenkins real.

El paso a paso permanece en esta guía del proyecto. La evidencia de ejecución queda privada en `data/runtime/m3d5-manual-resolution-verification.json`, ignorada por Git; no se crea una copia de la documentación en Documents/Codex.

Validación M3d5: **228 pruebas locales, 146 PostgreSQL, 8/8 evals de escritura y 9/9 de lectura aprobadas**. Diez integraciones nuevas y tres pruebas de frontera/cliente cubren cierre, revisor actual, aislamiento, rollback atómico, concurrencia y reclamos agotados. La suite completa final pasó tras hacer explícitos los aplazamientos permitidos de una prueba de recuperación anterior, sin modificar el ejecutor de producción. La verificación HTTP terminó CONFIRMED/NOT_APPLIED/DONE con cero solicitudes aceptadas, cierre que rechazó un intento tardío, un incremento de fencing y auditoría del revisor; el solicitante estaba deshabilitado al confirmar. Se restauraron grants y revocaron las sesiones del ejercicio. No quedaron operaciones inciertas ni jobs de escritura activos en los schemas principales. Compose/rotación no se repitieron. Gitleaks no encontró secretos en los archivos publicables; runtime y credenciales permanecen ignorados.
