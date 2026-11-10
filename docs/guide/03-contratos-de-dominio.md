# Contratos de dominio

[Índice](../guide.md) · [← Anterior](02-entorno-sin-dependencias-externas.md) · [Siguiente →](04-puertos-y-adaptadores-falsos.md)


En `harness/contracts.py` definimos:

| Tipo | Qué representa |
| --- | --- |
| `Identity` | Actor, workspace, roles y proyectos otorgados por una fuente confiable. |
| `Run` | Una ejecución con UUID, fecha UTC, estado, versión, contadores, resultados y respuesta. |
| `ToolCall` | Propuesta de herramienta, argumentos y correlación `call_id`. |
| `ToolResult` | Resultado correlacionado, datos, referencia de evidencia y versión de schema. |
| `FinalAnswer` | Texto final que propone el adaptador del modelo. |
| `ContractError` | Error de dominio con un código estable y sanitizado. |

Una `dataclass` evita repetir código de inicialización. `frozen=True` impide reasignar sus atributos; no vuelve inmutable el contenido de un diccionario anidado. Por eso copiamos snapshots al cruzar límites y la propuesta de aprobación conserva una representación canónica de sus argumentos.

Los type hints ayudan a leer e implementar interfaces, pero Python no valida automáticamente todos sus tipos en ejecución. No sustituyen un schema de API. En esta fase validamos explícitamente el límite de tool que ejecutamos.

`issue_arguments` admite exactamente `issue_key`, como `DEV-123`, y rechaza campos extra, tipos incorrectos y rutas. No acepta `role`, `workspace`, URL o credenciales del modelo. La forma de la clave no prueba acceso: la política verifica además que el proyecto esté permitido.

`ContractError('FORBIDDEN')` separa una decisión de dominio de los mensajes humanos. El futuro transporte podrá mapear ese código a una respuesta HTTP/MCP sin buscar palabras dentro de una excepción.
