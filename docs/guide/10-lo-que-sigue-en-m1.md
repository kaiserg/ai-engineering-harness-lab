# Lo que sigue en M1

[Índice](../guide.md) · [← Anterior](09-pruebas-y-limites-pendientes.md) · [Siguiente →](11-m1-guardar-y-recuperar-un-run-con-sqlite.md)


El próximo incremento previsto al cerrar M0 era un `SQLiteRunStore` con el mismo contrato; ya se implementó en la sección M1 siguiente. Guardaremos estado, contadores y resultados de lectura, provocaremos una interrupción en un punto conocido y retomaremos sin empezar de cero. Primero definiremos la transición de recuperación y sus pruebas; no añadiremos SQLite esperando que resuelva por sí solo el flujo.

Después vendrán deadline global y mejores contratos de herramientas/evidencia. MCP, Go, API, PostgreSQL, autenticación y acciones aprobadas conservan sus hitos del diseño. No necesitamos incorporarlos todos para explicar y verificar este primer recorrido.
