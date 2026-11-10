# M3c PostgreSQL y productor transaccional

[Índice](../guide.md) · [← Anterior](19-m3b-primer-frontend-del-harness.md) · [Siguiente →](21-m3c-worker-independiente-leases-y-recuperacion.md)


### Paso 1 definir este incremento

Agregamos PostgreSQL con Compose, un store que conserva los contratos de M1 y un productor que guarda **run y trabajo pendiente en una misma transacción**. Todavía no implementamos el worker que reclama ese trabajo.

```text
Productor → BEGIN → insertar run QUEUED → insertar job PENDING → COMMIT
                                                        ↘ error → ROLLBACK de ambos
```

El frontend y API actuales continúan usando SQLite y ejecución local. Cambiaremos su productor al integrar el worker con claims/leases, para no dejar trabajos PENDING de runs que la web ya ejecutó por otro camino. Esta primera fase se prueba con comandos independientes y pruebas reales de PostgreSQL.

### Paso 2 distinguir SQLite de PostgreSQL

SQLite guarda la base en un archivo que abre nuestra aplicación. PostgreSQL es un proceso servidor: la aplicación conecta por TCP y el servidor administra transacciones, filas y archivos de datos.

| Concepto | SQLite actual | PostgreSQL de este incremento |
| --- | --- | --- |
| Conexión | Archivo local. | Servidor en `127.0.0.1:55432`. |
| Driver | `sqlite3`, biblioteca estándar. | Psycopg 3, fijado en `uv.lock`. |
| Estado del run | JSON en una tabla local. | JSONB más columnas de búsqueda/versión. |
| Actualización | Transacción local y CAS. | Bloqueo de fila y CAS. |
| Persistencia operativa | Archivo fuera de Git. | Volumen Docker fuera del contenedor. |
| Trabajo pendiente | Tarea web en memoria, no cola durable. | Tabla `jobs` persistida; consumidor aún pendiente. |

No cambiamos el significado de las políticas, resultados, fallos, perfiles o presupuestos al cambiar de base. Reutilizamos `check_new`, `check_update`, `encode` y `decode` de `harness/storage.py`.

### Paso 3 agregar Compose con un solo servicio

`compose.yaml` declara un proyecto propio y únicamente el servicio `postgres`. Usamos la imagen oficial `postgres:18.6-alpine`, un puerto ligado a loopback, un healthcheck y un volumen con nombre.

```yaml
ports:
  - "127.0.0.1:55432:5432"
volumes:
  - postgres_data:/var/lib/postgresql
```

El primer puerto es el del equipo anfitrión; el segundo es el interno del contenedor. Elegimos 55432 para reducir conflictos con una instalación que ya use el puerto PostgreSQL habitual.

