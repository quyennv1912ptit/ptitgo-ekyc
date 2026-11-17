import os
from dataclasses import dataclass
from typing import Annotated
from uuid import uuid4

from fastapi import Header, Request

from ekyc.core.errors import EkycError, ErrorCode


@dataclass(frozen=True)
class Actor:
    uid: str
    admin: bool


class FirebaseVerifier:
    def __init__(self, project: str):
        import firebase_admin

        if os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
            raise RuntimeError("Firebase Auth emulator is disabled in the main API")
        self.project = project
        self.app = firebase_admin.initialize_app(
            options={"projectId": project, "httpTimeout": 5}, name=f"verification-{uuid4()}"
        )

    def close(self):
        import firebase_admin

        firebase_admin.delete_app(self.app)

    def verify(self, token: str) -> Actor:
        from firebase_admin import auth, exceptions

        try:
            claims = auth.verify_id_token(token, app=self.app, check_revoked=True)
        except (
            auth.InvalidIdTokenError,
            auth.UserDisabledError,
            auth.UserNotFoundError,
            ValueError,
        ):
            raise EkycError(ErrorCode.UNAUTHORIZED) from None
        except exceptions.FirebaseError:
            raise EkycError(ErrorCode.VERIFICATION_UNAVAILABLE) from None

        uid = claims.get("sub")
        if (
            not isinstance(uid, str)
            or not uid
            or len(uid) > 128
            or claims.get("aud") != self.project
            or claims.get("iss") != f"https://securetoken.google.com/{self.project}"
            or claims.get("firebase", {}).get("tenant")
        ):
            raise EkycError(ErrorCode.UNAUTHORIZED)
        return Actor(uid, claims.get("driver_verification_admin") is True)


def authenticated_actor(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Actor:
    scheme, _, token = (authorization or "").partition(" ")
    if (
        scheme.lower() != "bearer"
        or not token
        or len(token) > 8192
        or any(c.isspace() for c in token)
    ):
        raise EkycError(ErrorCode.UNAUTHORIZED)
    verifier = request.app.state.verification_auth
    if verifier is None:
        raise EkycError(ErrorCode.VERIFICATION_UNAVAILABLE)
    return verifier.verify(token)
