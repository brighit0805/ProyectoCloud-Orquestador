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
    GET  /zonas                              zonas de disponibilidad reales (GUI, 23-nov)
    GET  /catalogo                           catálogo de topologías disponibles (GUI, 23-nov)
    POST /mantenimiento/limpiar-residuos     SOLO admin
    GET  /, /app.js, /style.css, ...         sirve la GUI estática (misma API, sin CORS)
    POST /slices/<id>/desplegar              encola el despliegue -> 202 {trabajo}
    DELETE /slices/<id>?asincrono=1          encola la eliminación -> 202 {trabajo}
    GET  /trabajos                           sus trabajos (admin + ?todos=1 -> de todos)
    GET  /trabajos/<id>                      estado de un trabajo (pendiente, en_curso...)

Con ``"asincrono": true`` en el cuerpo de POST /slices (y ``"desplegar": true``),
el slice se crea como borrador y su despliegue se encola: responde 202 al
instante en lugar de esperar a que terminen las VMs (cola de trabajos, R1).

Todas las rutas salvo /login y los archivos estáticos exigen el header
``Authorization: Bearer <token>``.

Arranque:
    python3 -m slice_orchestrator.api_http --driver linux --puerto 8080
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
                asincrono = bool(cuerpo.pop("asincrono", False))
                if desplegar and asincrono:
                    borrador = _svc.crear_slice(sesion["usuario"], cuerpo)
                    trabajo = _svc.encolar_despliegue(sesion["usuario"], borrador["id"], estrategia)
                    self._enviar_json(202, {"slice": borrador, "trabajo": trabajo})
                    return
                resultado = _svc.crear_slice(sesion["usuario"], cuerpo, desplegar=desplegar,
                                              estrategia=estrategia)
                self._enviar_json(201, resultado)
            elif ruta.startswith("/slices/") and ruta.endswith("/desplegar"):
                sesion = self._sesion()
                slice_id = ruta[len("/slices/"):-len("/desplegar")]
                cuerpo = self._leer_json()
                trabajo = _svc.encolar_despliegue(sesion["usuario"], slice_id,
                                                  cuerpo.get("estrategia", "objetivo"))
                self._enviar_json(202, trabajo)
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
            elif ruta == "/trabajos":
                sesion = self._sesion()
                todos = sesion["rol"] == "admin" and qs.get("todos", ["0"])[0] == "1"
                self._enviar_json(200, _svc.listar_trabajos(sesion["usuario"], todos=todos))
            elif ruta.startswith("/trabajos/"):
                sesion = self._sesion()
                self._enviar_json(200, _svc.ver_trabajo(sesion["usuario"], ruta[len("/trabajos/"):],
                                                        admin=sesion["rol"] == "admin"))
            elif ruta == "/zonas":
                self._sesion()
                self._enviar_json(200, _svc.listar_zonas())
            elif ruta == "/catalogo":
                self._sesion()
                self._enviar_json(200, _svc.listar_topologias())
            elif self._servir_estatico(ruta):
                pass
            else:
                self._enviar_json(404, {"error": f"Ruta no encontrada: GET {ruta}"})
        except Exception as exc:  # noqa: BLE001
            self._responder_error(exc)

    # -- GUI estática (agregado 23-nov-2026) ---------------------------------
    def _servir_estatico(self, ruta: str) -> bool:
        """Sirve los archivos de la GUI (carpeta ``gui/`` junto a este módulo)
        en el mismo origen que la API, para no necesitar CORS ni un segundo
        servidor. Devuelve False (sin escribir nada) si la ruta no calza con
        ningún archivo, para que el llamador pueda seguir con su propio 404."""
        base = Path(__file__).resolve().parent / "gui"
        if not base.is_dir():
            return False
        rel = "index.html" if ruta == "/" else ruta.lstrip("/")
        destino = (base / rel).resolve()
        try:
            destino.relative_to(base.resolve())
        except ValueError:
            return False  # intento de escapar de la carpeta (../..) -- se ignora
        if not destino.is_file():
            return False
        tipo, _ = mimetypes.guess_type(str(destino))
        cuerpo = destino.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", tipo or "application/octet-stream")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)
        return True

    def do_DELETE(self) -> None:  # noqa: N802
        partes = urlparse(self.path)
        ruta, qs = partes.path, parse_qs(partes.query)
        try:
            if ruta.startswith("/slices/"):
                sesion = self._sesion()
                slice_id = ruta[len("/slices/"):]
                if qs.get("asincrono", ["0"])[0] == "1":
                    self._enviar_json(202, _svc.encolar_eliminacion(
                        sesion["usuario"], slice_id, admin=sesion["rol"] == "admin"))
                elif sesion["rol"] == "admin":
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
