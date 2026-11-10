# Ciclo de ejecución propio

[Índice](../guide.md) · [← Anterior](05-store-y-control-de-version.md) · [Siguiente →](07-demostracion-permitida-y-denegada.md)


`Harness` recibe sus dependencias en el constructor. Esa **inyección de dependencias** permite sustituir Jira o el modelo por un fake de timeout sin cambiar el core.

El método `execute` hace este recorrido:

1. Carga el run con su identidad y verifica QUEUED.
2. Cambia a RUNNING mediante el store.
3. Verifica y reserva una llamada al modelo antes de invocarlo.
4. Construye la lista visible de herramientas a partir de política y registro. Pasa una copia del run al modelo para que no pueda alterar nuestros contadores accidentalmente.
5. Si recibe respuesta final, valida tipo, longitud y contenido no vacío, y guarda COMPLETED.
6. Si recibe `ToolCall`, verifica nombre conocido, schema de argumentos y permiso.
7. Verifica y reserva una invocación de herramienta antes de despacharla.
8. Ejecuta con `asyncio.wait_for`, valida correlación y estructura básica del resultado, y guarda evidencia.
9. Vuelve al modelo dentro de los límites restantes.

Visibilidad y autorización son controles distintos: no ofrecer una herramienta reduce exposición; comprobarla justo antes de ejecutar bloquea incluso una propuesta forzada. Una tool desconocida produce UNKNOWN_TOOL; una conocida pero prohibida produce FORBIDDEN.

El registro actual solo implementa el schema de `jira_get_issue`. Cuando agreguemos otros tipos de tool moveremos sus validadores al catálogo; no interpretaremos todos sus argumentos como tickets de Jira.

### Límites implementados

`Limits` fija máximo de llamadas al modelo, máximo de invocaciones de tool y timeout por llamada. Los contadores se reservan antes del intento: un timeout no devuelve el intento al presupuesto. Si el modelo repite la misma consulta, alcanza BUDGET_EXCEEDED.

En M0 no contamos tokens ni costo porque no hay proveedor real. El deadline global y la reanudación durable se implementan en las secciones M1 siguientes; retries y concurrencia de lecturas siguen pendientes. Los valores objetivo del documento de contratos siguen siendo el diseño futuro, no funcionalidades existentes.

### Errores y cancelación

Errores de dominio guardan su código. Timeout guarda UPSTREAM_TIMEOUT. Una excepción inesperada guarda INTERNAL_ERROR, sin persistir el texto original. En esta fase un fallo de lectura termina el run; los resultados parciales se incorporarán cuando haya varias fuentes.

`CancelledError` guarda CANCELLED y se propaga para que el llamador sepa que canceló. Es correcto para este flujo sin escrituras. No extrapolarlo a una acción externa: en ese caso necesitaremos CANCELLING y posiblemente AWAITING_RESOLUTION, según el diseño.

Si el propio store falla, el error puede propagarse sin poder guardar el estado final. M1 deberá definir checkpoints y recuperación; no prometemos que un fallo de almacenamiento pueda registrarse en ese mismo almacenamiento caído.
