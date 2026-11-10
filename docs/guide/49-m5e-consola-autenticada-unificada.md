# M5e Consola autenticada unificada

[Índice](../guide.md) · [← Anterior](48-m5d-rag-como-herramienta-del-core.md) · [Siguiente →](50-m5f-embeddings-semanticos-y-evaluaciones.md)

## 1. Objetivo: recorrer el harness desde un lugar

La entrada común es [la consola autenticada](http://127.0.0.1:8005/console). `/` en esa API abre la misma consola; `/knowledge` sigue disponible y abre su sección de conocimiento. Las rutas anteriores de API conservan sus contratos y la consola de fixtures de 8002/8003 sigue siendo el ejercicio de desarrollo separado.

Unificamos navegación y sesión dentro de una sola página, con cinco secciones:

| Sección | Qué permite |
| --- | --- |
| Ejecuciones | Iniciar CI o CI+RAG, consultar runs propios, recuperar checkpoints y revisar evidencia/citas. |
| Conocimiento | Cargar/retirar documentos, admitir indexación, seguir jobs y buscar fragmentos. |
| Aprobaciones | Revisar propuestas, decidir como otro actor, preparar operaciones y encolar pruebas simuladas; revisar cierres aparcados. |
| Observabilidad | Abrir Jaeger, Grafana y Prometheus; cada run puede enlazar a su traza. |
| Configuración | Consultar perfiles, límites e ingestión efectivos sin credenciales ni controles de edición. |

`frontend/console.html`, `console.css` y `console.js` forman la consola. El módulo de conocimiento existente conserva su carga/búsqueda y aporta un único cliente HTTP/sesión compartido. No añadimos un framework ni copiamos la lógica del core al navegador.

## 2. Una sesión en memoria, permisos en el servidor

Si la API aún no está levantada:

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
```

Emití una sesión con el mecanismo local existente y seleccioná el archivo que indica el comando:

```bash
uv run python scripts/users.py issue --actor reader-demo --workspace workspace-alpha
```

Cambiar entre secciones mantiene esa sesión en la página. La URL guarda únicamente el nombre de sección, por ejemplo `#runs` o `#knowledge`; no contiene bearer. Recargar o salir elimina la credencial de memoria y exige seleccionarla otra vez. No se guarda en localStorage, sessionStorage ni cookies.

`knowledge.js` es ahora un módulo compartido. La consola importa sus funciones de petición y consulta de identidad, sin exportar el bearer. Un evento local sin credenciales comunica entradas/salidas para limpiar los estados de runs, propuestas y configuración.

Cada petición conserva origen fijo, Authorization, rechazo de redirects y las protecciones CSP/no-store/origen del servidor. Además, se comprueba la generación de sesión después de recibir headers **y después de parsear el cuerpo**. Si el usuario salió o cambió de credencial mientras llegaba una respuesta, sus datos se descartan. Una respuesta 401 antigua tampoco puede limpiar la sesión posterior. Dos pruebas JavaScript simulan precisamente esas carreras.

La consola comprueba la identidad actual durante los sondeos. Si cambian sus permisos, limpia la sesión visible y pide seleccionar nuevamente la credencial. La API verifica siempre los grants actuales: ocultar un botón es una ayuda de navegación, no una autorización.

## 3. Roles distintos dentro de la misma interfaz

- `reader` puede consultar conocimiento y ejecutar lecturas permitidas.
- `operator` añade carga/indexación, propuestas y las acciones propias autorizadas.
- `approver` tiene la bandeja de revisión. Si carece de reader/operator, no obtiene acceso a documentos ni runs desde la consola.

La navegación se adapta al rol. Configuración y enlaces operativos están disponibles para las identidades habilitadas del laboratorio. El workspace procede de la sesión, sin un selector que permita asumir otro alcance.

Para revisar como otro actor, salí de la pantalla y seleccioná una credencial diferente:

```bash
uv run python scripts/users.py issue --actor approver-demo --workspace workspace-alpha
```

Esto usa la misma interfaz y dos sesiones reales del directorio local. No hay un botón que convierta al operador en aprobador ni se retoman las identidades fijas del modo de fixtures. Al terminar, revocá las sesiones de ejercicio con `scripts/users.py revoke SESSION_ID`.

## 4. Ejecuciones CI y RAG, presupuestos propios

En **Ejecuciones** elegí:

- **DevOps:** escenarios CI anteriores y pausas didácticas después de Jira/GitHub/Jenkins. La pausa de fallo corresponde al escenario Jenkins no disponible; la API rechaza una combinación inválida.
- **DevOps + conocimiento:** perfil RAG de M5d, con escenario/consulta fija y cinco decisiones. No cambia el límite de cuatro decisiones de CI.

**Iniciar lectura** usa las rutas anteriores: `/api/runs` para CI y `/api/knowledge/agent/runs` para RAG. La API admite; el worker de runs ejecuta. Para atender ambos perfiles:

```bash
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha
```

La lista común reúne los últimos 30 runs del actor/workspace, incluyendo metadatos CI y RAG. Abrir uno consulta su endpoint de detalle correspondiente. El detalle muestra contadores/reservas, tiempo activo, estado/intentos del worker, respuesta, fuentes y evidencia. RAG conserva la reautorización documental de M5d antes de devolver fragmentos guardados.

**Recuperar ejecución** aparece solo para un job pausado o con lease vencido. Reutiliza las rutas de reencolado existentes; no reinicia presupuestos ni crea otro run. Los botones no toman leases ni activan trabajo en el navegador.

**Crear o recuperar propuesta** aparece para un operador y una lectura CI completada. RAG no habilita propuestas M4; su evidencia documental mantiene el contrato de lectura. La clave de propuesta se conserva en memoria por run para repetir esa acción en la misma sesión de página. Entrar nuevamente permite consultar propuestas persistidas desde la bandeja; las claves temporales de UI no son almacenamiento durable.

Las listas tienen scroll limitado y evitan reconstruirse durante sondeos sin cambios, para facilitar selección y lectura. La consola consulta la sección activa cada cinco segundos; conocimiento conserva su sondeo de tres segundos mientras esté visible. No añade trabajo cuando el navegador está en otra pestaña ni solapa sondeos del mismo controlador.

## 5. Conocimiento conserva su worker independiente

Los componentes de carga, estado documental, indexación, jobs y búsqueda se mantienen dentro de **Conocimiento**, con la misma sesión que las demás secciones. Se conservan la carga idempotente, cuotas, revisión de inventario, retirada inmediata y verificación de citas.

La indexación sigue ejecutándose en otro proceso:

```bash
uv run python -m harness.knowledge_worker --actor reader-demo --workspace workspace-alpha
```

El worker de ingestión construye índices; el worker de runs consulta herramientas. Compartir pantalla no mezcla sus colas ni convierte una carga en una ejecución dentro de FastAPI. Sus deadlines, intentos y recuperación permanecen persistidos.

## 6. Propuestas y operaciones con dos actores

En **Aprobaciones** se muestra la bandeja existente del servidor. Al abrir una propuesta se ve su intención canónica, revisión, vigencia y hash. Los botones conservan el hash y `expected_version` del objeto revisado; un cambio concurrente provoca conflicto en el backend.

El recorrido disponible es:

1. El operador crea una propuesta desde una lectura CI válida.
2. Otro actor con rol approver revisa y aprueba o rechaza.
3. El solicitante vuelve con su credencial y prepara una operación única, consumiendo la aprobación según el contrato existente.
4. **Encolar pruebas simuladas** admite el job de escrituras. El worker de servicio mantiene la autoridad de envío/consulta del ledger.
5. La UI consulta el resultado persistido y eventos; SUCCEEDED confirma aceptación del proveedor simulado, no éxito del build.

La UI no implementa otro ejecutor de escrituras ni una consulta de reconciliación fuera del servicio. Si una operación queda aparcada NEEDS_REVIEW, muestra preparación de cierre al dueño y confirmación a otro aprobador, usando los endpoints de resolución manual existentes y sus guards. No ofrece cerrar un job con lease activo ni reenviar una operación incierta.

El worker de escrituras autenticadas de M3d4 debe estar ejecutándose para procesar esa cola:

```bash
uv run python -m harness.user_write_worker --actor reader-demo --workspace workspace-alpha
```

Los contratos y recuperación siguen en el [capítulo M3d4](42-m3d4-cola-y-worker-de-escrituras-autenticadas.md). Este incremento no cambia proveedor, aprobación, ledger, fencing ni la recuperación tras un efecto incierto.

## 7. Observabilidad y configuración: alcance claro

El enlace a una traza se deriva del `previous_traceparent` validado del job propio. La API entrega el trace ID permitido, sin headers ni bearer. El cliente construye una URL fija de Jaeger y abre una pestaña con `noopener noreferrer`. No recibe un destino de navegación desde documentos o herramientas.

**Observabilidad** reúne los enlaces locales a Jaeger, Grafana y alertas Prometheus. Conservan sus interfaces especializadas. Los monitores del laboratorio son compartidos y no aplican la ACL por workspace de esta consola; un portal con sesión no resuelve el aislamiento operativo pendiente de S09. No incrustamos un supuesto dashboard autenticado que prometa ese aislamiento.

**Configuración** muestra valores efectivos de modelos/perfiles, límites y cuotas. Es de solo lectura y construye una respuesta explícita a partir de configuración conocida; no devuelve variables de entorno, rutas de archivos privados, DSN, contraseñas o tokens. Cambiar límites/modelos sigue siendo una modificación versionada del proyecto. Los embeddings semánticos serán el siguiente incremento.

## 8. Dos lecturas de API nuevas, acciones anteriores intactas

`harness/console_api.py` añade solamente:

| Ruta | Frontera |
| --- | --- |
| GET `/api/console/runs` | Requiere reader/operator; actor/workspace actuales; últimos 30 metadatos sin answer/results/chunks. |
| GET `/api/console/config` | Identidad local habilitada con reader/operator/approver; valores efectivos no secretos y `editable: false`. |

Los detalles y mutaciones siguen en las rutas CI, RAG, documentos, propuestas, operaciones y resoluciones previas. `/` de la API autenticada ahora sirve HTML; los metadatos de identidad/configuración siguen en `/api/me`, `/api/config` y las nuevas lecturas. En modo fixture se mantiene la portada anterior y no se instalan las rutas de consola autenticada.

No hay nuevas migraciones de datos. No se trasladan las bases, no se duplica el historial y no se cambian los contratos del worker por unir las pantallas.

## 9. Verificación reproducible y siguiente paso

```bash
uv run python -m unittest integration_tests.test_console -v
node --test frontend_tests/session.test.mjs
```

Las pruebas de backend cubren shell común sin datos, autenticación, roles, aislamiento por actor/workspace, combinación de perfiles sin entregar texto guardado y referencias de traza válidas. Las pruebas JavaScript ejercitan respuestas 200/401 que terminan después de cambiar de sesión. Las suites existentes siguen cubriendo recuperación, retirada, aprobaciones/ledger y resolución manual.

El siguiente paso acordado es **embeddings semánticos y evaluaciones de recuperación/citas**. Esta consola ya permite observar sus resultados sin abrir otra aplicación del harness. OIDC, administración editable, protección de monitores y otras capacidades de producción siguen en el alcance pendiente.

Validación M5e: **243 pruebas de Python, 199 integraciones PostgreSQL/Chroma y 2 pruebas JavaScript aprobadas**. Cuatro integraciones nuevas comprueban shell/roles/ownership/configuración y referencias de traza, y dos pruebas JS descartan respuestas tardías 200/401 durante cambio de sesión. En navegador real se revisaron las cinco secciones con sesión común, se inició un RAG COMPLETED 5/4, se pausó CI tras Jira y recuperó hasta 4/3 con dos claims, y se creó/aprobó/preparó/despachó una operación con dos actores. El ledger quedó SUCCEEDED/DONE, un intento y exactamente una solicitud aceptada por el proveedor simulado. Recargar exigió seleccionar la credencial otra vez; salir limpió contenido/credencial, y ambas sesiones de verificación fueron revocadas. API autenticada actualizada; workers y contratos de recuperación/ledger previos conservados, sin migraciones nuevas. No quedaron jobs de lectura ni escritura activos del ejercicio. Gitleaks no encontró secretos en los 95 archivos publicables nuevos/modificados del conjunto M5 y los enlaces Markdown locales comprobados existen. Las capturas/reportes/credenciales no se incluyen en Git. No se repitieron las suites de rotación/contenedores ni los runners independientes de evals; la regresión de integración incluye sus pruebas existentes. Sin commit ni publicación. Próximo paso acordado: embeddings semánticos y evaluaciones.
