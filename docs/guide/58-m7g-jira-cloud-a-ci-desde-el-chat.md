# 58. M7g · Del ticket de Jira Cloud al build de Jenkins

[Índice](../guide.md) · [← Anterior](57-m7f-pr-real-y-jenkins-local.md) · [Siguiente →](59-rag-agentico-minimo.md)

## 1. Preparar la evidencia

Usá un ticket en un proyecto Jira autorizado y agregá el enlace directo al PR en su **descripción**. Para el ejercicio M7f, el enlace es `https://github.com/octocat/Hello-World/pull/1`. Podés pegarlo como URL o insertarlo como hipervínculo o tarjeta de Jira. Conservá un solo PR distinto en la descripción. La deduplicación cuenta una URL repetida como un único PR.

La conversación fija la conexión de Jira, el actor y workspace, el PR público y el job de Jenkins de M7f. El repositorio, el número de PR y el job permitido vienen de la configuración local. La pregunta indica el ticket y el número de build. Ese número se guarda con el turno y permite volver a consultar la misma ejecución de Jenkins.

## 2. Seguir el enlace con tres lecturas

1. **Jira Cloud.** El worker pide el ticket por MCP con la identidad autorizada. El servidor extrae referencias estructuradas de la descripción ADF. En un hipervínculo lee el destino real; en texto sin hipervínculo reconoce URLs directas a PRs de GitHub. La descripción se trata como datos. El planificador solo continúa cuando hay un PR inequívoco y coincide con el configurado.
2. **GitHub.** El MCP público recupera el SHA del PR vinculado. La petición se construye con el repositorio y número validados, sin abrir directamente enlaces tomados del ticket.
3. **Jenkins.** El MCP de Go lee el build indicado en el job autorizado. El finalizador de M7f verifica que el artefacto pertenece al mismo PR y compara el SHA ejecutado con el SHA observado en GitHub. El informe cita las tres fuentes.

| Evidencia | Resultado |
| --- | --- |
| Build exitoso para el mismo SHA | `VERIFIED` |
| Build de otro SHA, aunque haya pasado | `STALE_BUILD` |
| Build fallido para el mismo SHA | `BUILD_FAILED` |
| Jenkins inaccesible o sin evidencia utilizable | `INCOMPLETE` |
| Vínculo ausente, ambiguo o fuera de alcance | El informe pide corregir el vínculo y deja CI sin confirmar. El turno termina tras Jira. |

El informe verifica CI para un SHA observado. Los permisos de Jira, GitHub y Jenkins son de lectura. La aprobación de despliegues se gestiona en otro flujo.

## 3. Recuperar un turno pausado

El chat admite el turno y su trabajo en la misma transacción. El worker guarda cada resultado antes de pasar al siguiente proveedor y mantiene los leases y límites del harness. En una ejecución normal hay cuatro decisiones deterministas y tres herramientas. El presupuesto permite cinco decisiones, cuatro intentos de herramienta y 120 segundos activos.

Si marcás la pausa, el turno se detiene después de confirmar Jira. Al recuperarlo, el worker revalida el permiso sobre el ticket y reutiliza el vínculo guardado antes de consultar GitHub y Jenkins. La comprobación de acceso es independiente de la lectura que obtuvo la evidencia. Si editás el vínculo y querés evaluar la nueva versión, enviá un turno nuevo.

## 4. Probarlo desde la consola

Usá la conexión Jira ya preparada en el [capítulo 54](54-m7c-jira-desde-el-chat.md) y el Jenkins del [capítulo 57](57-m7f-pr-real-y-jenkins-local.md). Confirmá que el actor local tiene permiso para el proyecto de Jira, el repositorio público y el job configurado. Reiniciá la API y el worker si estaban ejecutando una versión anterior.

En [Chat](http://127.0.0.1:8005/console#chat), creá una conversación en **Investigación de CI · Jira Cloud → GitHub → Jenkins**. Reemplazá `PROY-123` por la clave de tu ticket:

1. Enviá «¿Pasó el build 1 de PROY-123?». Con los builds del ejercicio, debe confirmar el SHA del PR.
2. Marcá **Pausar después de la lectura** y enviá «¿Pasó el build 2 de PROY-123?». Recuperá el turno. Debe conservar el identificador y explicar que el build verde ejecutó otro commit.

La prueba local cubre vínculos ausentes, ambiguos y fuera de alcance. La integración opt-in usa un esquema PostgreSQL temporal, el ticket vinculado, GitHub público y Jenkins local:

```bash
uv run python -m unittest tests.test_ci_jira
HARNESS_TEST_JIRA_CI_ISSUE=PROY-123 uv run python -m unittest integration_tests.test_ci_jira
```
