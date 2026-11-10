# M4b ledger y despacho Jenkins simulado

[Índice](../guide.md) · [← Anterior](31-m4a-propuestas-y-aprobaciones-persistidas.md) · [Siguiente →](33-m4c-despacho-y-consulta-del-efecto-por-mcp.md)


### Paso 1 distinguir propuesta, operación y efecto

Una propuesta describe lo que alguien revisó. Una operación registra un intento autorizado de producir un efecto. Una referencia de cola confirma aceptación de la solicitud; **no confirma que el build terminó ni que CI pasó**.

Agregamos `harness/operations.py`, `harness/jenkins_simulator.py`, `scripts/operations.py` y la migración `002_operations.sql`. El módulo de propuestas migra de versión 1 a 2; las tablas/versiones de runs y los workers de lectura no cambian.

El proveedor de escritura todavía es ficticio y directo desde Python: no extendimos el MCP Jenkins Go con una escritura ni conectamos Jenkins real. Su SQLite independiente permite estudiar primero el fallo entre dos sistemas persistentes. No hay llamadas a modelos ni escrituras DevOps reales en este recorrido.

### Paso 2 consumir la aprobación de forma atómica

Partí de una propuesta aprobada y vigente de la sección 31. Si venció, prepará evidencia y creá/aprobá otra propuesta; repetir su key no extiende la anterior.

```bash
uv run python scripts/operations.py prepare PROPOSAL_ID
```

El comando verifica permisos actuales del operador y del aprobador en el directorio de fixtures, separación de actores, workspace, vencimiento, versión de política/configuración y conservación de la evidencia del run. La revisión se compara además con el head actual del simulador.

En una sola transacción PostgreSQL:

1. Bloquea la propuesta.
2. Crea un `operation_id` asignado por el host, estado PREPARED, versión 0.
3. Cambia la propuesta de APPROVED a CONSUMED conservando el payload y la decisión.
4. Registra el evento PREPARED.

La relación UNIQUE con `proposal_id` impide consumir dos veces la misma aprobación. Repetir `prepare` devuelve la operación existente, sin crear otro UUID. La consulta de un recurso existente no concede otro despacho si ya está incierto o terminado.

Si el head ya cambió, persistimos INVALIDATED y devolvemos STALE_PRECONDITION sin crear una operación. Hace falta nueva evidencia/propuesta/aprobación; no editamos la autorización antigua.

### Paso 3 registrar intención antes de enviar

Copiá el `operation_id` de PREPARED:

```bash
uv run python scripts/operations.py show OPERATION_ID
uv run python scripts/operations.py dispatch OPERATION_ID
```

Antes del efecto, PostgreSQL confirma DISPATCHING, aumenta la versión, registra el evento y asigna un lease de diez segundos. Esa es la frontera durable: después de ese commit, una interrupción puede esconder un efecto remoto.

Solo hay un envío por operación. El claim usa bloqueo transaccional por workspace/recurso; otra operación DISPATCHING o UNKNOWN bloquea nuevas escrituras sobre ese recurso. El estado/versión impide que dos procesos despachen la misma operación. No mantenemos la transacción PostgreSQL abierta durante la solicitud al proveedor.

La operación tiene su propio estado de un único intento: no resetea ni reutiliza el presupuesto ya gastado por el run de lectura. Los límites, tiempo activo, CAS, leases, heartbeat y fencing de los workers existentes permanecen iguales. Este ejecutor local no es todavía otro worker con heartbeat: hace una solicitud acotada a SQLite, con espera de lock de tres segundos, y no permite reclamar el envío otra vez al vencer el lease.

### Paso 4 comprobar la precondición en el proveedor

El simulador guarda sus datos en `data/runtime/jenkins-write-simulator.sqlite3`, privado e ignorado por Git. La aceptación se confirma en una transacción distinta del ledger. La solicitud contiene el mismo operation ID, identidad, hash del payload y argumentos aprobados.

