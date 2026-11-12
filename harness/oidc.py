"""Autenticación OIDC opcional: el proveedor prueba quién es el actor; los permisos siguen en PostgreSQL."""
import jwt
from jwt import PyJWKClient
from .contracts import ContractError
from .user_identity import TOKEN, name

ALGORITHMS = ['RS256']


class OidcAuthenticator:
    """Misma interfaz que UserIdentityStore.authenticate; delega todo lo demás al directorio local.

    Los tokens locales (`hsu_...`) van al directorio; los JWT, al verificador OIDC. Un fallo en una
    vía nunca se reintenta por la otra. Roles y workspace se leen siempre de la base, no del token.
    """

    def __init__(self, directory, *, issuer, audience, jwks_url=None, jwk_client=None, actor_claim='preferred_username'):
        if not issuer or not audience:
            raise ValueError('OIDC requiere issuer y audience explícitos')
        self.directory = directory
        self.issuer = issuer
        self.audience = audience
        self.actor_claim = actor_claim
        self.jwk_client = jwk_client or PyJWKClient(jwks_url, cache_keys=False, lifespan=300, timeout=5)  # sin caché por kid: una clave retirada deja de valer al refrescar el JWKS

    def __getattr__(self, attribute):
        if attribute == 'directory':  # evita recursión en copy/pickle antes de __init__
            raise AttributeError(attribute)
        return getattr(self.directory, attribute)

    def authenticate(self, authorization, requested_workspace=None):
        if type(authorization) is not str or not authorization.startswith('Bearer '):
            raise ContractError('UNAUTHENTICATED')
        token = authorization[7:]
        if TOKEN.fullmatch(token):
            return self.directory.authenticate(authorization, requested_workspace)
        if token.count('.') != 2 or len(token) > 8192:
            raise ContractError('UNAUTHENTICATED')
        # El JWT no trae workspace: se exige explícito y la pertenencia en base lo autoriza.
        if requested_workspace is None:
            raise ContractError('FORBIDDEN')
        try:
            name(requested_workspace)
        except ContractError as error:
            raise ContractError('FORBIDDEN') from error
        actor = self._actor(token)
        return self.directory.resolve(actor, requested_workspace)

    def _actor(self, token):
        try:
            header = jwt.get_unverified_header(token)
            if header.get('alg') not in ALGORITHMS:
                raise ContractError('UNAUTHENTICATED')
            key = self.jwk_client.get_signing_key_from_jwt(token).key
        except ContractError:
            raise
        except jwt.PyJWKClientConnectionError as error:
            raise ContractError('IDENTITY_UNAVAILABLE') from error
        except jwt.PyJWTError as error:
            raise ContractError('UNAUTHENTICATED') from error
        try:
            claims = jwt.decode(token, key, algorithms=ALGORITHMS, issuer=self.issuer, audience=self.audience,
                options={'require': ['exp', 'iss', 'aud', 'sub']})
        except jwt.PyJWTError as error:
            raise ContractError('UNAUTHENTICATED') from error
        if claims.get('typ') != 'Bearer':  # rechaza ID tokens y refresh tokens
            raise ContractError('UNAUTHENTICATED')
        value = claims.get(self.actor_claim)
        try:
            return name(value)
        except ContractError as error:
            raise ContractError('UNAUTHENTICATED') from error
