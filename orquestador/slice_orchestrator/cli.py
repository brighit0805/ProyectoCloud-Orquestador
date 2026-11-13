"""
cli.py
------
Interfaz de usuario de consola del orquestador (R1B). Decisión de diseño del HLD
(§3.8): CLI en la Fase 1 y web en la Fase 2, porque la CLI ejercita la misma
superficie de operaciones con una fracción del esfuerzo.

La CLI **no valida nada por su cuenta**: traduce argumentos a peticiones y se las
pasa a ``ServicioSlices``, que es quien valida (HLD §3.2: «toda petición sale
hacia R1C y es ahí donde se valida»). Lo único que hace aquí es interpretar la
sintaxis de los argumentos.

Uso (desde la carpeta ``orquestador``):

    python -m slice_orchestrator.cli --usuario ana topologias
    python -m slice_orchestrator.cli -u ana slice crear --nombre demo --tipo anillo --nodos 4 --desplegar
    python -m slice_orchestrator.cli -u ana slice listar
    python -m slice_orchestrator.cli --help

El estado se guarda en ``estado_r1b.json`` (o en ``--estado``/``ORQ_ESTADO``), de
modo que varias invocaciones y varios usuarios comparten la misma "infraestructura".

El backend se elige con ``--driver`` (o la variable ``ORQ_DRIVER``):
    - ``linux`` (por defecto): driver Linux en modo SIMULADO, no toca ningún clúster.
    - ``openstack``: driver OpenStack en modo REAL contra el clúster del curso;
      requiere las variables de entorno de ``. .env.real`` ya cargadas en la
      sesión de shell (ver ``driver_openstack.ConfigOpenStack.desde_entorno``).
      Descubre el inventario real de hosts vía Placement antes de operar.
Esto cierra, para R1B, el criterio de rúbrica "ser independiente de la capa de
infraestructura (cluster Linux, OpenStack, AWS, etc.)": la misma CLI, con el
mismo conjunto de comandos, puede apuntar a cualquiera de los dos backends.

La CLI no expone una opción para elegir la estrategia de colocación (balanceado/
round_robin/objetivo): confirmado con German el 12-nov, esa decisión es del
sistema, no del usuario. Por dentro, ``ServicioSlices`` siempre usa 'objetivo'
(el Scheduler: filtros + pesadores + función objetivo, Lab 7) por defecto.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .servicio_slices import ErrorServicio, ServicioSlices


def _construir_servicio_openstack_real(ruta_estado: str) -> ServicioSlices:
    """Arma un ServicioSlices contra el clúster REAL de OpenStack: lee la config
    de las variables de entorno (las de '. .env.real'), construye el driver en
    modo REAL y descubre el inventario real de hosts vía Placement -- en vez
    del inventario simulado fijo (worker1..3, sin disco) que usa 'linux'. Sin
    esto, --driver openstack llamaría a la API real pero seguiría calculando
    la colocación con números inventados."""
    # import perezoso: driver_openstack solo hace falta en este camino, no en
    # el camino simulado por defecto.
    from .driver_openstack import ConfigOpenStack, DriverOpenStack, ModoOpenStack

    cfg = ConfigOpenStack.desde_entorno()
    faltan = cfg.faltantes()
    if faltan:
        raise ErrorServicio(
            "Faltan variables de entorno para el driver OpenStack real: "
            f"{', '.join(faltan)}. Cargue primero '. .env.real' en esta sesión "
            "de shell (o configure ORQ_DRIVER=linux para usar el simulado).")
    driver = DriverOpenStack(modo=ModoOpenStack.REAL, config=cfg)
    try:
        hosts_reales = driver.descubrir_hosts()
    except Exception as exc:  # cualquier fallo de red/API se traduce a un error de CLI limpio
        raise ErrorServicio(f"No se pudo descubrir el inventario real del clúster: {exc}") from exc
    print("⚠ Usando el driver OpenStack REAL -- las operaciones afectan al clúster compartido.",
          file=sys.stderr)
    return ServicioSlices(ruta_estado=ruta_estado, hosts=hosts_reales, driver=driver)

ESTADO_POR_DEFECTO = "estado_r1b.json"


# ---------------------------------------------------------------------------
# Interpretación de argumentos (solo sintaxis)
# ---------------------------------------------------------------------------
def _tres_partes(texto: str, que: str) -> tuple[str, str, int]:
    partes = texto.split(":")
    if len(partes) != 3 or not partes[2].isdigit():
        raise argparse.ArgumentTypeError(f"{que} debe tener la forma SOBRE:TIPO:NODOS (p. ej. vm4:lineal:2)")
    return partes[0], partes[1], int(partes[2])


def _par(texto: str) -> list[str]:
    partes = texto.split(",")
    if len(partes) != 2 or not all(partes):
        raise argparse.ArgumentTypeError("un enlace se escribe A,B (p. ej. vm2,vm5)")
    return partes


def _asignacion(texto: str) -> tuple[str, str]:
    if "=" not in texto:
        raise argparse.ArgumentTypeError("use la forma CLAVE=VALOR (p. ej. vm2=cirros)")
    clave, valor = texto.split("=", 1)
    return clave, valor


def _capacidad(texto: str) -> tuple[str, dict[str, Any]]:
    """``vm3=vcpu:2,ram:1024,disco:8,imagen:x.qcow2,flavor:cirros``."""
    clave, resto = _asignacion(texto)
    equivalencias = {"vcpu": ("vcpu", int), "ram": ("ram_mb", int), "disco": ("disco_gb", float),
                     "imagen": ("imagen", str), "flavor": ("flavor", str)}
    valores: dict[str, Any] = {}
    for trozo in resto.split(","):
        nombre, _, valor = trozo.partition(":")
        if nombre not in equivalencias or not valor:
            raise argparse.ArgumentTypeError(
                "capacidad: use vcpu:N, ram:MB, disco:GB, imagen:NOMBRE y/o flavor:NOMBRE separados por comas")
        destino, conv = equivalencias[nombre]
        try:
            valores[destino] = conv(valor)
        except ValueError:
            raise argparse.ArgumentTypeError(f"valor inválido para {nombre}: {valor}") from None
    return clave, valores


def _leer_json(ruta: str) -> Any:
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except OSError as exc:
        raise ErrorServicio(f"No se pudo leer {ruta}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise ErrorServicio(f"{ruta} no es un JSON válido: {exc}") from None


# ---------------------------------------------------------------------------
# Presentación
# ---------------------------------------------------------------------------
def _tabla(filas: list[list[Any]], cabecera: list[str]) -> str:
    textos = [[str(c) for c in fila] for fila in [cabecera] + filas]
    anchos = [max(len(f[i]) for f in textos) for i in range(len(cabecera))]
    lineas = ["  ".join(c.ljust(anchos[i]) for i, c in enumerate(f)).rstrip() for f in textos]
    lineas.insert(1, "  ".join("-" * a for a in anchos))
    return "\n".join(lineas)


def _mostrar_slice(d: dict[str, Any]) -> str:
    sal = [f"Slice {d['id']}  «{d['nombre']}»  estado={d['estado']}  tipo={d['tipo']}  "
           f"zona={d['zona'] or '-'}  propietario={d['owner']}", ""]
    sal.append(_tabla([[n["clave"], n["flavor"], n["vcpu"], n["ram_mb"], n["disco_gb"], n["imagen"],
                        n["host"] or "-", "sí" if n["acceso_internet"] else "no"] for n in d["nodos"]],
                      ["nodo", "flavor", "vCPU", "RAM MB", "disco GB", "imagen", "host", "internet"]))
    sal.append("")
    sal.append(_tabla([[e["id"], f"{e['a']} — {e['b']}", e["vlan"] or "-", e["red"] or "-"]
                       for e in d["enlaces"]], ["enlace", "nodos", "VLAN", "red"]))
    if any(n["interfaces"] for n in d["nodos"]):
        sal += ["", "Direcciones de los enlaces:"]
        for n in d["nodos"]:
            sal.append(f"  {n['clave']}: " + ", ".join(f"{i['ip']} (vlan {i['vlan']})" for i in n["interfaces"]))
    return "\n".join(sal)


def _salida(args: argparse.Namespace, datos: Any, texto: str) -> None:
    print(json.dumps(datos, ensure_ascii=False, indent=2) if args.json else texto)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------
def _cmd_topologias(svc: ServicioSlices, a: argparse.Namespace) -> None:
    datos = svc.listar_topologias()
    _salida(a, datos, _tabla([[t["tipo"], t["enlaces"], t["minimo_nodos"], t["restriccion"],
                               "sí" if t["disponible"] else "no (diseñada)"] for t in datos],
                             ["topología", "enlaces", "mín. nodos", "restricción", "disponible"]))


def _cmd_flavors(svc: ServicioSlices, a: argparse.Namespace) -> None:
    datos = svc.listar_flavors()
    _salida(a, datos, _tabla([[f["nombre"], f["vcpu"], f["ram_mb"], f["disco_gb"], f["imagen"]] for f in datos],
                             ["flavor", "vCPU", "RAM MB", "disco GB", "imagen"]))


def _spec_desde_argumentos(a: argparse.Namespace) -> dict[str, Any]:
    if a.spec:
        spec = _leer_json(a.spec)
        if a.nombre and isinstance(spec, dict):
            spec["nombre"] = a.nombre
        return spec
    if not (a.nombre and a.tipo and a.nodos is not None):
        raise ErrorServicio("Indique --spec ARCHIVO, o bien --nombre, --tipo y --nodos")
    spec: dict[str, Any] = {
        "nombre": a.nombre,
        "topologia": {
            "base": {"tipo": a.tipo, "nodos": a.nodos, "ramificacion": a.ramificacion},
            "injertos": [{"sobre": s, "tipo": t, "nodos": n} for s, t, n in a.rama],
            "enlaces_extra": a.enlace_extra,
            "enlaces_quitados": a.enlace_quitado,
        },
        "flavores": dict(a.flavor),
        "capacidad": dict(a.capacidad),
        "acceso_internet": a.internet,
    }
    if a.zona:
        spec["zona_disponibilidad"] = a.zona
    return spec


def _cmd_slice(svc: ServicioSlices, a: argparse.Namespace) -> None:
    u = a.usuario
    if a.sub == "crear":
        d = svc.crear_slice(u, _spec_desde_argumentos(a), desplegar=a.desplegar)
        _salida(a, d, _mostrar_slice(d))
    elif a.sub == "listar":
        datos = svc.listar_slices(u)
        _salida(a, datos, _tabla([[s["id"], s["nombre"], s["estado"], s["tipo"], s["nodos"], s["enlaces"],
                                   s["zona"] or "-"] for s in datos],
                                 ["id", "nombre", "estado", "tipo", "nodos", "enlaces", "zona"])
                if datos else "No tiene slices.")
    elif a.sub == "ver":
        d = svc.ver_slice(u, a.id)
        _salida(a, d, _mostrar_slice(d))
    elif a.sub == "desplegar":
        d = svc.desplegar_slice(u, a.id)
        _salida(a, d, _mostrar_slice(d))
    elif a.sub == "eliminar":
        d = svc.eliminar_slice(u, a.id)
        _salida(a, d, f"Slice {d['id']} eliminado.")
    elif a.sub == "editar":
        ops: list[dict[str, Any]] = []
        if a.operaciones:
            cargadas = _leer_json(a.operaciones)
            if not isinstance(cargadas, list):
                raise ErrorServicio("El archivo de operaciones debe contener una lista JSON")
            ops += cargadas
        ops += [{"op": "eliminar_nodo", "nodo": n} for n in a.eliminar_nodo]
        ops += [{"op": "agregar_nodo", "flavor": a.flavor_nodo} for _ in range(a.agregar_nodos)]
        ops += [{"op": "injertar", "sobre": s, "tipo": t, "nodos": n} for s, t, n in a.injertar]
        ops += [{"op": "agregar_enlace", "a": x, "b": y} for x, y in a.agregar_enlace]
        ops += [{"op": "quitar_enlace", "a": x, "b": y} for x, y in a.quitar_enlace]
        ops += [{"op": "capacidad", "nodo": k, **v} for k, v in a.capacidad]
        if a.zona is not None:
            ops.append({"op": "zona", "zona": a.zona})
        d = svc.editar_slice(u, a.id, ops)
        _salida(a, d, _mostrar_slice(d))
    elif a.sub == "exportar":
        plantilla = svc.exportar_plantilla(u, a.id)
        with open(a.archivo, "w", encoding="utf-8") as f:
            json.dump(plantilla, f, ensure_ascii=False, indent=2)
        _salida(a, plantilla, f"Plantilla de {a.id} guardada en {a.archivo}")
    elif a.sub == "importar":
        d = svc.crear_desde_plantilla(u, _leer_json(a.archivo), a.nombre)
        _salida(a, d, _mostrar_slice(d))


def _cmd_recursos(svc: ServicioSlices, a: argparse.Namespace) -> None:
    r = svc.consumo_recursos(a.usuario)
    texto = ["Consumo por host:", _tabla(
        [[h["host"], h["zona"], f"{h['vcpu_usados']}/{h['vcpu_total']}",
          f"{h['ram_usada_mb']}/{h['ram_total_mb']}", h["disco_usado_gb"]] for h in r["hosts"]],
        ["host", "zona", "vCPU usadas", "RAM usada MB", "disco usado GB"]), "", "Sus slices activos:"]
    texto.append(_tabla([[s["id"], s["nombre"], s["nodos"], s["vcpu"], s["ram_mb"], s["disco_gb"]]
                         for s in r["mis_slices"]], ["id", "nombre", "nodos", "vCPU", "RAM MB", "disco GB"])
                 if r["mis_slices"] else "  (ninguno)")
    t = r["total_mio"]
    texto.append(f"\nTotal suyo: {t['vcpu']} vCPU, {t['ram_mb']} MB de RAM, {t['disco_gb']} GB de disco")
    _salida(a, r, "\n".join(texto))


def _cmd_imagen(svc: ServicioSlices, a: argparse.Namespace) -> None:
    if a.sub == "listar":
        datos = svc.listar_imagenes()
        _salida(a, datos, _tabla([[i["nombre"], i["formato"], i["tamano_bytes"] or "-",
                                   i["importada_por"] or "catálogo"] for i in datos],
                                 ["imagen", "formato", "bytes", "origen"]))
    else:
        i = svc.importar_imagen(a.usuario, a.nombre, a.origen)
        _salida(a, i, f"Imagen {i['nombre']} registrada ({i['tamano_bytes']} bytes, sha256 {i['sha256'][:12]}…).\n"
                      "Recuerde: la copia al repositorio de imágenes del servidor no la hace esta interfaz.")


def _cmd_mantenimiento(svc: ServicioSlices, a: argparse.Namespace) -> None:
    if a.sub == "limpiar-residuos":
        r = svc.limpiar_residuos(confirmar=a.confirmar)
        texto = []
        for etiqueta, clave in (("Proyectos", "proyectos"), ("Redes", "redes"), ("Flavors", "flavors")):
            items = r[clave]
            if not items:
                texto.append(f"{etiqueta}: ninguno.")
                continue
            texto.append(f"{etiqueta} ({len(items)}):")
            texto.append(_tabla([[i["name"], i["id"]] for i in items], ["nombre", "id"]))
        encabezado = ("Residuos BORRADOS:" if a.confirmar else
                      "Residuos detectados (dry-run; use --confirmar para borrarlos):")
        _salida(a, r, encabezado + "\n\n" + "\n".join(texto))


def _cmd_consola(svc: ServicioSlices, a: argparse.Namespace) -> None:
    if a.sub == "emitir":
        t = svc.emitir_token_consola(a.usuario, a.id, a.nodo)
        _salida(a, t, f"Consola de {a.nodo}: host {t['host']}, puerto {t['puerto']}\n"
                      f"Token (de un solo uso, caduca en {t['expira_en']} s): {t['token']}\n"
                      "Aviso: el puerto es simulado; el proxy VNC todavía no existe.")
    else:
        t = svc.canjear_token_consola(a.token)
        _salida(a, t, f"Token válido y consumido: {t['usuario']} → {t['slice_id']}/{t['nodo']} "
                      f"({t['host']}:{t['puerto']})")


# ---------------------------------------------------------------------------
# Construcción del parser
# ---------------------------------------------------------------------------
def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orq", description="Interfaz de consola del orquestador de slices (R1B)")
    p.add_argument("-u", "--usuario", default=os.environ.get("ORQ_USUARIO"),
                   help="usuario que opera (o variable ORQ_USUARIO)")
    p.add_argument("--estado", default=os.environ.get("ORQ_ESTADO", ESTADO_POR_DEFECTO),
                   help=f"archivo de estado (por defecto {ESTADO_POR_DEFECTO})")
    p.add_argument("--json", action="store_true", help="salida en JSON")
    p.add_argument("--driver", choices=["linux", "openstack"],
                   default=os.environ.get("ORQ_DRIVER", "linux"),
                   help="'linux' (por defecto): driver SIMULADO, no toca ningún clúster. "
                        "'openstack': driver REAL -- requiere las variables de entorno de "
                        ". .env.real (ver driver_openstack.ConfigOpenStack) ya cargadas en la sesión.")
    sub = p.add_subparsers(dest="comando", required=True)

    sub.add_parser("topologias", help="catálogo de topologías predefinidas")
    sub.add_parser("flavors", help="capacidades de VM disponibles")
    sub.add_parser("recursos", help="consumo de recursos del sistema y de sus slices")

    s = sub.add_parser("slice", help="ciclo de vida de slices").add_subparsers(dest="sub", required=True)
    c = s.add_parser("crear", help="crear un slice (BORRADOR; con --desplegar también lo despliega)")
    c.add_argument("--spec", help="archivo JSON con la especificación completa")
    c.add_argument("--nombre")
    c.add_argument("--tipo", help="lineal, anillo, malla, arbol o bus")
    c.add_argument("--nodos", type=int)
    c.add_argument("--ramificacion", type=int, default=2, help="hijos por nodo (solo árbol)")
    c.add_argument("--rama", action="append", default=[], type=lambda t: _tres_partes(t, "--rama"),
                   metavar="SOBRE:TIPO:N", help="injerta una figura sobre un nodo (repetible)")
    c.add_argument("--flavor", action="append", default=[], type=_asignacion, metavar="vmN=FLAVOR")
    c.add_argument("--capacidad", action="append", default=[], type=_capacidad,
                   metavar="vmN=vcpu:2,ram:1024,disco:8")
    c.add_argument("--internet", action="append", default=[], metavar="vmN", help="nodo con salida a Internet")
    c.add_argument("--enlace-extra", action="append", default=[], type=_par, metavar="A,B")
    c.add_argument("--enlace-quitado", action="append", default=[], type=_par, metavar="A,B")
    c.add_argument("--zona")
    c.add_argument("--desplegar", action="store_true")
    # Hallazgo del 12-nov (confirmado con German): la estrategia de colocación
    # NO la elige el usuario -- "su solución debería decidirlo". Por eso ya no
    # hay un --estrategia aquí: el sistema siempre usa el Scheduler inteligente
    # (filtros + pesadores + función objetivo, Lab 7) por dentro.

    s.add_parser("listar", help="sus slices")
    for nombre, ayuda in (("ver", "detalle de un slice"), ("eliminar", "borrar un slice"),
                          ("desplegar", "desplegar un BORRADOR")):
        x = s.add_parser(nombre, help=ayuda)
        x.add_argument("id")

    e = s.add_parser("editar", help="componer y editar un slice en BORRADOR (todo o nada)")
    e.add_argument("id")
    e.add_argument("--operaciones", help="archivo JSON con una lista de operaciones, en el orden dado")
    e.add_argument("--eliminar-nodo", action="append", default=[], metavar="vmN")
    e.add_argument("--agregar-nodos", type=int, default=0, metavar="N", help="agrega N nodos aislados")
    e.add_argument("--flavor-nodo", default=None, help="flavor de los nodos agregados")
    e.add_argument("--injertar", action="append", default=[], type=lambda t: _tres_partes(t, "--injertar"),
                   metavar="SOBRE:TIPO:N")
    e.add_argument("--agregar-enlace", action="append", default=[], type=_par, metavar="A,B")
    e.add_argument("--quitar-enlace", action="append", default=[], type=_par, metavar="A,B")
    e.add_argument("--zona", default=None,
                   help="fija/cambia la zona de disponibilidad del slice (obligatoria para "
                        "desplegar con --driver openstack)")
    e.add_argument("--capacidad", action="append", default=[], type=_capacidad,
                   metavar="vmN=vcpu:2,ram:1024,disco:8")

    x = s.add_parser("exportar", help="guarda el diseño del slice como plantilla JSON")
    x.add_argument("id")
    x.add_argument("archivo")
    x = s.add_parser("importar", help="crea un BORRADOR desde una plantilla JSON")
    x.add_argument("archivo")
    x.add_argument("--nombre")

    i = sub.add_parser("imagen", help="catálogo de imágenes").add_subparsers(dest="sub", required=True)
    i.add_parser("listar")
    x = i.add_parser("importar", help="registra una imagen qcow2 en el catálogo")
    x.add_argument("nombre")
    x.add_argument("origen", help="ruta del archivo .qcow2")

    k = sub.add_parser("consola", help="tokens de acceso a la consola de una VM").add_subparsers(
        dest="sub", required=True)
    x = k.add_parser("emitir")
    x.add_argument("id")
    x.add_argument("nodo")
    x = k.add_parser("canjear", help="valida y consume un token (lo haría el proxy de consola)")
    x.add_argument("token")

    m = sub.add_parser("mantenimiento", help="tareas de mantenimiento del clúster "
                        "(solo --driver openstack)").add_subparsers(dest="sub", required=True)
    x = m.add_parser("limpiar-residuos", help="detecta (y, con --confirmar, borra) "
                      "proyectos/redes/flavors huérfanos dejados por sesiones anteriores")
    x.add_argument("--confirmar", action="store_true",
                    help="borra de verdad los residuos detectados (sin esto, solo informa: dry-run)")
    return p


def main(argv: list[str] | None = None, servicio: ServicioSlices | None = None) -> int:
    parser = construir_parser()
    args = parser.parse_args(argv)
    try:
        if servicio is not None:
            svc = servicio
        elif args.driver == "openstack":
            svc = _construir_servicio_openstack_real(args.estado)
        else:
            svc = ServicioSlices(ruta_estado=args.estado)
        necesita_usuario = (args.comando in ("slice", "recursos")
                            or (args.comando == "imagen" and args.sub == "importar")
                            or (args.comando == "consola" and args.sub == "emitir"))
        if necesita_usuario and not args.usuario:
            raise ErrorServicio("Indique el usuario con --usuario o la variable ORQ_USUARIO")
        {"topologias": _cmd_topologias, "flavors": _cmd_flavors, "slice": _cmd_slice,
         "recursos": _cmd_recursos, "imagen": _cmd_imagen, "consola": _cmd_consola,
         "mantenimiento": _cmd_mantenimiento}[args.comando](svc, args)
        return 0
    except ErrorServicio as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for _flujo in (sys.stdout, sys.stderr):      # consola de Windows (cp1252): evitar UnicodeEncodeError
        if hasattr(_flujo, "reconfigure"):
            _flujo.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
