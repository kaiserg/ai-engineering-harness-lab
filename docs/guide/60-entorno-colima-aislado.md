# 60. Arrancar y apagar un entorno Colima exclusivo

El perfil `harness-lab` mantiene los contenedores y volúmenes del laboratorio en una máquina virtual propia. Su contexto Docker es `colima-harness-lab`. Los scripts del proyecto recuerdan ese contexto en `data/runtime/docker-context`. La opción `--activate=false` conserva el contexto Docker global que usás en otros proyectos.

## Encender

Desde la raíz del laboratorio:

```bash
colima start --profile harness-lab --activate=false
uv run python scripts/stack.py up
```

Para inicializar un entorno nuevo por primera vez, indicá el contexto al preparar PostgreSQL. Los arranques siguientes usarán el contexto guardado:

```bash
uv run python scripts/postgres.py --context colima-harness-lab up
```

El Jenkins del ejercicio M7f se arranca por separado:

```bash
docker --context colima-harness-lab start harness-m7f-jenkins
```

## Apagar

El comando de la app detiene también su planificador local. Después detené Jenkins, si lo estabas usando, y apagá la máquina virtual del laboratorio:

```bash
uv run python scripts/stack.py down
docker --context colima-harness-lab stop harness-m7f-jenkins
colima stop --profile harness-lab
```

Las bases, los índices, los dashboards y el historial de Jenkins se conservan en volúmenes. Jaeger tiene un volumen nombrado para conservar su directorio de datos al recrear el contenedor. La persistencia de las trazas también depende del backend de almacenamiento configurado en Jaeger.

Si levantaste la API o workers directamente en terminales del host, detenelos con Ctrl+C en esas terminales. Ollama se administra desde su aplicación y puede seguir activo para otros usos.

## Comprobar el estado

```bash
colima list
docker --context colima-harness-lab ps
```

El segundo comando requiere que la máquina virtual esté encendida. Para ver contenedores detenidos, usá `ps -a`. Los respaldos privados de una migración local se guardan dentro de `data/runtime`, que está excluido de Git.
