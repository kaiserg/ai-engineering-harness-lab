# M4a propuestas y aprobaciones persistidas

[Índice](../guide.md) · [← Anterior](30-evaluaciones-deterministas-del-recorrido.md) · [Siguiente →](32-m4b-ledger-y-despacho-jenkins-simulado.md)


### Paso 1 delimitar este incremento

Ahora una propuesta y su decisión sobreviven al cierre del proceso. Podemos crear, consultar, aprobar y rechazar. **Todavía no solicitamos builds a Jenkins**, ni creamos operaciones en un ledger, ni reencolamos un run por aprobar. Esas funciones serán el siguiente incremento de M4.

El recorrido de lectura conserva sus estados, presupuestos y recuperación. Una lectura COMPLETED puede originar una propuesta separada; no cambiamos retrospectivamente ese run a AWAITING_APPROVAL. Esto evita introducir un estado de escritura en el worker antes de construir su contrato de ejecución.

Agregamos tres piezas:

- `harness/proposals.py`: autorización, snapshot, idempotencia y decisiones transaccionales.
- `harness/migrations/proposals/001_proposals.sql`: tabla, índices/restricciones y protección de inmutabilidad.
- `scripts/proposals.py`: CLI local educativa con identidades de desarrollo controladas.

Las propuestas usan PostgreSQL, que ya sostiene el trabajo durable de la etapa 2. No agregamos otro backend SQLite para este módulo. Los tests y evals de lectura con SQLite continúan funcionando.

### Paso 2 preparar evidencia antes de proponer

Con PostgreSQL activo:

```bash
uv sync --locked
uv run python scripts/build_jenkins.py
# Si la app/BD todavía no está levantada:
uv run python scripts/stack.py up
uv run python scripts/proposals.py prepare
```

`prepare` ejecuta el mismo core/adaptadores MCP con fixtures. Guarda directamente un run de lectura en PostgreSQL; no lo encola a la vez para otro worker. Usa el escenario con build desactualizado: el PR tiene `revision-A`, aunque el build disponible sea de `revision-B`.

La salida contiene `run_id` y un `example_request_key` UUID. Son datos sintéticos, no credenciales. Copiá ambos para crear la propuesta:

```bash
uv run python scripts/proposals.py create --run-id RUN_ID --key REQUEST_KEY
```

Reemplazá los dos valores por los que imprimió `prepare`. Esta CLI también puede proponer desde otro run PostgreSQL del mismo actor, completado y con evidencia CI válida. No recibe una URL, script, rol o job arbitrarios del modelo/cliente.

El store exige rol `operator`, pertenencia al workspace, propiedad del run y permisos vigentes de proyecto/repositorio/job. Reautoriza sus resultados usando la política de lectura existente y vuelve a comprobar los vínculos mediante el finalizador determinista. Un run incompleto o ajeno no habilita una propuesta.

### Paso 3 guardar exactamente lo que se aprueba

El único binding de acción de este incremento es `jenkins_request_tests`, recurso `atlas-tests`, repositorio `demo/atlas`. La revisión sale del head del PR confirmado, **no del build viejo**.

El JSON canónico incluye:

- Actor/workspace y herramienta/recurso.
- Argumentos exactos: job, repositorio y revisión.
- Vencimiento a treinta minutos, calculado con el reloj de PostgreSQL.
- ID/versión del run, fuentes y hash del snapshot de evidencia y fallos.
- Versiones de política y configuración usadas al proponer.

`payload_hash` es SHA-256 del JSON completo y se calcula en el servidor. No es una contraseña ni una firma: sirve para vincular la decisión al contenido exacto. La autorización sigue dependiendo de identidad, roles, recursos y estado.

No existe un endpoint para editar argumentos. El trigger de PostgreSQL también bloquea cambios del payload y transiciones inválidas: una actualización accidental no debe modificar lo aprobado. El dueño administrativo de la base sigue siendo una frontera de confianza, no un atacante del que ese trigger pueda protegernos.

### Paso 4 repetir creación sin duplicar intención

`--key` es una clave de idempotencia elegida una vez por el cliente. Ante timeout o pérdida de respuesta, repetí el mismo comando con **la misma key y el mismo run**:

```bash
uv run python scripts/proposals.py create --run-id RUN_ID --key REQUEST_KEY
```

Devuelve el mismo ID, hash y vencimiento, aunque el proceso que creó la propuesta ya haya terminado. La misma key con otro run devuelve CONFLICT. Otra key representa otra intención: no afirmamos que dos propuestas distintas se conviertan automáticamente en un único efecto Jenkins.

La unicidad está acotada por actor/workspace/key. Un lock transaccional serializa solicitudes con esa key y una restricción UNIQUE refuerza la garantía. Las solicitudes diferentes no comparten un lock global.

El vencimiento no se renueva al repetir la solicitud: idempotencia devuelve el recurso existente. Perder la respuesta no es motivo para darle otros treinta minutos.

### Paso 5 revisar y aprobar o rechazar

La creación imprime `proposal_id`, `payload_hash`, `version: 0` y el payload. Revisá el contenido con la identidad del aprobador:

```bash
uv run python scripts/proposals.py show PROPOSAL_ID --actor approver
```

Después decidí sobre ese hash y esa versión:

```bash
uv run python scripts/proposals.py decide PROPOSAL_ID --hash PAYLOAD_HASH --version 0 --decision approve
# Para una propuesta aún pendiente, la alternativa es:
uv run python scripts/proposals.py decide PROPOSAL_ID --hash PAYLOAD_HASH --version 0 --decision reject
```

No ejecutes ambas decisiones esperando cambiar la primera. Estados de este módulo:

