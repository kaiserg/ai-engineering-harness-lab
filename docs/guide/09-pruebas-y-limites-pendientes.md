# Pruebas y límites pendientes

[Índice](../guide.md) · [← Anterior](08-contratos-aislados-de-aprobacion-y-recuperacion.md) · [Siguiente →](10-lo-que-sigue-en-m1.md)


```bash
uv run python -m unittest discover -s tests -v
```

**Validación inicial: 21 pruebas pasaron.** Cubren:

- Ejecución permitida, evidencia y contadores.
- Tool desconocida, tool prohibida y campos adicionales.
- Capacidad visible, permisos de proyecto y actor/workspace ajenos.
- Snapshots independientes y conflictos de versión.
- Transiciones inválidas y estados terminales.
- Límites de llamadas, timeout real de espera y cancelación.
- Resultado mal correlacionado y errores sanitizados.
- Hash de propuesta, autoaprobación, vencimiento y decisión duplicada.
- Operación interrumpida antes/después del despacho y estrategia UNKNOWN.
- Lease perdido, vencido o de otro propietario.

Estas pruebas no demuestran autenticación, idempotencia remota, auditoría durable, resiliencia distribuida, grounding general ni seguridad productiva. Hacen comprobables las primeras reglas del diseño.
