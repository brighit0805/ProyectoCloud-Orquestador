"""API HTTP mínima para R1C (Control de Acceso) sobre ServicioSlices.

Deliberadamente construida solo con la librería estándar de Python
(``http.server``), sin Flask/FastAPI, para no depender de instalar
paquetes nuevos en el headnode justo antes de la presentación.

Endpoints:
    POST /login                              {usuario, contrasena} -> {token, rol}
    GET  /slices                             (propios; admin + ?todos=1 -> de todos)
    POST /slices                             crea (y opcionalmente despliega) un slice
    GET  /slices/<id>                        detalle (admin puede ver cualquiera)
    DELETE /slices/<id>                      elimina (admin puede eliminar cualquiera)
    GET  /recursos                           consumo de recursos (propio)
    POST /mantenimiento/limpiar-residuos     SOLO admin

Todas las rutas salvo /login exigen el header ``Authorization: Bearer <token>``.

Arranque:
    python3 -m slice_orchestrator.api_http --driver linux --puerto 8080
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .control_acceso import (
    CredencialesInvalidas,
    ErrorAcceso,
    GestorUsuarios,
    PermisoInsuficiente,
    exigir_rol,
    generar_token,
    verificar_token,
)
from .servicio_slices import ErrorServicio, NoEncontrado, ServicioSlices

logger = logging.getLogger(__name__)

_svc: ServicioSlices | None = None
_gestor: GestorUsuarios | None = None


def _construir_servicio(driver_nombre: str, ruta_estado: str | None) -> ServicioSlices:
    ruta_estado = ruta_estado or "estado_r1b.db"
    if driver_nombre == "openstack":
        # Reusa exactamente la misma función que ya usa la CLI de R1B para
        # este camino (ver cli.py), así la API y la CLI nunca se desalinean
        # en cómo arman el driver real.
        from .cli import _construir_servicio_openstack_real
        return _construir_servicio_openstack_real(ruta_estado)
    return ServicioSlices(ruta_estado=ruta_estado)


class Handler(BaseHTTPRequestHandler):
    server_version = "OrquestadorR1C/1.0"

    def log_message(self, formato: str, *args: object) -> None:  # silencia el log por defecto
        logger.info("%s - %s", self.address_string(), formato % args)

    # -- helpers ----------------------------------------------------------
    def _enviar_json(self, status: int, obj: object) -> None:
        cuerpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _leer_json(self) -> dict:
        largo = int(self.headers.get("Content-Length", 0) or 0)
        crudo = self.rfile.read(largo) if largo else b"{}"
        try:
            return json.loads(crudo or b"{}")
        except json.JSONDecodeError:
            raise ErrorServicio("Cuerpo JSON inválido") from None

    def _sesion(self) -> dict:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise ErrorAcceso("Falta el header 'Authorization: Bearer <token>'")
        return verificar_token(auth[len("Bearer "):].strip())

    def _responder_error(self, exc: Exception) -> None:
        if isinstance(exc, PermisoInsuficiente):
            status = 403
        elif isinstance(exc, (CredencialesInvalidas, ErrorAcceso)):
            # CredencialesInvalidas: login con credenciales malas, o token
            # ausente/inválido/expirado -- siempre 401. ErrorAcceso "pelado"
            # (p.ej. falta ORQ_API_SECRET) se trata igual por seguridad: es
            # un fallo de autenticación, nunca uno de permisos insuficientes.
            status = 401
        elif isinstance(exc, NoEncontrado):
            status = 404
        elif isinstance(exc, ErrorServicio):
            status = 400
        else:
            status = 500
            logger.exception("Error no controlado")
        self._enviar_json(status, {"error": str(exc)})

    # -- rutas --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 (nombre exigido por BaseHTTPRequestHandler)
        ruta = urlparse(self.path).path
        try:
            if ruta == "/login":
                cuerpo = self._leer_json()
                rol = _gestor.autenticar(cuerpo.get("usuario", ""), cuerpo.get("contrasena", ""))
                token = generar_token(cuerpo["usuario"], rol)
                self._enviar_json(200, {"token": token, "rol": rol})
            elif ruta == "/slices":
                sesion = self._sesion()
                cuerpo = self._leer_json()
                desplegar = bool(cuerpo.pop("desplegar", False))
                estrategia = cuerpo.pop("estrategia", "objetivo")
                resultado = _svc.crear_slice(sesion["usuario"], cuerpo, desplegar=desplegar,
                                              estrategia=estrategia)
                self._enviar_json(201, resultado)
            elif ruta == "/mantenimiento/limpiar-residuos":
                sesion = self._sesion()
                exigir_rol(sesion, "admin")
                cuerpo = self._leer_json()
                resultado = _svc.limpiar_residuos(confirmar=bool(cuerpo.get("confirmar", False)))
                self._enviar_json(200, resultado)
            else:
                self._enviar_json(404, {"error": f"Ruta no encontrada: POST {ruta}"})
        except Exception as exc:  # noqa: BLE001 (frontera HTTP: todo error se traduce a JSON)
            self._responder_error(exc)

    def do_GET(self) -> None:  # noqa: N802
        partes = urlparse(self.path)
        ruta, qs = partes.path, parse_qs(partes.query)
        try:
            if ruta == "/slices":
                sesion = self._sesion()
                if sesion["rol"] == "admin" and qs.get("todos", ["0"])[0] == "1":
                    self._enviar_json(200, _svc.listar_todos_slices())
                else:
                    self._enviar_json(200, _svc.listar_slices(sesion["usuario"]))
            elif ruta.startswith("/slices/"):
                sesion = self._sesion()
                slice_id = ruta[len("/slices/"):]
                if sesion["rol"] == "admin":
                    self._enviar_json(200, _svc.ver_slice_admin(slice_id))
                else:
                    self._enviar_json(200, _svc.ver_slice(sesion["usuario"], slice_id))
            elif ruta == "/recursos":
                sesion = self._sesion()
                self._enviar_json(200, _svc.consumo_recursos(sesion["usuario"]))
            else:
                self._enviar_json(404, {"error": f"Ruta no encontrada: GET {ruta}"})
        except Exception as exc:  # noqa: BLE001
            self._responder_error(exc)

    def do_DELETE(self) -> None:  # noqa: N802
        ruta = urlparse(self.path).path
        try:
            if ruta.startswith("/slices/"):
                sesion = self._sesion()
                slice_id = ruta[len("/slices/"):]
                if sesion["rol"] == "admin":
                    self._enviar_json(200, _svc.eliminar_slice_admin(slice_id))
                else:
                    self._enviar_json(200, _svc.eliminar_slice(sesion["usuario"], slice_id))
            else:
                self._enviar_json(404, {"error": f"Ruta no encontrada: DELETE {ruta}"})
        except Exception as exc:  # noqa: BLE001
            self._responder_error(exc)


def crear_servidor(driver_nombre: str = "linux", ruta_estado: str | None = None,
                    ruta_usuarios: str | None = None, puerto: int = 8080,
                    host: str = "0.0.0.0") -> ThreadingHTTPServer:
    global _svc, _gestor
    _svc = _construir_servicio(driver_nombre, ruta_estado)
    _gestor = GestorUsuarios(ruta_usuarios)
    return ThreadingHTTPServer((host, puerto), Handler)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="api_http")
    p.add_argument("--driver", choices=["linux", "openstack"],
                   default=os.environ.get("ORQ_DRIVER", "linux"))
    p.add_argument("--estado", default=None, help="archivo JSON de estado de los slices")
    p.add_argument("--usuarios", default=None, help="archivo JSON de usuarios de la API")
    p.add_argument("--puerto", type=int, default=8080)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        servidor = crear_servidor(args.driver, args.estado, args.usuarios, args.puerto, args.host)
    except ErrorAcceso as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"API de R1C escuchando en http://{args.host}:{args.puerto} (driver={args.driver})")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