```text
PENDING_APPROVAL (v0) → APPROVED (v1)
                     → REJECTED (v1)
                     → EXPIRED (v1, al intentar decidir después del vencimiento)
```

La decisión guarda `decided_by` y `decided_at`, conserva el payload y aumenta la versión. Comprueba rol `approver`, workspace y recursos vigentes; impide que el solicitante se apruebe o rechace a sí mismo. La consulta también verifica acceso: saber el UUID no otorga permiso.

Cada decisión bloquea su fila (`FOR UPDATE`) y exige la versión esperada. Dos aprobadores o decisiones simultáneas no pueden sobrescribirse silenciosamente. El contrato puro `Proposal.approve` conserva las guardas de hash/versión/vencimiento/separación de actores, también utilizadas al validar un rechazo.

### Paso 6 recuperar una respuesta perdida

Si se perdió la respuesta del comando `decide`, repetí **el mismo actor, decisión, hash y versión original 0**. No cambies la versión a 1 para reintentar el mismo acto. La decisión ya confirmada se devuelve sin registrar otra transición.

Una decisión contraria, otro aprobador que intenta reclamarla o un hash/versión diferente dan conflicto. Rechazar no borra la propuesta ni habilita aprobar después. Para cambiar el contenido necesitaremos otra propuesta; la antigua nunca se edita.

En la CLI cada comando ya es un proceso nuevo: `show`, repetir `create` y repetir `decide` no dependen de memoria de sesión. La transacción guarda todo o nada. Si falla antes del commit, el reintento puede completar; si se confirmó y se perdió la respuesta, devuelve lo persistido.

No cambiamos el mecanismo de recuperación de los jobs de lectura ni sus leases/fencing. Tampoco suponemos que idempotencia de esta API local deduplique Jenkins: todavía no hubo ninguna llamada de escritura.

### Paso 7 tratar expiración y vigencia futura

El reloj autoritativo es PostgreSQL, no una fecha enviada por el cliente. `effective_status` muestra EXPIRED cuando la autorización pendiente/aprobada ya venció, aunque el `status` histórico todavía sea PENDING_APPROVAL o APPROVED. No hace falta un job periódico para impedir una decisión tardía.

Si se intenta decidir una pendiente vencida, persistimos EXPIRED y luego devolvemos APPROVAL_EXPIRED. El error se emite **después** de confirmar la transacción, para no deshacer el registro de expiración. Una aprobación ya vencida tampoco vuelve a ser válida al repetir el comando. Un rechazo es terminal y no adquiere autorización con el tiempo.

**Aprobación válida no significa ejecución autorizada para siempre.** Antes del futuro despacho habrá que comprobar vencimiento, permisos, revisión actual y precondiciones, además de consumir la aprobación en una operación única. Si cambió el PR, no alcanza con que el payload histórico tenga un hash correcto. S06 completo se cerrará con ese despacho y su prueba de cambio de revisión.

### Paso 8 comprender las identidades de esta CLI

`fixture_identity` define dos actores de desarrollo:

- `reader-demo` actuando como lector/operador para proponer.
- `approver-demo` actuando como aprobador de los recursos sintéticos.

Los roles no vienen de un prompt ni de una cabecera del navegador. Esta CLI local elige fixtures explícitos para practicar la separación. La misma persona con acceso al host puede ejecutar ambos comandos: esto **no es autenticación de dos personas**. La identidad real de usuarios/OIDC sigue pendiente. No exponemos estos comandos de cambio de identidad en el frontend ni como API pública.

### Paso 9 migración compatible con la app actual

`ProposalStore` usa la misma conexión/schema y transacciones que `PostgresRunStore`. Tiene un registro separado `proposal_schema_version=1`: instala su tabla una vez, con lock de migración, y rechaza versiones desconocidas.

Esto permite agregar el módulo sin subir de golpe la versión de las tablas de runs/jobs que entienden los workers de lectura ya desplegados. La CLI aplica la migración al abrir el módulo; una copia nueva del repositorio no necesita un dump de propuestas. No modifica presupuestos, run IDs, evidencia existente ni jobs pendientes.

Este incremento vive en la CLI del host. No requiere reiniciar los workers actuales. Un futuro build de la imagen incorporará los archivos nuevos, pero la app aún no expone endpoints ni pantalla de propuestas.

### Paso 10 pruebas y límites

```bash
uv run python -m unittest discover -s integration_tests -p test_proposals.py -v
uv run python -m unittest discover -s integration_tests -v
uv run python -m unittest discover -s tests -v
```

Las pruebas usan schemas PostgreSQL aleatorios y eliminan solo el propio. Cubren persistencia al reconstruir el store, claves repetidas, respuesta perdida, decisiones en carrera, autoaprobación, roles/recursos/workspace, hash/versión incorrectos, payload SQL inmutable, expiración persistida y conservación del run original. Comprueban que proponer/aprobar no crea un job.

El siguiente incremento de M4 conectará una aprobación vigente con un ledger y un despacho Jenkins simulado. Hasta entonces APPROVED significa únicamente «esta propuesta recibió esta decisión», no «se solicitaron pruebas» ni «las pruebas pasaron».

Validación M4a: **190 pruebas locales, 53 PostgreSQL (10 nuevas de propuestas) y 9/9 evaluaciones deterministas aprobadas**. Además verificamos la CLI en procesos separados: crear/repetir, aprobar/repetir y consultar conservaron el mismo recurso y decisión. La prueba de concurrencia crea desde cero con la misma key y comprueba una sola nueva fila. Los archivos modificados pasaron Gitleaks sin hallazgos y los reportes locales siguen ignorados.
