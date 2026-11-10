# M3c API y worker en Docker Compose

[Índice](../guide.md) · [← Anterior](21-m3c-worker-independiente-leases-y-recuperacion.md) · [Siguiente →](23-m3c-jira-por-streamable-http-e-identidad-de-servicio.md)


### Paso 1 cambiar la operación sin cambiar el core

Hasta ahora PostgreSQL estaba en un contenedor, pero API y worker se iniciaban en terminales del host. Ahora operamos los tres servicios con Compose:

```text
Navegador → 127.0.0.1:8002 → [api]
                              ↓
                         [postgres] ← [worker] → MCP stdio Python / Go
```

La API conserva el productor transaccional y el worker conserva claims, heartbeat, fencing y recuperación. No cambiamos el protocolo HTTP de la pantalla ni agregamos otro loop de agente.

**Objetivo:** poder preparar, levantar, consultar y bajar el laboratorio con comandos reproducibles, y comprobar qué pasa al detener o reiniciar un servicio. Esto agrega operación de procesos, no nuevas capacidades del modelo.

MCP todavía es stdio dentro del worker. Los servidores Jira/GitHub y el binario Jenkins son procesos hijos del contenedor worker; no son servicios HTTP separados ni están aislados entre sí como contenedores independientes.

### Paso 2 construir una imagen con dos etapas

`Dockerfile` usa una etapa Go y otra Python:

1. `golang:1.27.3-alpine` descarga módulos fijados y compila Jenkins para Linux.
2. `python:3.13-slim` instala uv 0.12.18 y sincroniza `uv.lock` sin dependencias de desarrollo.
3. La imagen final recibe el código Python, frontend y el binario Linux compilado, no el compilador Go o el entorno del host.

Usamos `CGO_ENABLED=0` para que este binario Go no dependa de la biblioteca C de Alpine al ejecutarse en la imagen Python de Debian. No copiamos el ejecutable macOS que habíamos construido en `bin/`.

API y worker usan una sola imagen de la aplicación, con comandos de arranque distintos. El script construye esa imagen una vez antes de levantar ambos servicios; evita dos builds concurrentes si el motor usa el builder clásico.

Ordenamos las capas para que un cambio de frontend o código no obligue a descargar nuevamente las dependencias si `pyproject.toml`/`uv.lock` siguen iguales. La imagen Python base conserva su etiqueta 3.13-slim; el lock fija las dependencias Python, no todos los bytes de una imagen base que puede recibir actualizaciones.

