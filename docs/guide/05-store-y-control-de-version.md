# Store y control de versión

[Índice](../guide.md) · [← Anterior](04-puertos-y-adaptadores-falsos.md) · [Siguiente →](06-ciclo-de-ejecucion-propio.md)


`MemoryRunStore` guarda y devuelve copias. Si un consumidor modifica un objeto leído, no modifica silenciosamente la versión guardada.

El número `version` funciona como una comparación optimista, conocida como compare-and-swap o CAS:

```text
Leer versión 0
Guardar cambios esperando versión 0 → queda versión 1
Intentar guardar otra copia vieja que aún espera 0 → CONFLICT
```

Esto evita que una copia vieja sobrescriba el estado de otra dentro del ejercicio. No implementa bloqueo distribuido, leases reales ni seguridad entre procesos. PostgreSQL necesitará una actualización condicional/transacción; un diccionario Python no equivale a ella.

`save` también verifica la transición. No permite saltar de QUEUED a COMPLETED ni actualizar un estado terminal. Cargar o guardar con actor/workspace diferentes devuelve NOT_FOUND para no revelar existencia del run. En M0 el ownership es intencionalmente más estricto que «cualquier persona del mismo workspace».
