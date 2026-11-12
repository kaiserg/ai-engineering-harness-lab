# 62. Login con OIDC y Keycloak · segundo paso: el botón de la consola

[Índice](../guide.md) · [← Anterior](61-oidc-con-keycloak-primer-paso.md)

El capítulo 61 hizo que la API acepte tokens de Keycloak. Aquí la consola gana un botón **Entrar con Keycloak** que reemplaza el comando `scripts/users.py issue` y la búsqueda del archivo. La opción del archivo sigue disponible.

## 1. El flujo, paso a paso

Es el flujo estándar de una aplicación de navegador: **código de autorización con PKCE**.

1. La consola pide `/oidc-config` (público, sin secretos) y, si OIDC está activo, muestra el botón.
2. Al pulsarlo, el navegador genera un `code_verifier` aleatorio, calcula su hash SHA-256 (`code_challenge`) y un `state`, guarda `verifier` y `state` en `sessionStorage` y redirige a Keycloak.
3. Keycloak muestra su pantalla de login. **La contraseña la ve Keycloak, nunca la consola ni el laboratorio.**
4. Keycloak redirige a `/console?code=...&state=...`.
5. `oidc.js` compara `state` con el guardado, borra la query de la URL y hace un `POST` al endpoint de tokens con el `code` y el `code_verifier`. Keycloak comprueba que el hash coincida con el `code_challenge` del paso 2: si alguien robó el `code` en el camino, no le sirve sin el verifier.
6. El `access_token` queda **solo en memoria** del módulo, igual que el token `hsu_`. Cada petición añade `Authorization` y `X-Harness-Workspace`.

PKCE existe porque una aplicación de navegador es un cliente *público*: no puede guardar un secreto. Por eso el cliente `harness-console` no tiene `client_secret`, y el realm exige PKCE S256 y desactiva el *password grant*.

## 2. Cambios

- `harness/web.py`: `GET /oidc-config` (datos públicos de endpoints y cliente) y las opciones `--oidc-client-id` y `--oidc-workspace`. La política de seguridad de contenido (CSP) añade el origen de Keycloak a `connect-src`, solo cuando OIDC está activo, porque el navegador hace un `fetch` al endpoint de tokens. La redirección de la página no la restringe la CSP.
- `frontend/oidc.js`: el flujo PKCE. `frontend/knowledge.js`: `connectToken` y el encabezado `X-Harness-Workspace`; el flujo del archivo se refactorizó para compartir `connect` sin cambiar su comportamiento. `frontend/console.html`: el botón.
- `frontend_tests/oidc.test.mjs`: el desafío S256 coincide con el vector de la RFC 7636 y el verifier es aleatorio. La suite completa: `node --test frontend_tests/*.mjs`.

## 3. Probarlo

Reiniciá la API de la consola con las opciones OIDC (Keycloak ya en marcha según el capítulo 61):

```bash
uv run python -m harness.web --storage postgres --user-auth --port 8005 \
  --oidc-issuer http://127.0.0.1:8080/realms/harness-lab --oidc-audience harness-api \
  --oidc-jwks-url http://127.0.0.1:8080/realms/harness-lab/protocol/openid-connect/certs
```

Abrí http://127.0.0.1:8005/console y pulsá **Entrar con Keycloak**. Las credenciales de `reader-demo` y `approver-demo` están en `data/runtime/keycloak-users.json`. Para probar la aprobación por otro usuario, cerrá la sesión de Keycloak (`http://127.0.0.1:8080/realms/harness-lab/account`) o usá una ventana privada con el otro usuario.

Verificado por script (no en un navegador): autorización con PKCE, login, intercambio del código con el encabezado `Origin` de la consola (Keycloak responde con `Access-Control-Allow-Origin` correcto), `GET /api/me` con el token y el workspace devuelve 200 y reutilizar el mismo código se rechaza con 400.

## 4. Límites

- **El workspace es único por servidor** (`--oidc-workspace`, por defecto `workspace-alpha`): el token no lo trae. La pertenencia en base sigue autorizándolo, pero la consola no permite elegir entre varios. Un selector alimentado por las membresías reales del actor sería el siguiente paso.
- **El token dura cinco minutos y no hay renovación silenciosa.** Al vencer, la API responde 401, la consola limpia la sesión y hay que pulsar el botón otra vez; mientras la sesión de Keycloak siga viva, no pide contraseña de nuevo. Un *refresh token* con rotación sería la mejora natural.
- **Salir de la consola no cierra la sesión de Keycloak.**
- Keycloak sigue en modo desarrollo, sin volumen, y la comunicación es HTTP en loopback. En un despliegue real irían TLS, una base de datos persistente y un dominio propio.
- La prueba en un navegador real queda como verificación manual; solo se automatizó la parte de protocolo y la criptografía del desafío.
