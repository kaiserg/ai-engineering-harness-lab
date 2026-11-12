# 61. Login con OIDC y Keycloak · primer paso: la API acepta tokens

[Índice](../guide.md) · [← Anterior](60-entorno-colima-aislado.md)

Hasta ahora la consola pedía un archivo `hsu_...` emitido por `scripts/users.py`. Era un atajo de laboratorio: el servidor guarda el hash del secreto y comprueba la sesión en cada petición, pero quien tiene acceso al comando se emite cualquier sesión. Un sistema real delega la pregunta «¿quién eres?» a un proveedor de identidad. Este capítulo agrega esa vía con **OIDC** (OpenID Connect) y **Keycloak**, un servidor de identidad de código abierto. **Todavía no cambia la consola**: el login en el navegador es el paso siguiente. Aquí se demuestra que la API verifica tokens reales.

## 1. Quién decide qué

| Pregunta | Quién responde |
| --- | --- |
| ¿Quién es el actor? | Keycloak, mediante un token firmado (JWT). |
| ¿En qué workspace actúa y con qué roles? | PostgreSQL (`user_memberships`), en cada petición. |

El token **no lleva roles ni workspace que el servidor acepte**. Un token con `roles: [approver]` no otorga nada. Se conserva la regla del capítulo 41: la autorización vigente sale de la base (`resolve`), de modo que deshabilitar a alguien surte efecto de inmediato. Como un JWT no se puede revocar como una sesión, su vida es corta (cinco minutos).

El workspace ya no sale de la sesión, porque el token no tiene uno. Con OIDC el cliente debe enviar `x-harness-workspace`; sin él la API responde 403, y la pertenencia en base autoriza el valor. Es una diferencia respecto del capítulo 49, donde el workspace venía de la credencial.

## 2. Piezas

- `harness/oidc.py`: `OidcAuthenticator`, con la misma interfaz que `UserIdentityStore.authenticate`. Los tokens `hsu_` van al directorio local; los JWT, al verificador; un fallo en una vía nunca se reintenta por la otra. Todo lo demás se delega al directorio.
- `harness/web.py`: tres opciones, `--oidc-issuer`, `--oidc-audience` y `--oidc-jwks-url`. Sin ellas el comportamiento es el de siempre. Se exigen las tres y `--user-auth`.
- `keycloak/harness-lab-realm.json`: el realm mínimo, sin usuarios ni secretos. Registro y edición de usuario desactivados, protección contra fuerza bruta, cliente público `harness-console` con PKCE S256 y sin *password grant*.
- `compose.yaml`: servicio `keycloak` (imagen `quay.io/keycloak/keycloak:26.8.2`) bajo el perfil `oidc`, de modo que `stack.py up` no cambia. Escucha solo en `127.0.0.1:8080`.
- `scripts/keycloak.py`: crea la clave del administrador y los usuarios de prueba en `data/runtime` (ignorado por Git), con contraseñas aleatorias.

## 3. Qué se valida en cada token

Algoritmo RS256 (se rechazan `none` y HS256), firma contra las claves públicas de Keycloak (JWKS, con caché), `iss` exacto, `aud` igual a `harness-api`, `exp`, `sub` y `typ=Bearer` (descarta tokens de identidad y de refresco). El actor sale de `preferred_username` y debe cumplir el mismo patrón que los nombres locales. Si Keycloak no responde, la API devuelve 503 `IDENTITY_UNAVAILABLE`, no 401.

Dos detalles de Keycloak que sorprenden: el token no trae `aud` para tu API salvo que un *mapper* lo agregue, y como el realm define sus propios *client scopes*, también hubo que agregar los mappers de `sub` y `preferred_username`. Se descubrieron inspeccionando un token real antes de validar nada.

## 4. Probarlo

```bash
uv run python scripts/keycloak.py init
docker --context colima-harness-lab compose -f compose.yaml --profile oidc up -d keycloak
# esperar ~30 s a que arranque
uv run python scripts/keycloak.py setup
uv run python -m harness.web --storage postgres --user-auth --port 8015 \
  --oidc-issuer http://127.0.0.1:8080/realms/harness-lab --oidc-audience harness-api \
  --oidc-jwks-url http://127.0.0.1:8080/realms/harness-lab/protocol/openid-connect/certs
```

En otra terminal:

```bash
T=$(uv run python scripts/keycloak.py token reader-demo)
curl -i http://127.0.0.1:8015/api/runs -H "Authorization: Bearer $T" -H "x-harness-workspace: workspace-alpha"
```

Resultados verificados: token válido con workspace 200; sin workspace 403; workspace ajeno 403; token alterado 401; sin token 401; `approver-demo` sobre `/api/runs` 403 (no tiene rol de lectura). El `issuer` debe coincidir exactamente con la URL que usa el navegador, por eso se fija `KC_HOSTNAME=http://127.0.0.1:8080`.

Pruebas: `uv run python -m unittest tests.test_oidc` (sin Keycloak: clave RSA local; cubre vencido, `iss`/`aud` erróneos, `alg none`, confusión HS256, ID token, `kid` desconocido, JWKS caído, sin membresía) y, con Keycloak en marcha, `HARNESS_TEST_LIVE_OIDC=1 uv run python -m unittest integration_tests.test_oidc_live`.

## 5. Límites

- Keycloak corre en modo desarrollo con base H2 sin volumen: al recrear el contenedor se pierden usuarios (`rm data/runtime/keycloak-users.json` y repetir `setup`). Mantiene la sesión y el realm solo mientras el contenedor viva.
- El usuario de prueba `harness-cli-test` con *password grant* se crea en tiempo de ejecución para las pruebas; no existe en el realm versionado.
- No hay revocación inmediata de un JWT ni cierre de sesión global; las membresías sí se revisan en cada petición.
- La consola sigue usando archivos `hsu_`. Falta el flujo *authorization code + PKCE* en el navegador, `connect-src` en la CSP para el intercambio del código y el envío de `x-harness-workspace` desde `knowledge.js`.
