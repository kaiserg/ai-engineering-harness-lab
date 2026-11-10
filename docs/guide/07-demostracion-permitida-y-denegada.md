# Demostración permitida y denegada

[Índice](../guide.md) · [← Anterior](06-ciclo-de-ejecucion-propio.md) · [Siguiente →](08-contratos-aislados-de-aprobacion-y-recuperacion.md)


Ejecutar desde la carpeta del proyecto:

```bash
uv run python -m harness
```

Campos esperados, omitiendo UUID y versión:

```json
{
  "status": "COMPLETED",
  "model_calls": 2,
  "tool_calls": 1,
  "adapter_invocations_this_process": 1,
  "answer": "DEV-123: IN_REVIEW [jira:DEV-123:v1]",
  "outcome_code": null
}
```

Son dos decisiones: proponer la consulta y generar la respuesta desde su resultado. La referencia es evidencia ficticia, no un ticket real ni una verificación automática de que cualquier frase del modelo sea verdadera.

```bash
uv run python -m harness --scenario denied
```

El fake intenta `jira_delete_issue`. Está registrada para este ejercicio de denegación, pero no es visible como capacidad permitida ni autorizada. No hay una implementación de borrado; incluso alcanzar ese adaptador solo sería una llamada al fake de lectura.

```json
{
  "status": "FAILED",
  "model_calls": 1,
  "tool_calls": 0,
  "adapter_invocations_this_process": 0,
  "answer": null,
  "outcome_code": "FORBIDDEN"
}
```

La salida de proceso 1 es deliberada. **El dato clave es `adapter_invocations_this_process=0`**: el bloqueo ocurre antes de llamar al adaptador. El campo tool_calls cuenta intentos autorizados despachados, no propuestas denegadas.

Cada comando crea un run nuevo en un store nuevo en RAM. No hay un comando resume en M0 y no se comparten resultados entre invocaciones.
