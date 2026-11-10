# M0 y su límite

[Índice](../guide.md) · [Siguiente →](02-entorno-sin-dependencias-externas.md)


**Esta sección describe M0 en memoria; la sección M1 al final documenta la persistencia actual.** Construimos una primera ejecución completa con una tool de lectura y un modelo falso. No buscamos una respuesta inteligente: buscamos que las decisiones pasen por controles observables y reproducibles.

```text
QUEUED → RUNNING → COMPLETED
                  ↘ FAILED, si una regla impide continuar
                  ↘ CANCELLED, si cancelamos esta lectura
```

El ciclo y sus decisiones son código de nuestra aplicación. Usar un modelo real más adelante cambiará el adaptador, no quién autoriza las llamadas.

La memoria de M0 desaparece al terminar el proceso. El método `save` no significa «escribir en disco»: guarda una copia dentro de `MemoryRunStore`. No demostramos todavía recuperación tras una caída real. Los estados de aprobación, leases y operaciones inciertas son contratos puros separados del ciclo actual.
