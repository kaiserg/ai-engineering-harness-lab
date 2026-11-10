# M3c alertas e incidente investigable

[Índice](../guide.md) · [← Anterior](28-m3c-prometheus-y-grafana.md) · [Siguiente →](30-evaluaciones-deterministas-del-recorrido.md)


### Paso 1 separar medición, alerta y respuesta

Prometheus recolecta series y evalúa reglas; Grafana también puede evaluar reglas usando ese datasource. Alertmanager recibe y agrupa alertas. Jaeger permite investigar un intento concreto. Ninguno de estos componentes concede permisos ni decide si repetir una herramienta.

Agregamos `observability/alerts.yml`, `observability/alerts.test.yml`, `observability/alertmanager.yml` y `observability/grafana/provisioning/alerting/harness.yml`. Todo queda provisionado desde archivos. El dashboard ahora tiene diez paneles: los ocho anteriores, estado de alertas Prometheus y listado de alertas Grafana.

| Motor | Regla | Condición sostenida | Decisión |
|---|---|---|---|
| Prometheus | `HarnessExporterUnavailable` | `up == 0`, 30 s | Detectar que no podemos consultar un exporter. Incluye el rol, sin IDs de runs. |
| Prometheus | `HarnessStateUnavailable` | snapshot inválido o ausente con exporter API disponible, 30 s | Evitar interpretar datos faltantes como cola vacía; suprimir la duplicación cuando el exporter API ya está caído. |
| Prometheus | `HarnessCertificateExpiring` | vigencia activa menor a siete días, 5 min | Investigar renovación antes del vencimiento; jamás desactivar validación TLS. |
| Grafana | `Cola pendiente del harness` | suma de jobs `PENDING` mayor que cero, 30 s | Observar impacto en el trabajo. `PAUSED` es intencional y queda excluido. |

`for` exige continuidad: primero pending, luego firing si persiste. La evaluación ocurre cada diez segundos; el scrape también. Los 30 segundos no prometen detectar exactamente 30 segundos después de detener el proceso. Un blip breve puede recuperarse sin disparar. Los umbrales son didácticos: aún no tenemos SLOs ni distribución de carga empresarial para definirlos.

Grafana conserva `NoData` y `Error` como estados distintos de Normal. Una consulta que falla no prueba que el servicio esté bien. Durante la verificación encontramos que `settings.mode: strict` no era compatible con el reducer de Grafana 13; usamos las opciones predeterminadas y comprobamos `health: ok` del motor real. Provisionar un archivo no basta para validar una alerta.

### Paso 2 recibir las alertas localmente

Alertmanager está en http://127.0.0.1:9093, con volumen propio y puerto exclusivo de loopback. Su receiver `local-only` no tiene integraciones: podemos ver y agrupar alertas sin enviar correo, Slack ni webhooks externos. Prometheus le entrega sus reglas; Grafana usa un contact point `prometheus-alertmanager` hacia el mismo servicio. La política de notificación está provisionada y reemplaza la ruta predeterminada.

Vistas:

- Prometheus: http://127.0.0.1:9090/alerts.
- Grafana: http://127.0.0.1:3003/alerting/list y dashboard `/d/harness-overview`.
- Alertmanager: http://127.0.0.1:9093.
- Jaeger: http://127.0.0.1:16686.

El rol anónimo Viewer sigue siendo de lectura. Los archivos requieren reiniciar Grafana para aplicar cambios explícitamente (`scripts/stack.py reload-dashboard`). Para incorporar el servicio nuevo y los mounts, usá `uv run python scripts/stack.py up`.

### Paso 3 preparar un incidente con evidencia previa

Desde la raíz del repositorio, con la app activa y sin otros runs en ejecución:

```bash
uv run python scripts/incident.py run
```

No uses otro worker ni envíes nuevos trabajos durante el ejercicio. El comando rechaza una cola con jobs PENDING/RUNNING y exige que el worker de Compose esté activo. Toma el mismo lock de operación que mantenimiento/stack para evitar una intervención simultánea del planificador; no cambia su configuración ni sus certificados. Durante el ejercicio el planificador puede posponer un ciclo y después retoma su operación normal.

El recorrido es:

1. Crear un run sintético `matching` con pausa tras Jira.
2. Esperar `PAUSED`: evidencia Jira confirmada, una decisión y una tool consumidas.
3. Guardar intención de restauración, run ID, hash de evidencia y trace ID antes de detener el worker.
4. Detener únicamente el worker de este Compose y reanudar el run desde la API. PostgreSQL conserva el job `PENDING`.
5. Esperar que ambas alertas lleguen a firing y comprobar que Alertmanager recibió las dos fuentes.
6. Restaurar el worker, esperar DONE y validar evidencia, presupuestos e intentos.
7. Esperar que ambas reglas vuelvan a inactive. Alertmanager deja de mostrar las instancias activas tras recibir su resolución.

