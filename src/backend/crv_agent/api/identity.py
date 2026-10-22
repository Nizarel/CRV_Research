import asyncio
from typing import Protocol
from uuid import UUID

import jwt


class AuthenticationError(ValueError):
    pass


class Identity(Protocol):
    async def owner(self, authorization: str | None) -> str: ...


class LocalIdentity:
    def __init__(self, owner: str) -> None:
        self.value = owner

    async def owner(self, authorization: str | None) -> str:
        return self.value


class EntraIdentity:
    def __init__(self, tenant: str, audience: str) -> None:
        self.tenant = str(UUID(tenant))
        self.audience = audience
        self.issuer = f"https://login.microsoftonline.com/{self.tenant}/v2.0"
        self.keys = jwt.PyJWKClient(f"{self.issuer}/discovery/v2.0/keys")

    async def owner(self, authorization: str | None) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("A bearer access token is required")

        def validate() -> str:
            try:
                claims = jwt.decode(
                    token, self.keys.get_signing_key_from_jwt(token).key,
                    algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                    options={"require": ["exp", "nbf", "iat", "iss", "aud", "tid", "oid"]},
                )
                if str(UUID(claims["tid"])) != self.tenant:
                    raise AuthenticationError("Invalid tenant")
                return f"{self.tenant}:{UUID(claims['oid'])}"
            except (jwt.PyJWTError, ValueError, KeyError, TypeError) as error:
                raise AuthenticationError("Invalid access token") from error

        return await asyncio.to_thread(validate)