Referencia: [integración de uv con Docker](https://docs.astral.sh/uv/guides/integration/docker/).

### Paso 3 limitar qué recibe el build

`.dockerignore` permite únicamente Dockerfile, archivos de dependencias, `harness`, `frontend` y las fuentes Go necesarias. Excluye también `.env*`, SQLite, cachés y Git en cualquier carpeta.

**Decisión:** no enviar toda la carpeta de trabajo al builder y confiar después en borrar archivos de una capa. Las contraseñas locales, bases, historial y `.venv` del host no forman parte del contexto permitido. Las dependencias del contenedor se instalan para Linux desde el lock.

Las pruebas inspeccionan una imagen sin mounts y confirman que no tiene el archivo de contraseña de runtime o `.env`. En los servicios verifican también que no aparezcan `.git`, la base SQLite del host ni un binario macOS en lugar de ELF Linux.

No realizamos una auditoría genérica de cualquier secreto futuro que alguien escriba dentro del código; la lista de build y las comprobaciones protegen las ubicaciones concretas del laboratorio.

### Paso 4 pasar configuración de red del host a la red Compose

Dentro de un contenedor, `127.0.0.1` apunta a ese contenedor. El servicio PostgreSQL se alcanza por su nombre DNS `postgres` y puerto interno 5432.

Compose configura:

```yaml
environment:
  HARNESS_POSTGRES_HOST: postgres
  HARNESS_POSTGRES_PORT: "5432"
```

`local_dsn` admite esas dos opciones confiables para la conexión de aplicación. La conexión administrativa del script del host sigue fija en loopback/55432. Ninguno de estos destinos se toma de un prompt, argumentos de tool o body del navegador.

La API escucha en `0.0.0.0` **dentro** de su contenedor, pero Compose publica únicamente `127.0.0.1:8002:8000` en el host. No abrimos la interfaz de desarrollo a toda la red del equipo. Los modos locales conservan el bind loopback predeterminado.

### Paso 5 montar credenciales al arrancar y abandonar root

Compose entrega solamente el secreto de aplicación a API/worker. El secreto administrativo sigue reservado al bootstrap PostgreSQL.

El archivo del host está protegido con modo 0600. Para conservar ese modo sin hacerlo legible para todos, `harness/container_entrypoint.py` realiza un bootstrap breve:

1. Lee el secreto montado y valida su formato local.
2. Escribe una copia 0600, propiedad de UID/GID 10001, en un `tmpfs` de runtime.
3. Vacía grupos suplementarios y cambia a ese UID/GID.
4. Usa `exec` para reemplazarse por API o worker.

La contraseña no se incorpora como valor de una variable de entorno, argumento del proceso o capa de imagen. La copia privada existe solamente en el filesystem temporal del servicio y se prepara otra vez al iniciar. El archivo original del host y el volumen PostgreSQL siguen siendo los que debemos conservar.

**No afirmamos que todo el contenedor arranque sin root:** el bootstrap y el iniciador necesitan permisos iniciales. Lo que verificamos es que el proceso de la aplicación opera con UID 10001, `CapEff=0` y `NoNewPrivs=1`.

El filesystem raíz del servicio es de solo lectura. Las escrituras de runtime y temporales van a `tmpfs`; los datos durables están en el volumen de PostgreSQL. Activamos `no-new-privileges` y limitamos las capacidades iniciales a cambio de UID/GID, propiedad/lectura del secreto y reenvío de señales.

`KILL` permite que el iniciador reenvíe señales a su hijo después del cambio de UID. Es una necesidad concreta del cierre ordenado: la prueba durante ejecución comprueba ese recorrido. Después del cambio de UID, la aplicación mantiene cero capacidades efectivas.

### Paso 6 definir servicios opcionales y dependencia de arranque

`compose.yaml` agrega `api` y `worker` bajo el perfil `app`. PostgreSQL sigue siendo el servicio base; podemos levantar solo la base para ejercicios de persistencia sin iniciar toda la aplicación.

El perfil y comportamiento de servicios opcionales están descritos en la [documentación de Compose](https://docs.docker.com/compose/how-tos/profiles/). El script de la app activa explícitamente `app`.

Antes de iniciar API/worker, `scripts/stack.py up` usa el bootstrap PostgreSQL existente. Así el rol y las credenciales ya están preparados; después construye la imagen y ejecuta Compose con espera de disponibilidad.

El healthcheck PostgreSQL comprueba disponibilidad del servicio. El de API comprueba que responde por HTTP. Para el worker, sin healthcheck propio, Compose comprueba que el proceso esté corriendo. Ninguna de esas señales por sí sola demuestra que todo un run funcione: las pruebas end-to-end ejecutan realmente el recorrido.

Usamos `init: true` para reenviar señales y recoger procesos hijos de MCP, y una política de reinicio de servicios. No agregamos Kubernetes o un supervisor externo para esta primera operación local.

### Paso 7 separar cierre ordenado de caída

El worker ahora atiende SIGTERM solicitando dejar de reclamar jobs nuevos y terminar el run actual. Mantiene su heartbeat mientras termina y sale después de confirmar el resultado. La API también tiene su cierre normal.

Compose permite hasta 45 segundos de parada. Si se supera ese período, el motor puede terminar el proceso por la fuerza; un trabajo no confirmado queda sujeto a lease y recuperación como antes. No prometemos que cualquier operación de 120 segundos complete dentro de esa ventana.

Un cierre ordenado no equivale a una caída abrupta. Verificamos que detener el worker durante un run de lectura normal permite terminar COMPLETED/DONE. Las pruebas de caída real del incremento anterior siguen cubriendo recuperación después de un proceso terminado sin cleanup.

La pausa didáctica conserva PAUSED y no se reclama automáticamente aunque reiniciemos el worker. El usuario sigue decidiendo cuándo reanudarla.

### Paso 8 operar el laboratorio con un comando

Desde la carpeta del proyecto, con un motor Docker activo:

```bash
uv run python scripts/stack.py up
```

Abrí [la consola de Compose](http://127.0.0.1:8002). No necesitás dejar dos terminales ejecutando API y worker. La primera construcción descarga las imágenes y dependencias; las siguientes aprovechan las capas.

El script reutiliza el contexto Docker elegido localmente en el incremento PostgreSQL. Si todavía falta configurarlo y el contexto predeterminado no tiene motor, prepará primero la base con `scripts/postgres.py --context NOMBRE_CONTEXTO up`.

Comandos de operación:

```bash
uv run python scripts/stack.py status
uv run python scripts/stack.py logs
uv run python scripts/stack.py down
```

`status` muestra los tres servicios. `logs` muestra las últimas líneas de API/worker sin seguir indefinidamente. `down` detiene la app y conserva el volumen y los archivos de credenciales; `up` la vuelve a preparar.

El comando antiguo `scripts/postgres.py down` rechaza bajar solamente la base si API/worker están activos y explica que corresponde usar `stack.py down`. Evitamos dejar la aplicación reiniciándose mientras se retira su dependencia.

### Paso 9 distinguir los puertos y la historia que ves

| Dirección | Modo |
| --- | --- |
| `127.0.0.1:8000` | Ejemplo web SQLite, si se inició localmente. |
| `127.0.0.1:8001` | API PostgreSQL iniciada en una terminal, si sigue abierta. |
| `127.0.0.1:8002` | API/frontend de Compose. |
| `127.0.0.1:55432` | Conexión PostgreSQL desde el host. |

8001 y 8002 usan el mismo PostgreSQL/schema del laboratorio, por lo que comparten sus runs. SQLite sigue siendo un historial separado.

Para verificar que el trabajo de la app lo ejecuta el worker de Compose, detené cualquier worker adicional del host. Lo hicimos para las pruebas: no queremos que otro consumidor se lleve el job del ensayo de apagado. No es un requisito general del claim: varios workers son válidos, pero cambiarían lo que demuestra ese experimento.

La pantalla no cambió de reglas. Elegí otra revisión o Jenkins no disponible, practicá pausa/recuperación y observá los mismos presupuestos y evaluaciones. En este entorno los tres servicios ya quedaron levantados.

### Paso 10 pruebas reales de contenedores

Las suites anteriores conservan sus dependencias y comandos:

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s integration_tests -v
```

La verificación Docker está separada para no convertir cada prueba PostgreSQL en una prueba de toda la app:

```bash
uv run python scripts/stack.py up
uv run python -m unittest discover -s container_tests -v
```

**Pasaron 126 pruebas habituales, 36 PostgreSQL y 5 de Compose.** Las cinco nuevas verifican:

- Recorridos MCP reales dentro de Linux: coincidencia, otra revisión, informe parcial y denegación adversarial.
- Evidencia pausada que sobrevive a reiniciar API/worker y se recupera sin repetir consultas confirmadas.
- Admisión durable con worker apagado y consumo cuando vuelve.
- UID/capacidades del proceso, credencial privada de runtime, ausencia de datos del host y binario ELF Linux; imagen sin secreto al inspeccionarla sin mounts.
- Parada con SIGTERM durante un run y finalización ordenada del trabajo.

Estas pruebas crean runs sintéticos visibles en el historial PostgreSQL y reinician o detienen los servicios propios del laboratorio. No purgan la base ni eliminan su volumen. Deben ejecutarse sin otro worker que consuma esos mismos jobs.

### Glosario y siguiente incremento

| Término | Significado aquí |
| --- | --- |
| Build context | Archivos enviados al builder; lo reducimos con `.dockerignore`. |
| Multi-stage build | Compilar en una etapa y copiar solo el resultado a la imagen final. |
| Perfil Compose | Selección de servicios opcionales; `app` agrega API/worker. |
| `tmpfs` | Almacenamiento temporal en memoria del contenedor, no historial durable. |
| UID/GID | Identificadores del usuario/grupo que ejecutan la aplicación. |
| SIGTERM | Solicitud de parada que permitimos atender ordenadamente. |
| Drain | Dejar de tomar trabajo nuevo mientras se termina el actual. |

Ahora operamos el flujo con Compose. El siguiente incremento será separar un primer MCP como servicio por Streamable HTTP y definir su identidad verificable, conservando permisos, schemas, presupuestos y recuperación. La identidad humana todavía es de desarrollo y no conectamos un LLM o proveedores DevOps reales en esta fase.
