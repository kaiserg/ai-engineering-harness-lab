# Entorno sin dependencias externas

[Índice](../guide.md) · [← Anterior](01-m0-y-su-limite.md) · [Siguiente →](03-contratos-de-dominio.md)


En M0 usamos Python 3.13, `uv`, `pyproject.toml`, `uv.lock` y `.venv`, inicialmente sin paquetes externos. M2 incorpora el SDK de MCP y sus dependencias en ese mismo lockfile. Las pruebas usan `unittest`, que viene con Python.

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
```

**Decisión:** no agregar FastAPI, Pydantic, LangGraph o un proveedor LLM antes de necesitar su función. M0 debe poder reproducirse sin red ni credenciales una vez disponible Python. No se implementa un cliente HTTP, un protocolo de modelos ni criptografía desde cero.

La carpeta es independiente del laboratorio anterior. No inicializamos Git ni configuramos remoto en esta sesión; `.gitignore` prepara exclusiones para un repositorio futuro.