El volumen guarda los archivos fuera de la vida del contenedor. En PostgreSQL 18 la imagen usa una disposición por versión y el montaje se hace en `/var/lib/postgresql`, como describe la [imagen oficial](https://hub.docker.com/_/postgres). No copiamos la ruta de datos de una imagen de otra versión sin revisarla.

El healthcheck usa `pg_isready`; `up --wait` espera que el servicio esté saludable. Después el script verifica además una conexión real con el rol de la aplicación. Estar saludable no equivale a que nuestra migración o permisos estén correctos.

En el entorno de desarrollo encontramos un motor de Colima activo mientras el contexto Docker predeterminado no tenía motor disponible. Usamos un contexto explícito, sin cambiar el contexto global. El proyecto y volumen del laboratorio tienen nombres propios; no detenemos otros servicios del motor.

### Paso 4 crear credenciales locales sin incluirlas en Git

`harness/postgres_config.py` genera dos contraseñas aleatorias en `data/runtime/`, con permisos de archivo 0600:

- `postgres-admin-password`: bootstrap administrativo del contenedor.
- `postgres-app-password`: conexión del store de la aplicación.

El directorio ya está excluido de Git. No abrimos `.env`, copiamos claves del laboratorio anterior ni ponemos una contraseña fija en Compose o en el código. Si los archivos existen, el script no los rota automáticamente.

Compose monta el secreto administrativo como archivo y la imagen lo lee mediante `POSTGRES_PASSWORD_FILE`. El script de preparación usa esa conexión para crear `harness_app` y otorgarle CONNECT/CREATE sobre esta base. La app no tiene SUPERUSER, CREATEROLE o CREATEDB; lo verificamos en una prueba real.

CREATE permite que este prototipo aplique su migración en un schema propio. Cuando separemos despliegue y operación, podremos usar un rol de migración y otro limitado a DML. Por ahora no presentamos este rol local como una configuración de permisos completa de producción.

La función `local_dsn` arma la conexión sin imprimirla ni pasarla como argumento de un proceso. **DSN** significa los datos necesarios para conectar: host, puerto, base, usuario y credencial. Los scripts muestran estado y códigos de error; no el DSN ni las contraseñas.

### Paso 5 levantar y consultar el servicio

Desde la carpeta del proyecto:

```bash
uv sync --locked
uv run python scripts/postgres.py up
uv run python scripts/postgres.py status
```

Si Docker funciona en otro contexto, el primer `up` puede indicarlo explícitamente:

```bash
uv run python scripts/postgres.py --context NOMBRE_CONTEXTO up
```

Reemplazá el nombre por uno disponible en `docker context ls`. Tras un `up` exitoso, conservamos esa selección en `data/runtime/docker-context`, fuera de Git. Los comandos siguientes la reutilizan sin cambiar la preferencia global del usuario. Si Docker Desktop o el contexto actual ya funciona, no necesitás ese flag.

`up` genera los secretos faltantes, inicia este servicio, espera salud, crea/verifica el rol de aplicación y prueba su conexión. En este entorno el servicio ya quedó preparado.

Para reiniciarlo o retirar el contenedor conservando sus datos:

```bash
uv run python scripts/postgres.py restart
uv run python scripts/postgres.py down
uv run python scripts/postgres.py up
```

`down` no incluye `--volumes`: el volumen permanece. Conservá también los archivos de credenciales locales para volver a conectar al mismo volumen. La imagen no cambia la contraseña de una base existente solamente porque se genere otro archivo en el host.

### Paso 6 definir una migración explícita

`harness/migrations/001_postgres.sql` crea dos tablas:

| Tabla | Qué conserva |
| --- | --- |
| `runs` | UUID, workspace, actor, versión, fecha y payload JSONB del run. |
| `jobs` | ID de trabajo, run asociado, workspace, actor, estado PENDING y fecha. |

**JSONB** es la representación de JSON que PostgreSQL almacena y permite consultar como dato estructurado. Conservamos el payload de dominio para compartir serialización con SQLite; las columnas separadas sirven para filtrar propietario, ordenar y hacer CAS.

Una clave foránea compuesta exige que el job apunte al mismo run, workspace y actor. `jobs.run_id` es único: no creamos varios trabajos iniciales para el mismo run. Los índices acompañan las consultas por propietario y fecha.

La tabla `schema_version` registra la versión SQL, inicialmente 1. Es independiente del schema 2 del JSON del run y de la versión mayor 18 de PostgreSQL. Una versión SQL desconocida se rechaza, sin sobrescribirla o borrar datos.

El inicializador aplica la migración dentro de una transacción y usa un advisory lock transaccional para serializar inicializaciones del mismo schema. Los identificadores de schema se componen con `psycopg.sql.Identifier`, no interpolando texto recibido del modelo.

En esta fase `jobs` solo admite PENDING. Añadiremos los estados y campos de claim/lease mediante una migración posterior junto con su consumidor, en vez de fingir que ya existe un worker.

### Paso 7 conservar el contrato RunStore y separar admisión de ejecución

`harness/postgres_store.py` implementa `create`, `load`, `list`, `save` y `requeue_read` con los contratos actuales. Eso permite probar el core existente contra PostgreSQL sin cambiar su loop.

Para el productor agregamos una operación distinta:

```python
store.enqueue(run, identity)
```

`enqueue` inserta el run y su job dentro del mismo bloque de conexión/transacción. Si la segunda inserción falla, también se revierte la primera. El contexto de conexión de Psycopg confirma o revierte al salir, como explica su [documentación de transacciones](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).

`create` sigue siendo el contrato de persistencia para ejecuciones directas del core y no crea un job. `enqueue` es la admisión durable del futuro productor API. No son dos nombres para la misma operación.

Un run admitido por `enqueue` no puede ejecutarse mediante `save` sin claim: devuelve `WORK_NOT_CLAIMED`. De ese modo no dejamos un job pendiente mientras alguien ejecuta el run por un camino alternativo. El worker deberá implementar y presentar ese reclamo en el próximo incremento.

### Paso 8 mantener CAS, identidad y errores sanitizados

Para actualizar un run cargamos su fila con `SELECT ... FOR UPDATE`, verificamos las reglas compartidas y confirmamos solo la versión esperada. El bloqueo protege la transacción de actualización; CAS evita que una copia vieja sobrescriba el estado nuevo. Ver los [bloqueos de fila de PostgreSQL](https://www.postgresql.org/docs/current/explicit-locking.html).

Las consultas filtran por workspace y actor. Al restaurar, también comprobamos que UUID, propietario y versión del payload coincidan con sus columnas SQL. Las políticas de acceso al contenido siguen perteneciendo al core/API; el filtro de propietario no convierte este incremento en autenticación empresarial o RLS.

Los errores del driver se traducen a códigos, como DATABASE_UNAVAILABLE o DATABASE_ERROR. Una colisión de IDs devuelve CONFLICT; una versión SQL desconocida, STORAGE_SCHEMA_UNSUPPORTED. No devolvemos mensajes del driver que puedan incluir detalles de conexión o consultas.

El adaptador es síncrono, como el store SQLite actual. Cada operación abre/cierra su conexión y tiene límites de conexión, sentencia y espera de locks. Esto mantiene simple el primer contrato, pero el deadline del core sigue sin ser un interruptor de tiempo real sobre código síncrono. Pooling o un puerto asíncrono se evaluarán con el worker y la carga; no los agregamos antes de tener una comprobación que los necesite.

Un bloqueo de fila o un CAS **no son un lease de worker**. No garantizan por sí solos que un ejecutor antiguo deje de realizar acciones después de perder su trabajo. Claim, vencimiento y fencing son el próximo paso.

### Paso 9 admitir un trabajo y verlo pendiente

```bash
uv run python -m harness.postgres enqueue
uv run python -m harness.postgres status
```

El primer comando devuelve un UUID en QUEUED. El segundo debe mostrar ese run y un job PENDING con el mismo `run_id`. Los límites iniciales ya están guardados en el run.

Es correcto que permanezca pendiente: en este incremento solo implementamos el productor. La API y pantalla habituales no se han cambiado a PostgreSQL ni consumen estos trabajos. Su base SQLite sigue siendo independiente y no hacemos una migración silenciosa de su historial.

### Paso 10 comprobar persistencia fuera del contenedor

Después de admitir el run y anotar su UUID:

```bash
uv run python scripts/postgres.py down
uv run python scripts/postgres.py up
uv run python -m harness.postgres status
```

Esperado: el mismo UUID del run y el mismo ID de job siguen QUEUED/PENDING. Esta comprobación recrea el contenedor, no solo vuelve a abrir una conexión. La realizamos con el servicio propio del laboratorio y conservando el volumen.

No demuestra recuperación del worker: demuestra que el productor dejó trabajo durable fuera de su memoria y de la vida del contenedor.

### Paso 11 ejecutar pruebas con una base real

La suite habitual sigue funcionando sin requerir que PostgreSQL esté activo:

```bash
uv run python -m unittest discover -s tests -v
```

La integración PostgreSQL está en otro directorio y exige un servidor real; no omite pruebas si falta:

```bash
uv run python -m unittest discover -s integration_tests -p test_postgres.py -v
```

**Validación: 126 pruebas habituales y 14 pruebas de integración PostgreSQL pasaron.** Las 14 nuevas verifican persistencia entre conexiones, run/job atómicos, rollback si falla el job, duplicados sin trabajos huérfanos, filtrado por actor/workspace, dos escritores concurrentes con versión vieja, presupuestos/configuración inmutables, recuperación del core sin repetir Jira, guardia de trabajo no reclamado, FK de propietario, versión SQL desconocida, payload corrupto, rol sin superusuario y el informe parcial MCP con fallos/evaluación en JSONB.

Cada prueba crea su propio schema aleatorio `harness_it_*` y elimina exclusivamente ese schema al terminar. No borra las tablas de `harness_lab` o los jobs de la demostración. Ninguna de estas 14 pruebas usa una simulación de PostgreSQL. Además confirmamos manualmente que el run/job sobrevivieron al `down`/`up` conservando el volumen.

### Glosario del incremento

| Término | Significado en este laboratorio |
| --- | --- |
| Imagen | Paquete versionado desde el que Docker crea el servicio PostgreSQL. |
| Contenedor | Proceso aislado que ejecuta esa imagen. |
| Volumen | Almacenamiento que conservamos al recrear el contenedor. |
| Healthcheck | Comprobación de disponibilidad del servicio. |
| DSN | Parámetros de conexión, incluida la credencial; no se imprime. |
| Migración | Cambio versionado y reproducible de la estructura SQL. |
| Transacción | Unidad que confirma todos sus cambios o los revierte. |
| Productor | Componente que admite el run y guarda su trabajo pendiente. |
| Job | Registro durable que indica trabajo que otro componente debe ejecutar. |
| Claim | Reclamo que habilitará a un worker a ejecutar un job; aún pendiente. |
| Lease | Reclamo con vencimiento, que implementaremos junto al worker. |

### Próximo paso

Agregar el consumidor independiente: reclamar un job, registrar lease/versión, ejecutar el core y confirmar su resultado. Después conectaremos el productor API a PostgreSQL y haremos que la pantalla observe esa ejecución. Recién con esas piezas probadas podremos interrumpir un worker y estudiar recuperación y concurrencia entre ejecutores.
