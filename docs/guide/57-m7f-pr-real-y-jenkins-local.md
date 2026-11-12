# 57. M7f · Verificar un PR real con Jenkins local

## 1. Preparar dos builds comparables

El ejercicio usa el PR público `octocat/hello-world#1` y un controlador Jenkins dedicado al laboratorio. El job `atlas-tests` trae por Git el head del PR en el build 1 y la rama base en el build 2. En ambos casos comprueba que el checkout contiene el README y archiva `harness-revision.json` con el repositorio, el número de PR y el SHA que devuelve `git rev-parse HEAD`. El job ejecuta una comprobación escrita por el laboratorio sobre esos archivos. El contenido del PR se trata como datos.

Que el segundo build salga verde hace visible el problema: pasó la prueba para otro commit. El artefacto es evidencia emitida por este job de confianza. Administrar el job y su controlador forma parte de la frontera de confianza del ejercicio.

## 2. Limitar la lectura

El perfil queda fijado a actor, workspace, PR, URL local, cuenta y job. El número de build se indica en cada mensaje y se guarda con el turno. El host revalida los permisos locales de repositorio y job. GitHub público se consulta sin token por el MCP existente, y se comprueba que el repositorio siga siendo público antes de reutilizar la evidencia.

El MCP Go consulta dos rutas fijas de Jenkins: la API JSON de un build concreto y su artefacto. Usa `GET`, un límite de respuesta, plazos de red y una URL de loopback. El token corresponde a una cuenta con permiso de lectura sobre ese job. El controlador queda publicado en `127.0.0.1`. Para un controlador remoto haría falta configurar un transporte TLS y su confianza de forma explícita. El token se guarda con permisos privados en `data/runtime/jenkins-ci.json` y se entrega al proceso Go. La huella del perfil identifica el binding sin incluir el token. Rotar el token con los mismos parámetros conserva la huella. Cambiar el binding invalida el perfil anterior hasta restaurar su configuración.

Referencias: [API remota de Jenkins](https://www.jenkins.io/doc/book/using/remote-access-api/) y [autorización por proyecto](https://plugins.jenkins.io/Matrix-Auth/).

## 3. Comparar y recuperar

El planificador determinista lee GitHub, luego Jenkins. El finalizador comprueba que el artefacto corresponde al mismo repositorio y PR, y compara los SHA completos. Publica `VERIFIED` si coinciden y Jenkins reporta éxito, `BUILD_FAILED` si coinciden y el build falló, `STALE_BUILD` si difieren e `INCOMPLETE` si Jenkins no responde o falta evidencia utilizable. Una denegación de acceso o un artefacto inválido cierra con error. El informe verifica CI para la revisión observada. El despliegue y su aprobación tienen su propio flujo.

PostgreSQL conserva el turno, el trabajo y sus checkpoints. Con la pausa marcada, se detiene al guardar el PR. Al recuperar el mismo run, se revalida el acceso y se consulta Jenkins, reutilizando la revisión guardada. En una ejecución normal hay tres decisiones y dos herramientas. El presupuesto admite cuatro decisiones, tres intentos de herramienta y 120 segundos activos para cubrir un intento interrumpido dentro del plazo.

## 4. Repetir el ejercicio

El Jenkins de prueba ya quedó configurado si estás en el entorno del ejercicio. En un entorno nuevo, usá un motor Docker activo. Los comandos usan el contexto exclusivo del laboratorio descrito en el [capítulo 60](60-entorno-colima-aislado.md).

```bash
docker --context colima-harness-lab build -t harness-m7f-jenkins:local examples/jenkins-ci
docker --context colima-harness-lab create --name harness-m7f-jenkins -p 127.0.0.1:18080:8080 \
  --mount type=volume,src=harness-m7f-jenkins-data,dst=/var/jenkins_home \
  -e JAVA_OPTS=-Djenkins.install.runSetupWizard=false harness-m7f-jenkins:local
docker --context colima-harness-lab cp examples/jenkins-ci/fixture.groovy harness-m7f-jenkins:/usr/share/jenkins/ref/init.groovy.d/fixture.groovy
docker --context colima-harness-lab start harness-m7f-jenkins
```

Esperá que Jenkins complete la inicialización y ejecute sus dos builds. Prepará PostgreSQL con el procedimiento habitual y compilá el MCP de Go. Con la identidad de demostración ya inicializada, agregá el permiso local de GitHub y configurá Jenkins por un pipe que entrega el token directamente al configurador.

```bash
uv run python scripts/postgres.py up
uv run python scripts/build_jenkins.py --test
uv run python scripts/github_public.py allow --repository octocat/hello-world
docker --context colima-harness-lab exec harness-m7f-jenkins cat /var/jenkins_home/harness-reader-token \
  | uv run python scripts/jenkins_ci.py --token-stdin
```

El configurador también admite entrada oculta en una terminal, ejecutándolo sin `--token-stdin`. Si ya tenías la API y el worker abiertos, reinicialos en terminales separadas.

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005
uv run python -m harness.worker --user-actor reader-demo --user-workspace workspace-alpha
```

En [Chat](http://127.0.0.1:8005/console#chat), elegí **Investigación de CI · PR público y Jenkins local** y creá una conversación.

1. Enviá `octocat/hello-world#1 build 1`. El informe debe confirmar que el build corresponde al SHA observado del PR.
2. Marcá **Pausar después de la lectura** y enviá `octocat/hello-world#1 build 2`. Recuperá el mismo mensaje. El informe debe mostrar que el segundo build pasó para otro commit.
3. Para repetir la prueba automatizada con un esquema temporal de PostgreSQL, ejecutá `HARNESS_TEST_LIVE_CI=1 uv run python -m unittest integration_tests.test_ci_live`. La prueba también consulta un puerto local sin Jenkins y comprueba que CI queda sin confirmar.

Podés detener el controlador del ejercicio con `docker --context colima-harness-lab stop harness-m7f-jenkins` y arrancarlo nuevamente con `docker --context colima-harness-lab start harness-m7f-jenkins`. Conserva el job, su historial y su cuenta de lectura.
