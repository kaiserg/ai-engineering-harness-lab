# M2 informe parcial cuando falta Jenkins

[Índice](../guide.md) · [← Anterior](15-m2-jenkins-en-go-y-comparacion-de-revisiones.md) · [Siguiente →](17-m2-s04-contenido-malicioso-y-cierre-del-hito.md)


### Paso 1 distinguir evidencia ausente de evidencia negativa

Hasta el primer incremento Jenkins, cualquier error de proveedor terminaba el run. Ahora cubrimos S02: Jira y GitHub respondieron, pero no pudimos obtener el build. Queremos conservar los hechos conocidos y explicar la ausencia sin declarar que CI pasó o falló.

**Decisión:** guardar errores y resultados como conceptos diferentes. `ToolFailure` contiene la llamada original (`ToolCall`) y un código estable; `Run.tool_failures` contiene los fallos de lectura confirmados. `Run.results` sigue conteniendo únicamente evidencia obtenida y validada.

```text
Jira responde   → results[0]
GitHub responde → results[1]
Jenkins falla  → tool_failures[0], sin inventar results[2]
               → informe parcial determinista
```

La ausencia no recibe una referencia de evidencia Jenkins. El informe solo cita las respuestas disponibles de Jira y GitHub; muestra el job/build solicitado y el motivo por separado.

### Paso 2 definir una política pequeña de fallos opcionales

No hacemos que todo error permita continuar. `OPTIONAL_READ_ERRORS` en `harness/contracts.py` define una lista cerrada:

```python
OPTIONAL_READ_ERRORS = {
    'jenkins_get_build': frozenset({'UPSTREAM_UNAVAILABLE', 'UPSTREAM_TIMEOUT'})
}
```

El core recibe `partial_tools=('jenkins_get_build',)` únicamente en el nuevo perfil CI. La opción no amplía permisos: la autorización ocurre antes de llamar al adaptador, y solo tratamos estos errores dentro del await de esa lectura.

| Situación | Comportamiento |
| --- | --- |
| Jenkins devuelve `UPSTREAM_UNAVAILABLE` | Registrar ausencia y continuar al informe parcial. |
| Vence el timeout individual de Jenkins y queda tiempo global | Registrar `UPSTREAM_TIMEOUT` y continuar. |
| Se agota el deadline global | FAILED; no usar un informe parcial para evadir el límite. |
| Permiso denegado, argumentos inválidos o recurso no encontrado | FAILED. |
| Schema distinto o resultado inválido | FAILED. |
| Error inesperado | FAILED, con código sanitizado. |
| Cancelación del usuario | CANCELLED. |
| Error de Jira, GitHub o modelo | Mantener su comportamiento bloqueante. |

`MCP_UNAVAILABLE` también sigue bloqueando. Hoy es una clasificación general de fallos de conexión/protocolo; no distinguimos suficientemente sus causas como para convertirlas todas en una ausencia opcional. La fixture `unavailable` entrega un código de proveedor explícito, y el timeout individual es una condición concreta. Ampliar esta lista requerirá clasificar y probar más errores, no capturar cualquier excepción y seguir.

### Paso 3 confirmar el fallo antes de continuar

En el core, al observar un error permitido:

1. Medimos el tiempo consumido y liberamos la reserva de la llamada terminada.
2. Comprobamos que todavía quede tiempo global.
3. Guardamos `ToolFailure` con el `call_id` y argumentos originales.
4. Limpiamos `pending_call` y dejamos el checkpoint en READY.
5. Confirmamos todo en SQLite y emitimos el hook `after-tool-failure`.
6. Continuamos con la siguiente decisión del modelo.

Los contadores no se devuelven al presupuesto: una consulta que falló también consumió un intento. No hay reintento automático de Jenkins para intentar completar el informe. El camino normal al informe parcial conserva cuatro decisiones del modelo y tres intentos de herramientas, con dos resultados y un fallo registrado.

Si cae el proceso después del commit del fallo, ese fallo ya está confirmado. Recuperar reutiliza tanto los resultados como la ausencia registrada; no vuelve a reservar ni enviar la consulta a Jenkins. Si la caída ocurre antes de confirmarlo, se conservan las reglas conservadoras de recuperación de la llamada pendiente de M1.

### Paso 4 representar lo desconocido sin convertirlo en falso

`finalize_ci_partial` en `harness/evidence.py` genera el informe. Si no hay fallos, reutiliza la comparación completa de revisiones. Si falta Jenkins, exige exactamente la evidencia Jira/GitHub relacionada y un fallo permitido del job/build esperado. Una respuesta prematura sin esas condiciones no obtiene una evaluación parcial inventada.

La evaluación guardada tiene esta forma:

```json
{
  "status": "INCOMPLETE",
  "same_revision": null,
  "build_successful": null,
  "pr_revision": "revision-A",
  "build_revision": null,
  "sources": ["jira:DEV-123:v1", "github:demo/atlas:pr:42:revision-A"],
  "missing": [{
    "tool_name": "jenkins_get_build",
    "arguments": {"job": "atlas-tests", "build_number": 101},
    "code": "UPSTREAM_UNAVAILABLE"
  }]
}
```