La parada es deliberadamente ordenada: enseña indisponibilidad de capacidad, cola durable y recuperación entre intentos. Las pruebas anteriores de leases/fencing cubren las interrupciones con un intento en curso; aquí no pretendemos simular una escritura externa incierta.

### Paso 4 recuperarse si interrumpimos el propio ejercicio

```bash
uv run python scripts/incident.py status
uv run python scripts/incident.py recover
```

El journal privado `data/runtime/incident.json` se guarda de forma atómica, con permisos restringidos y fuera de Git. La intención precede a `stop`; si el proceso muere entre ambos, `start` sigue siendo seguro. `finally` intenta restaurar incluso ante error o Ctrl-C; un cierre forzado requiere `recover`.

`recover` inicia el worker de forma idempotente. Si el job todavía está PAUSED, lo reanuda una vez; si está PENDING/RUNNING, espera; si terminó DONE, valida. No crea otro run ni reintenta automáticamente un FAILED/CANCELLED. Si un deadline venció, hay que investigar ese resultado: el ejercicio no extiende límites para ocultarlo. Un segundo `recover` tras completar no hace nada.

### Paso 5 investigar en Grafana y Jaeger

El comando imprime dos enlaces de Jaeger: primer intento y recuperación. No añadimos run IDs a etiquetas Prometheus: la consola/API y el reporte privado sirven para localizar el run afectado, y los enlaces de trazas conectan los intentos.

1. En Grafana, correlacioná worker no scrapeable, cola pendiente y snapshot válido. Un snapshot válido ayuda a distinguir falta de consumidor de una lectura fallida de PostgreSQL.
2. En Prometheus, revisá `HarnessExporterUnavailable{role="worker"}`. `up == 0` solo prueba fallo del scrape; confirmá la causa con el estado/log del servicio.
3. Abrí el primer intento en Jaeger: una decisión y una invocación Jira, con spans distribuidos worker → identidad → Jira.
4. Abrí el segundo: tres decisiones y dos herramientas pendientes (GitHub y Jenkins), `run.confirmed_results=1` en `worker.attempt`, y link al intento anterior y al productor de reanudación.
5. Verificá que el segundo intento no incluye el servicio Jira. El total durable termina en cuatro decisiones, tres herramientas y dos intentos; el hash de la evidencia inicial coincide.
6. El intervalo con el worker apagado no contiene spans de ejecución: Jaeger no inventa actividad mientras el trabajo espera. Esa espera se entiende junto a las métricas y los tiempos de los productores/intentos, no como una tool de duración enorme.

**Resultado del ejercicio real:** ambas alertas dispararon y llegaron al receptor local; después quedaron inactivas y sin alertas activas en Alertmanager. El run terminó con cuatro decisiones, tres tools y dos intentos. Consultamos las dos trazas por API de Jaeger y comprobamos una tool en el primer intento, dos en el segundo, tres decisiones en la recuperación, evidencia confirmada igual a uno y enlace al intento anterior. Jira no se repitió.

### Paso 6 comprobar reglas y controles

`observability/alerts.test.yml` contiene siete escenarios de tiempo virtual de `promtool`: caída sostenida, blip breve, snapshot fallido, snapshot ausente, supresión al caer API, certificado cercano y certificado vigente. Verifican estados antes/después del `for` y resolución. Se ejecutan con el `promtool` de la misma imagen de Prometheus; no se espera cinco minutos reales para probar el certificado.

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s container_tests -p test_alerts.py -v
```

El segundo comando valida reglas dentro del contenedor, salud de los motores, receptor sin integraciones externas y dashboard aplicado. El incidente real es explícito y separado para no detener el worker inadvertidamente al validar solo las reglas.

No cambiamos el core ni sus presupuestos, autorizaciones, deadlines, tiempo activo, CAS, leases, heartbeat o fencing. Los monitores y Alertmanager no son dependencias de ejecución. Tampoco agregamos una reparación automática disparada por alertas: la restauración corresponde al ejercicio operado por la persona.

Referencias: [reglas Prometheus](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/), [pruebas con promtool](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/), [provisioning de alertas Grafana](https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/file-provisioning/) e [integración Alertmanager](https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/configure-alertmanager/).

Validación de este incremento: **184 pruebas locales, 6 pruebas Compose de métricas/alertas y 7 escenarios promtool pasaron**. Además ejecutamos el incidente real, verificamos ambas trazas y repetimos `recover` tras finalizar sin crear otro intento. No se modificó el core ni se repitieron las pruebas de rotación/PostgreSQL que no cambian en este incremento.
