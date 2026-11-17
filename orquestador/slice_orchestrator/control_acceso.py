"""Control de Acceso (R1C) — autenticación mínima con contraseña + tokens
firmados estilo JWT, y autorización por rol (admin / usuario).

Deliberadamente sin dependencias externas (ni PyJWT ni bcrypt): todo con la
librería estándar de Python, para no depender de que el headnode tenga
acceso a internet o permisos para instalar paquetes nuevos justo antes de
la presentación.

Los usuarios (nombre, hash de su contraseña, y rol) se guardan en un
archivo JSON (por defecto ``usuarios_api.json``, configurable con la
variable de entorno ``ORQ_USUARIOS_API``). Las contraseñas NUNCA se
guardan en texto plano: se guarda solo una sal aleatoria por usuario y el
hash PBKDF2-HMAC-SHA256 de "sal + contraseña".

Los tokens son JWT mínimos hechos a mano (header.payload.firma, todo en
base64url, firmados con HMAC-SHA256) usando un secreto que NUNCA vive en
este código: se lee de la variable de entorno ``ORQ_API_SECRET``. Si esa
variable no está definida, el servicio rehúsa arrancar — por diseño, para
que nunca haya un secreto por defecto "de fábrica".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

ITERACIONES_PBKDF2 = 200_000
ROLES_VALIDOS = ("admin", "usuario")
TTL_TOKEN_SEG_DEFAULT = 3600


class ErrorAcceso(Exception):
    """Base común: credenciales inválidas, token inválido/expirado, o rol
    insuficiente. Capturar esta clase basta para cualquiera de los tres
    casos; para distinguir el código HTTP exacto, ver las subclases."""


class CredencialesInvalidas(ErrorAcceso):
    """Usuario/contraseña incorrectos en /login, o token ausente/inválido/
    expirado en cualquier otra ruta -- en HTTP, ambos son 401."""


class PermisoInsuficiente(ErrorAcceso):
    """El usuario se autenticó bien, pero su rol no alcanza para la
    operación pedida -- en HTTP, 403."""


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(texto: str) -> bytes:
    relleno = "=" * (-len(texto) % 4)
    return base64.urlsafe_b64decode(texto + relleno)


def hash_password(password: str, sal_hex: str | None = None) -> tuple[str, str]:
    """Devuelve (sal_hex, hash_hex). Genera una sal nueva si no se pasa una."""
    sal = bytes.fromhex(sal_hex) if sal_hex else os.urandom(16)
    derivado = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), sal,
                                    ITERACIONES_PBKDF2)
    return sal.hex(), derivado.hex()


def verificar_password(password: str, sal_hex: str, hash_hex: str) -> bool:
    _, calculado = hash_password(password, sal_hex)
    return hmac.compare_digest(calculado, hash_hex)


def _secreto_api() -> str:
    secreto = os.environ.get("ORQ_API_SECRET")
    if not secreto:
        raise ErrorAcceso(
            "Falta configurar la variable de entorno ORQ_API_SECRET "
            "(un secreto propio, largo y aleatorio, nunca de fábrica)")
    return secreto


def generar_token(usuario: str, rol: str, ttl_seg: int = TTL_TOKEN_SEG_DEFAULT) -> str:
    secreto = _secreto_api()
    header = _b64u_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64u_encode(json.dumps({
        "sub": usuario, "rol": rol, "exp": int(time.time()) + ttl_seg,
    }).encode())
    firma = hmac.new(secreto.encode("utf-8"), f"{header}.{payload}".encode(),
                      hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64u_encode(firma)}"


def verificar_token(token: str) -> dict[str, Any]:
    secreto = _secreto_api()
    try:
        header, payload, firma = token.split(".")
    except ValueError:
        raise CredencialesInvalidas("Token con formato inválido") from None
    firma_esperada = hmac.new(secreto.encode("utf-8"), f"{header}.{payload}".encode(),
                               hashlib.sha256).digest()
    if not hmac.compare_digest(_b64u_decode(firma), firma_esperada):
        raise CredencialesInvalidas("Token inválido (firma no coincide)")
    try:
        datos = json.loads(_b64u_decode(payload))
    except (ValueError, json.JSONDecodeError):
        raise CredencialesInvalidas("Token con formato inválido") from None
    if datos.get("exp", 0) < time.time():
        raise CredencialesInvalidas("Token expirado")
    return {"usuario": datos.get("sub"), "rol": datos.get("rol")}


def exigir_rol(sesion: dict[str, Any], *roles_permitidos: str) -> None:
    if sesion.get("rol") not in roles_permitidos:
        raise PermisoInsuficiente(
            f"Rol '{sesion.get('rol')}' sin permiso para esta operación "
            f"(se necesita: {', '.join(roles_permitidos)})")


class GestorUsuarios:
    """Carga/guarda el archivo JSON de usuarios y valida credenciales."""

    def __init__(self, ruta: str | os.PathLike | None = None) -> None:
        self._ruta = Path(ruta or os.environ.get("ORQ_USUARIOS_API", "usuarios_api.json"))

    def _cargar(self) -> dict[str, dict[str, str]]:
        if not self._ruta.exists():
            return {}
        with open(self._ruta, "r", encoding="utf-8") as f:
            return json.load(f)

    def _guardar(self, datos: dict[str, dict[str, str]]) -> None:
        tmp = self._ruta.with_suffix(self._ruta.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
        tmp.replace(self._ruta)

    def listar(self) -> list[dict[str, str]]:
        return [{"usuario": u, "rol": d["rol"]} for u, d in self._cargar().items()]

    def existe(self, usuario: str) -> bool:
        return usuario in self._cargar()

    def agregar(self, usuario: str, password: str, rol: str) -> None:
        if rol not in ROLES_VALIDOS:
            raise ErrorAcceso(f"Rol '{rol}' inválido. Use uno de: {', '.join(ROLES_VALIDOS)}")
        if not usuario or not password:
            raise ErrorAcceso("Usuario y contraseña no pueden estar vacíos")
        datos = self._cargar()
        sal_hex, hash_hex = hash_password(password)
        datos[usuario] = {"sal": sal_hex, "hash": hash_hex, "rol": rol}
        self._guardar(datos)

    def eliminar(self, usuario: str) -> None:
        datos = self._cargar()
        if usuario not in datos:
            raise ErrorAcceso(f"El usuario '{usuario}' no existe")
        del datos[usuario]
        self._guardar(datos)

    def autenticar(self, usuario: str, password: str) -> str:
        """Devuelve el rol si las credenciales son correctas; si no, lanza ErrorAcceso."""
        datos = self._cargar()
        entrada = datos.get(usuario)
        # Siempre se calcula el hash (con una sal fija de relleno si el usuario
        # no existe) para no filtrar por temporización si el usuario existe o no.
        sal_hex = entrada["sal"] if entrada else "00" * 16
        hash_hex = entrada["hash"] if entrada else "00" * 32
        ok = verificar_password(password, sal_hex, hash_hex)
        if not entrada or not ok:
            raise CredencialesInvalidas("Usuario o contraseña incorrectos")
        return entrada["rol"]
