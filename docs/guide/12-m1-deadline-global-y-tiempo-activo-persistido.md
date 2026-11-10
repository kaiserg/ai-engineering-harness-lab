# M1 deadline global y tiempo activo persistido

[Índice](../guide.md) · [← Anterior](11-m1-guardar-y-recuperar-un-run-con-sqlite.md) · [Siguiente →](13-m2-primera-herramienta-por-mcp.md)


### Paso 1 separar timeout individual y presupuesto global

Antes limitábamos cada llamada al modelo o herramienta. Eso no limita la suma: cuatro llamadas de 25 segundos podrían ser válidas individualmente y consumir más de un minuto.

`Limits.active_timeout` añade un presupuesto total de tiempo activo, inicialmente **120 segundos**, conservado dentro de `limits_snapshot`. Cada nueva llamada recibe:

```text
allowance = min(timeout_individual, tiempo_activo_restante)
```

En esta etapa «activo» significa tiempo de la sesión de ejecución, incluidas esperas de modelo/red, herramientas y trabajo local medido entre checkpoints. No significa solo tiempo de CPU. El tiempo que el proceso permanece detenido entre dos comandos no se calcula como consumo activo.

Todavía no existen aprobaciones humanas integradas al loop. Cuando lleguen, ese estado de espera deberá quedar fuera de las sesiones activas; no se debe dejar un timer corriendo durante una aprobación de treinta minutos.

### Paso 2 medir con un reloj monotónico

`harness/timing.py` contiene `ActiveBudget`. Usa `time.monotonic` mediante una dependencia inyectada. Mide diferencias dentro del proceso, sin depender de la hora de calendario ni de ajustes del reloj del sistema.

```text
nuevo_acumulado = acumulado_guardado + intervalo_monotónico_de_esta_sesión
```

No guardamos el valor absoluto del reloj monotónico para compararlo con otro proceso: su origen no es un timestamp UTC portable. Guardamos segundos acumulados y empezamos otro reloj local al ejecutar de nuevo.

Las pruebas pueden inyectar un reloj controlado y avanzar, por ejemplo, 0.4 segundos por decisión, sin hacer sleeps largos. También hay una prueba real de cancelación por timeout para verificar la integración con asyncio.

### Paso 3 guardar tiempo y distinguir reservas

El payload de Run pasa a schema 2 y agrega:

| Campo del CLI/store | Significado |
| --- | --- |
| `active_seconds` | Tiempo ya contabilizado: observado más cargos conservadores de recuperación. |
| `pending_time_seconds` | Reserva de una llamada que todavía no confirmó su finalización. |
| `recovered_time_seconds` | Subtotal de `active_seconds` imputado por intentos interrumpidos de duración desconocida. No sumarlo otra vez. |
| `timing_version` | Versión del contrato de medición; 1 para los runs nuevos. |
| `active_limit_seconds` | Límite global del snapshot. |
| `active_remaining_seconds` | Disponible sin comprometer: máximo de cero y límite menos activo menos reserva pendiente. |

En una llamada normal, antes de esperar guardamos una reserva de hasta su timeout o el tiempo global restante. Cuando vuelve, registramos el intervalo observado y liberamos la reserva: no cobramos el timeout completo a una llamada que terminó rápido.

Ejemplo con límite de 10 segundos: si había 2 segundos contabilizados y reservamos 3, quedan 5 sin comprometer mientras la llamada sigue pendiente. Si termina en 0.4 segundos, el acumulado pasa aproximadamente a 2.4, la reserva vuelve a cero y quedan 7.6 segundos.

El acumulado y el subtotal de recuperación no pueden retroceder. Los stores rechazan tiempos negativos, infinitos o NaN. El cambio de estado, resultados, contadores y medición se guarda como un snapshot transaccional.

### Paso 4 recuperar una llamada sin final confirmado