El simulador verifica recursos y revisión dentro de su transacción, antes de insertar la solicitud. Recibe también el menor vencimiento entre aprobación y lease: usa su reloj UTC para rechazar una solicitud nueva que llegue después, incluso si el proceso se suspendió tras el claim. DISPATCH_EXPIRED garantiza ausencia de efecto en este simulador. Así cubrimos también el cambio de head entre preparar la operación y enviar. Un rechazo STALE_PRECONDITION significa que este proveedor garantiza que no produjo efecto; registramos NOT_APPLIED.

También implementa deduplicación: el mismo operation ID con los mismos datos devuelve la referencia existente; datos diferentes con ese ID dan conflicto. Es una capacidad explícita de este simulador. No suponemos que Jenkins real ofrezca el mismo contrato de idempotencia, búsqueda o comprobación atómica del head.

Para practicar el cambio de revisión, con una operación PREPARED que aún no enviaste:

```bash
uv run python scripts/operations.py fixture-head --revision revision-B
uv run python scripts/operations.py dispatch OPERATION_ID
# Restaurar solamente este fixture después del ejercicio:
uv run python scripts/operations.py fixture-head --revision revision-A
```

`fixture-head` cambia únicamente el estado sintético del proveedor de escritura; no modifica GitHub ni los fixtures de lectura MCP. El comando sin `--revision` consulta el valor actual. La solicitud siempre apunta a una revisión concreta, no a «la última» al ejecutar el build.

### Paso 5 interpretar el ledger

```text
PREPARED → DISPATCHING → SUCCEEDED
    │           ├────→ NOT_APPLIED
    │           └────→ UNKNOWN → SUCCEEDED (solo con evidencia del proveedor)
    └─────────→ NOT_APPLIED (precondición local rechazada antes del envío)
```

- **SUCCEEDED:** el proveedor confirmó aceptación y guardamos su referencia `queue:operation_id`.
- **NOT_APPLIED:** sabemos que esta operación no produjo el efecto; conservamos el motivo. No la reintentamos automáticamente.
- **UNKNOWN:** no tenemos evidencia suficiente del resultado. No equivale a FAILED y no habilita otro UUID o envío.

Una respuesta inválida o excepción no conocida se trata conservadoramente como UNKNOWN; no guardamos detalles crudos. Los rechazos explícitos del simulador por revisión obsoleta o límite de aceptación vencido ofrecen la garantía de «no aplicado» tras entrar al proveedor.

Repetir `dispatch` de SUCCEEDED/NOT_APPLIED devuelve el resultado persistido. En DISPATCHING/UNKNOWN se rechaza con UNSAFE_RECOVERY: corresponde consultar/reconciliar, no enviar otra vez.

`show` incluye el historial `operation_events`, con versión, estado, actor y hora. Estado y evento se confirman juntos. Un trigger protege identidad de operación/propuesta y transiciones; la confirmación local usa versión esperada para impedir que un intento obsoleto sobrescriba una resolución posterior. El administrador de PostgreSQL permanece dentro de la frontera de confianza.

### Paso 6 simular la respuesta perdida después del efecto

Para este ejercicio usá **otra propuesta recién aprobada y otra operación PREPARED**. Una operación que ya terminó SUCCEEDED devuelve su resultado y no vuelve a ejecutar el punto de fallo.

```bash
uv run python scripts/operations.py dispatch OPERATION_ID --interrupt after-send
```

El simulador confirma su solicitud; después el comando se interrumpe antes de guardar el éxito local y termina con código 2. No mata otros procesos. La misma ventana puede aparecer ante una muerte real del proceso después del commit del proveedor.

```bash
uv run python scripts/operations.py show OPERATION_ID
```

Verás DISPATCHING. Durante los diez segundos de lease, `reconcile` devuelve OPERATION_BUSY para no resolver prematuramente un intento todavía activo. Esperá aproximadamente once segundos desde el envío y ejecutá:

```bash
uv run python scripts/operations.py reconcile OPERATION_ID
uv run python scripts/operations.py show OPERATION_ID
```

La recuperación hace esto:

1. Convierte el DISPATCHING vencido en UNKNOWN y confirma ese estado.
2. Consulta al proveedor por el mismo operation ID y hash, sin reenviar la solicitud.
3. Si obtiene la referencia válida, registra SUCCEEDED con otra versión/evento.
4. Repetir la consulta o `dispatch` después de SUCCEEDED devuelve lo que ya se guardó.

El proveedor debe conservar su base para poder encontrar ese efecto; borrar el archivo del simulador no demuestra que una operación histórica no ocurrió. Como en cualquier recuperación, hay que conservar los estados de ambos sistemas, no solo el checkpoint local.

### Paso 7 mantener la incertidumbre si no hay respuesta fiable

El modo `--interrupt before-send` deja DISPATCHING antes de llamar al proveedor. Está cubierto en las pruebas; al vencer el lease, la búsqueda no encuentra solicitud y conservamos UNKNOWN. **No reenviamos automáticamente por una búsqueda vacía.**

Por ahora ese estado bloquea otras escrituras al mismo recurso y no hay comando de resolución manual. No uses `before-send` en el entorno que querés seguir operando esperando que se limpie solo; el recorrido recomendado para practicar es `after-send`, que sí encuentra el efecto. Añadir resolución manual y otros contratos de proveedor será un incremento posterior.

Una revisión o aprobación que venció después del envío no impide investigar el efecto ya producido. La consulta mantiene autorización actual de acceso, pero no aplica una aprobación vencida para justificar otro despacho. Reconciliar no extiende vigencia ni vuelve a consumir la propuesta.

### Paso 8 conservar reintentos e identidades de M4a

Los comandos siguen usando fixtures locales controlados para operador/aprobador; no agregamos roles enviados por el navegador. El directorio de principals del ejecutor permite revalidar los permisos de ambos al preparar/despachar. La autenticación de usuarios reales y la pantalla/API de operaciones permanecen pendientes.

Si repetís la decisión APPROVED original después de CONSUMED o INVALIDATED, el store devuelve el estado actual para el mismo aprobador/hash/versión original. No restaura APPROVED ni cambia la fecha de vencimiento. Así un consumidor posterior no rompe la recuperación de una respuesta de aprobación perdida.

Las propuestas APPROVED/CONSUMED pueden consultarse como historia; solamente las comprobaciones del ejecutor habilitan el envío. El modelo de lectura sigue sin tener una herramienta de escritura registrada: este recorrido de operación nace de la CLI y de una aprobación persistida.

### Paso 9 verificar y continuar

```bash
uv run python -m unittest discover -s integration_tests -p test_operations.py -v
uv run python -m unittest discover -s integration_tests -v
uv run python -m unittest discover -s tests -v
uv run python scripts/evaluate.py
```

Las pruebas nuevas verifican consumo único, preservación del run, revisión cambiada antes de consumir y antes de aceptar, aprobación vencida, permisos revocados, carreras de despacho, deduplicación del proveedor, aislamiento, payload/transiciones SQL protegidos, respuesta perdida, lease activo/vencido, consulta sin reenvío, confirmación obsoleta y bloqueo de un segundo efecto mientras el primero sea incierto.

Además ejecutamos los comandos reales en procesos separados con el fallo after-send. El ledger mostró PREPARED → DISPATCHING → UNKNOWN → SUCCEEDED; el proveedor conservó exactamente una solicitud para ese operation ID, incluso después de repetir `dispatch`.

Este incremento no cierra toda M4: faltan integración de la acción con el MCP/worker/API/frontend, resolución manual y ampliación de observabilidad/evals para escrituras. Primero fijamos las garantías entre intención, aprobación, commit remoto e incertidumbre; después conectaremos el transporte sin delegarle esas decisiones.

Validación M4b: **190 pruebas locales, 66 PostgreSQL (13 de operaciones) y 9/9 evaluaciones deterministas de lectura aprobadas**. El ejercicio CLI after-send confirmó una sola solicitud en el proveedor y el mismo operation ID al recuperar/repetir. La última consulta dejó cero operaciones DISPATCHING/UNKNOWN de prueba en el schema de la app. Gitleaks no encontró secretos en los archivos revisados; proveedor/reporte siguen ignorados por Git.