Usamos `null` porque no conocemos la revisión o resultado del build. `false` significaría que conocemos un resultado negativo, algo distinto de no haber podido consultarlo.

La respuesta final también se construye en código determinista. Aunque el modelo intente afirmar que CI pasó, o borre la lista de fallos de la copia que recibió, el core conserva el fallo real y explica: «No podemos confirmar si CI pasó para esa revisión».

El run puede terminar `COMPLETED` con evaluación `INCOMPLETE`: terminó de preparar un informe honesto con evidencia limitada. La salida del comando es 0 porque pudo producir ese informe. Un gate de CI no debe interpretar solamente ese código como aprobación; necesitaría exigir la evaluación y las políticas correspondientes. Este laboratorio todavía no autoriza acciones externas.

### Paso 5 conservar permisos y proteger el historial

Al recuperar, reautorizamos también la llamada registrada en cada `ToolFailure`. Si retiramos el permiso del job, el host no entrega el informe parcial almacenado, incluso si ya está COMPLETED. Los permisos del proyecto Jira y repositorio GitHub también se vuelven a comprobar como antes.

El store mantiene `tool_failures` como historial que solo puede crecer. No permite borrar o cambiar fallos confirmados, usar el mismo `call_id` simultáneamente como resultado y fallo, ni registrar una nueva ausencia que no corresponda a la llamada pendiente. Registrar el fallo exige limpiar la llamada pendiente y confirmar un checkpoint READY sin reserva temporal restante.

SQLite serializa `ToolFailure` y su `ToolCall` anidado; al cargar reconstruye ambos tipos. Los payloads anteriores no tenían `tool_failures`: se interpretan como lista vacía. No inventamos errores pasados ni reescribimos resultados antiguos.

### Paso 6 versionar el nuevo comportamiento

Los nuevos runs CI usan `mcp-devops-ci-v2:<escenario>`. El CLI recupera automáticamente la versión de perfil y el escenario guardados.

Los runs `mcp-devops-ci-v1:*` conservan el comportamiento anterior: un proveedor no disponible termina FAILED. Los runs antiguos fallidos siguen sin reanudarse. Para probar informes parciales creá un run nuevo; no modificamos el significado de los checkpoints guardados.

Los perfiles Jira y Jira→GitHub tampoco adquieren fallos opcionales. El permiso para generar un informe parcial es configuración del host, no una instrucción del modelo o del resultado de una herramienta.

### Paso 7 ejecutar S02

Desde la carpeta del proyecto, con el binario Jenkins ya preparado:

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario unavailable start
```

Esperado:

```text
status: COMPLETED
execution_profile: mcp-devops-ci-v2:unavailable
model_calls: 4
tool_calls: 3
saved_results: 2
assessment.status: INCOMPLETE
tool_failures: una entrada UPSTREAM_UNAVAILABLE de Jenkins
```

La respuesta cita Jira y GitHub, identifica `atlas-tests#101` como evidencia ausente y no afirma que CI pasó. `outcome_code` queda `null`: el informe terminó; el problema del proveedor está en `tool_failures` y `assessment.missing`.

No cambió el servidor Go, así que no es necesario recompilarlo para este incremento. Sigue siendo una fixture local, sin acceso a una instancia Jenkins real.

### Paso 8 recuperar después de guardar el fallo

```bash
uv run python -m harness --tool-backend mcp --workflow jira-github-jenkins --build-scenario unavailable start --interrupt-at after-tool-failure
uv run python -m harness status UUID_DEL_RUN
uv run python -m harness resume UUID_DEL_RUN
```

El primer comando termina con código 2 intencional, después de guardar el fallo y antes de la decisión final. Copiá ese UUID. El proceso anterior debe haber terminado antes de recuperar.

Al reanudar:

- El perfil y el escenario se restauran sin repetir flags.
- Las tres herramientas tienen cero conexiones e invocaciones en este proceso.
- El contador de herramientas permanece en 3; el del modelo pasa de 3 a 4.
- Se conserva el `call_id` del fallo y el informe sigue INCOMPLETE.
- No se imputa una reserva huérfana por ese fallo: su tiempo y finalización ya estaban confirmados.

Si no queda presupuesto para la última decisión del modelo, el run falla por el límite correspondiente. Tener resultados o fallos guardados no concede tiempo ni llamadas adicionales.

### Paso 9 comprobar y elegir el próximo incremento

```bash
uv run python -m unittest discover -s tests -v
```

**98 pruebas Python pasaron: las 82 anteriores y 16 nuevas.** Las nuevas cubren representación y persistencia de la ausencia, timeout individual frente a deadline global, errores bloqueantes, errores en lecturas obligatorias/modelo, intentos del modelo de alterar la conclusión, recuperación tras confirmar el fallo sin conexiones, revocación de permisos, historial inmutable, presupuesto de la respuesta final, cancelación, payloads antiguos y conservación del comportamiento v1. El recorrido de recuperación también se prueba entre procesos reales con el servidor Go.

S02 está cubierto para la indisponibilidad explícita de la fixture y el timeout individual. El siguiente incremento de M2 es S04: introducir instrucciones maliciosas en contenido de una herramienta y demostrar que se tratan como datos, sin cambiar permisos ni herramientas. Las integraciones reales y la autenticación empresarial siguen pendientes.