Si el proceso termina durante una llamada, no conocemos su duración real. Tampoco podemos calcularla restando la fecha de recuperación: eso incluiría horas de proceso detenido.

Al retomar, imputamos de manera conservadora toda la reserva pendiente a `active_seconds` y `recovered_time_seconds`, y dejamos `pending_time_seconds=0`. Luego empezamos a medir la nueva sesión. Esto puede cobrar más de lo que realmente ocurrió, incluso cuando el hook interrumpió antes de llamar al adaptador; el importe imputado es visible, no una falsa medición exacta.

```text
Límite: 10 s
Activo confirmado: 2 s
Reserva pendiente al caer: 3 s
               ↓ recuperar
Activo contabilizado: 5 s, de los cuales 3 s son imputados
Reserva pendiente: 0 s
Disponible para continuar: 5 s
```

Una interrupción `after-result` ya tiene el resultado confirmado y la reserva liberada, así que no genera este cargo. La lectura se reutiliza como antes.

Una interrupción `before-model` puede reservar hasta 30 segundos con la configuración inicial; al recuperar esa reserva se imputa aunque el modelo sea falso. Es deliberadamente conservador. `before-tool` puede reservar hasta 10 segundos, además de consumir otra reserva de invocación al reintentar la lectura. Contadores de intentos y tiempo son presupuestos distintos.

Si la reserva pendiente agotó todo el presupuesto, la recuperación termina con RUN_DEADLINE_EXCEEDED sin hacer otra llamada. Reiniciar no devuelve tiempo al run.

### Paso 5 aplicar el remanente y clasificar el error

Antes de cada paso comprobamos el remanente. Para cada await usamos `asyncio.wait_for` con el menor valor entre timeout individual y remanente global. Volvemos a comprobarlo al terminar una llamada y antes de confirmar COMPLETED; una respuesta tardía no se presenta como éxito.

- `UPSTREAM_TIMEOUT`: expiró una llamada individual mientras quedaba presupuesto global.
- `RUN_DEADLINE_EXCEEDED`: se agotó el tiempo activo global, incluyendo cargos de recuperación.
- `CONFIG_CHANGED`: intentamos continuar con límites distintos de los guardados.
- `TIME_BUDGET_UNKNOWN`: el run proviene del formato anterior y no tiene contabilidad de tiempo suficiente para retomarlo.

Error o cancelación observados dentro de este proceso guardan el intervalo medido y liberan la reserva. Una interrupción del hook usa `SystemExit` y deja la reserva persistida sin fingir que se completó la llamada.

Esto sigue siendo cancelación cooperativa: el adaptador debe respetarla. `wait_for` no puede matar un proceso remoto ni garantiza que un proveedor deje de consumir recursos. Tampoco interrumpe código síncrono que bloquea el event loop. Las operaciones SQLite son síncronas y pueden esperar locks hasta su propio timeout; por eso no es un deadline duro de tiempo real.

La medición se confirma en checkpoints. El tramo de trabajo síncrono posterior al último checkpoint puede no quedar medido ante una caída, y el tiempo del último commit no puede incluirse exactamente dentro de ese mismo commit. Las reservas protegen el presupuesto de llamadas en vuelo, no proporcionan un cronómetro perfecto de todos los intervalos ante cualquier fallo. Las pruebas no equivalen a garantías distribuidas.

### Paso 6 observar un deadline real sin APIs

```bash
uv run python -m harness --active-timeout 0.2 --demo-delay 0.3 start
```

`--demo-delay` agrega una espera artificial a cada llamada del modelo y Jira falsos. No modifica el estado del proveedor ni representa latencia de una API real. El límite global de 0.2 segundos debe ganar al timeout individual de 30 segundos del modelo y a su demora de 0.3 segundos.

Resultado esperado:

```text
status: FAILED
outcome_code: RUN_DEADLINE_EXCEEDED
active_limit_seconds: 0.2
active_remaining_seconds: 0
```

