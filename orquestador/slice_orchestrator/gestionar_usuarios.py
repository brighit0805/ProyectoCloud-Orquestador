"""CLI para administrar los usuarios de la API de R1C (control_acceso.py).

Uso:
    python3 -m slice_orchestrator.gestionar_usuarios agregar <usuario> --rol admin|usuario
    python3 -m slice_orchestrator.gestionar_usuarios listar
    python3 -m slice_orchestrator.gestionar_usuarios eliminar <usuario>

La contraseña SIEMPRE se pide de forma interactiva (oculta, con getpass) —
nunca se pasa como argumento de línea de comandos ni queda en el historial
de la shell.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .control_acceso import ErrorAcceso, GestorUsuarios


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gestionar_usuarios")
    p.add_argument("--archivo", default=None,
                    help="ruta del JSON de usuarios (o la variable ORQ_USUARIOS_API)")
    sub = p.add_subparsers(dest="comando", required=True)

    a = sub.add_parser("agregar", help="crea o actualiza un usuario (pide la contraseña)")
    a.add_argument("usuario")
    a.add_argument("--rol", choices=("admin", "usuario"), required=True)

    sub.add_parser("listar", help="lista los usuarios y sus roles (sin contraseñas)")

    e = sub.add_parser("eliminar", help="elimina un usuario")
    e.add_argument("usuario")

    args = p.parse_args(argv)
    gestor = GestorUsuarios(args.archivo)

    try:
        if args.comando == "agregar":
            password = getpass.getpass(f"Contraseña para '{args.usuario}': ")
            confirmacion = getpass.getpass("Repita la contraseña: ")
            if password != confirmacion:
                print("Error: las dos contraseñas no coinciden", file=sys.stderr)
                return 1
            gestor.agregar(args.usuario, password, args.rol)
            print(f"Usuario '{args.usuario}' guardado con rol '{args.rol}'.")
        elif args.comando == "listar":
            usuarios = gestor.listar()
            if not usuarios:
                print("No hay usuarios registrados.")
            for u in usuarios:
                print(f"  {u['usuario']:<20} rol={u['rol']}")
        elif args.comando == "eliminar":
            gestor.eliminar(args.usuario)
            print(f"Usuario '{args.usuario}' eliminado.")
        return 0
    except ErrorAcceso as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
