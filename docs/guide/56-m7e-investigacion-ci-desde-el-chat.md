# 56. M7e · Investigación de CI desde el chat

## 1. La pregunta que queremos responder

«¿El build de DEV-123 pasó para el commit del PR?». Un build exitoso solo demuestra algo sobre la revisión que ejecutó. El informe tiene que mostrar el ticket, el PR, la revisión consultada y la revisión que probó Jenkins.

## 2. Cómo se conecta el recorrido

1. **Admitir el turno.** El chat guarda el mensaje y el trabajo en PostgreSQL en la misma transacción. El escenario queda fijado en la conversación. Si reenviás la misma solicitud con la misma clave, recuperás el mismo run.
2. **Reunir evidencia.** El planificador determinista busca la clave explícita `DEV-123`. Jira devuelve el vínculo a `demo/atlas#42`. GitHub devuelve la revisión observada del PR. El worker consulta `atlas-tests#101` en el servidor MCP de Jenkins escrito en Go. La vinculación del job sale de la configuración de la demo. La descripción del ticket no concede permisos ni elige jobs.
3. **Comparar y publicar.** El finalizador existente valida los vínculos y compara las dos revisiones. Cada fuente se cita por su identificador. La política comprueba actor, workspace y permisos actuales de proyecto, repositorio y job. El informe queda guardado en el historial.
4. **Recuperar.** La pausa opcional ocurre después de guardar Jira. Al recuperar, el worker revalida la evidencia y continúa con GitHub y Jenkins en el mismo run, con los presupuestos persistidos de cuatro decisiones, tres herramientas y 120 segundos activos. La cola conserva sus controles de lease y fencing.

Este modo usa fixtures en los tres proveedores y reglas deterministas para planificar y redactar el informe. Cada turno pide la clave explícita y consulta evidencia actual. Si falta la clave o aparecen varios tickets distintos, pide aclaración sin llamar a MCP. El contador muestra decisiones del planificador. Las conversaciones documentales, Jira Cloud y GitHub público siguen disponibles con Qwen3.

| Escenario | Resultado | Qué puede afirmar el informe |
| --- | --- | --- |
| Build exitoso para el commit del PR | `VERIFIED` | Pasó CI para la revisión consultada. |
| Build de otro commit | `STALE_BUILD` | Jenkins ejecutó otra revisión. La revisión consultada sigue sin validarse. |
| Build fallido | `BUILD_FAILED` | El build de la revisión consultada falló. |
| Jenkins no disponible | `INCOMPLETE` | Jira y GitHub aportan evidencia. El estado de CI queda sin confirmar. |

El alcance del informe es la verificación de CI para una revisión observada. La aprobación de un despliegue se rige por un flujo separado.

## 3. Probalo en la consola

Con Docker activo, prepará PostgreSQL y el binario local de Jenkins si todavía no los tenés:

```bash
uv run python scripts/postgres.py up
uv run python scripts/build_jenkins.py
```

Reiniciá la API y el worker si están ejecutando una versión anterior. Iniciá cada comando en su terminal:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha
```

Abrí [Chat](http://127.0.0.1:8005/console#chat), ingresá con la credencial local y elegí un escenario del grupo **Investigación de CI · demostración**. Creá la conversación y preguntá «¿El build de DEV-123 pasó para el commit del PR?». Usá una conversación nueva para comparar con **Build de otro commit** o **Jenkins no disponible**.

Para practicar recuperación, marcá **Pausar después de la lectura** antes de enviar. Cuando aparezca **Recuperar este mensaje**, continuá y comprobá que conserva el identificador del run. Si está configurada la exportación de trazas, abrí Jaeger desde el turno y buscá las lecturas MCP y el intento que retomó el trabajo.

## 4. Pruebas reproducibles

```bash
uv run python -m unittest tests.test_ci_chat
uv run python -m unittest integration_tests.test_ci_chat
```

La primera prueba los cuatro resultados con servidores MCP reales sobre fixtures, además de la ausencia de clave, la denegación por job y la recuperación con evidencia confirmada. La segunda requiere PostgreSQL y prueba el recorrido por la API autenticada, el worker y el historial, incluido el aislamiento entre workspaces y la recuperación del mismo turno.
