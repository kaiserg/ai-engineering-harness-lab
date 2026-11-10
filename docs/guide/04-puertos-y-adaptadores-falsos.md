# Puertos y adaptadores falsos

[Índice](../guide.md) · [← Anterior](03-contratos-de-dominio.md) · [Siguiente →](05-store-y-control-de-version.md)


Un **puerto** es la interfaz que necesita el core; un **adaptador** es su implementación concreta. Usamos `typing.Protocol`: define métodos esperados sin obligar a heredar de una clase base ni añadir frameworks.

- `LLMPort.decide` recibe un snapshot y capacidades visibles; devuelve una llamada o respuesta final.
- `PolicyPort` determina visibilidad y autoriza una llamada concreta.
- `ToolPort.invoke` ejecuta una herramienta con identidad separada de los argumentos.
- `RunStore` crea, carga y guarda una ejecución.

`ScriptedLLM` propone consultar `DEV-123`; después construye una respuesta desde el resultado. No predice tokens, no interpreta lenguaje natural y no tiene memoria privada de pasos: se guía por el snapshot del run. Su constructor permite proponer una herramienta prohibida para comprobar que el core no depende del buen comportamiento del modelo.

`FakeJira` devuelve un ticket de `workspace-alpha` y revalida la política antes de registrar una invocación. Es una segunda barrera educativa, no un servidor MCP. El contador de invocaciones permite comprobar si una denegación realmente evitó alcanzar el adaptador.

La identidad del demo se construye en el punto de entrada, fuera de la decisión del modelo. **No hay autenticación real**: quien ejecute el código puede editar esa fixture. Su propósito es probar aislamiento y contratos, no proteger una API pública.