El tiempo registrado puede superar ligeramente 0.2 por planificación, cancelación y limpieza; no afirmamos precisión de tiempo real. El comando sale con código 1 porque el run falló deliberadamente por presupuesto.

### Paso 7 observar tiempo persistido sin cobrar la pausa

```bash
uv run python -m harness --active-timeout 2 --demo-delay 0.05 start --interrupt-at after-result
```

Copiar el UUID. Se habrá registrado aproximadamente el tiempo de la primera decisión y de la herramienta, con variaciones por trabajo local. Podés esperar entre comandos: esa pausa no se mide como una sesión activa.

```bash
uv run python -m harness status UUID_DEL_RUN
uv run python -m harness --demo-delay 0.05 resume UUID_DEL_RUN
```

Al retomar se conserva el límite de 2 segundos del snapshot, aunque no repitamos `--active-timeout`. Solo se agrega el tiempo de la sesión nueva; Jira no se repite porque su resultado estaba confirmado. `recovered_time_seconds` debe seguir en cero para `after-result`.

En cambio, un run interrumpido dentro de una llamada puede mostrar una imputación conservadora al recuperar. No confundir ese cargo con el tiempo de pausa entre comandos.

### Paso 8 comprobar que un reinicio no amplía el límite

```bash
uv run python -m harness --active-timeout 0.1 start --interrupt-at before-model
uv run python -m harness resume UUID_DEL_RUN
```

Reemplazar el UUID por el de este ejercicio nuevo. La primera llamada tenía reservado todo el remanente; al recuperar se imputa y el run falla por deadline sin volver a consultar el modelo. El contador y el tiempo no se reinician.

Si pasamos explícitamente otro `--active-timeout` al recuperar un run no terminado, el core devuelve CONFIG_CHANGED. Para probar una configuración distinta se crea una nueva intención/run; no se reescribe el presupuesto del existente.

### Paso 9 conservar los runs previos sin inventar mediciones

La estructura SQL de la tabla no cambia: `PRAGMA user_version` sigue en 1. El schema del payload JSON nuevo es 2. Son versiones de capas distintas.

Los payloads anteriores de schema 1 se pueden cargar para inspección. Como no medían tiempo, el CLI muestra campos de tiempo como `null`, no cero. Un run antiguo COMPLETED puede devolver su resultado almacenado tras validar acceso. Un run antiguo no terminado no se retoma: TIME_BUDGET_UNKNOWN evita regalarle 120 segundos como si nunca hubiera trabajado.

Los datos anteriores no se borran ni se migran fingiendo una medición. Para el ejercicio de tiempo se crean runs nuevos con `start`. Una migración real necesitaría una decisión explícita sobre presupuestos e historial desconocido.

### Paso 10 validar lo construido

```bash
uv run python -m unittest discover -s tests -v
```

**Validación de este incremento: 42 pruebas pasaron.** Las nuevas comprueban:

- Suma global de varias llamadas individualmente válidas.
- Cancelación real del await cuando el global es menor que el timeout individual.
- Distinción del timeout individual.
- Persistencia de tiempo y recuperación con otro origen de reloj, sin cobrar la pausa.
- Reserva huérfana parcial o total y rechazo antes de una nueva llamada si ya se agotó.
- Cancelación con tiempo guardado y reserva liberada.
- Tiempo que no retrocede y rechazo de valores no finitos o límites inválidos.
- Lectura de formato anterior y rechazo de recuperación sin contabilidad temporal.
- CLI que recupera el límite original sin exigir repetirlo.

Seguimos sin LLM real, servicios MCP o claves. El tiempo que ves en los demos corresponde a los adaptadores falsos y sus demoras artificiales.

### Siguiente paso

M1 ya tiene persistencia, checkpoints, recuperación de lectura y presupuesto temporal global. El próximo recorrido de M2 conectará una primera herramienta por MCP conservando estos controles, antes de distribuirlo en varios servicios o añadir escrituras.
