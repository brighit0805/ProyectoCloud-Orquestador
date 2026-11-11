"""
composicion.py
--------------
Composición y edición de topologías arbitrarias (R1B, HLD §3.5).

Las topologías del catálogo son *generadores de grafo*: el usuario parte de una
(``topology_manager.construir_topologia``) y a partir de ahí puede modificar el
grafo libremente con las operaciones de este módulo:

    agregar_nodo        incorpora un nodo aislado
    agregar_enlace      une dos nodos que la figura base no conectaba
    quitar_enlace      elimina una conexión sin tocar los nodos
    eliminar_nodo      retira el nodo y los enlaces que dependían de él
    injertar           aplica otra figura del catálogo sobre un nodo existente

Son funciones puras sobre el modelo (``Slice``): no tocan ninguna infraestructura
y no deciden permisos ni estados; eso lo hace la capa de servicio. Tras editar,
``validar`` comprueba que el grafo sea conexo y que ningún nodo supere el número
de interfaces soportado, y ``clasificar`` etiqueta el slice con la figura del
catálogo que le corresponda o con ARBITRARIA si ya no coincide con ninguna.

Supuestos que debe confirmar el grupo
-------------------------------------
* ``MAX_INTERFACES_POR_NODO``: tope de interfaces de enlace por VM. 8 es un valor
  prudente para libvirt/virtio; no está medido en el clúster.
* ``MAX_ENLACES``: el direccionamiento reserva una /30 por enlace con la forma
  ``10.30.<n>.0/30`` y n ≤ 255 (ver ``direccionamiento.PLANTILLA_SUBRED``).
"""

from __future__ import annotations

from .models import (
    FLAVOR_POR_DEFECTO,
    Enlace,
    Nodo,
    Slice,
    TipoTopologia,
    obtener_flavor,
)
from . import topology_manager

MAX_INTERFACES_POR_NODO = 8
MAX_ENLACES = 250
MAX_NODOS = 50           # tope de diseño de la interfaz; no está medido en el clúster

# Límites de capacidad por VM que acepta la interfaz (se valida otra vez en
# VM Placement contra la capacidad real de cada host).
VCPU_MIN, VCPU_MAX = 1, 8
RAM_MIN_MB, RAM_MAX_MB = 256, 16384
DISCO_MIN_GB, DISCO_MAX_GB = 1.0, 100.0

FLAVOR_PERSONALIZADO = "personalizado"


# ---------------------------------------------------------------------------
# Nombres y claves de nodo
# ---------------------------------------------------------------------------
def clave_de(nodo: Nodo) -> str:
    """Clave corta estable del nodo: ``'demo-vm3'`` -> ``'vm3'``.

    No depende de la posición: al eliminar ``vm2`` el nodo ``vm3`` sigue siendo
    ``vm3``, de modo que las referencias del usuario no cambian de significado.
    """
    return nodo.nombre.rsplit("-", 1)[-1]


def resolver(slc: Slice, ref: str) -> Nodo:
    """Devuelve el nodo nombrado por clave corta, nombre completo o id."""
    for n in slc.nodos:
        if ref in (clave_de(n), n.nombre, n.id):
            return n
    claves = ", ".join(clave_de(n) for n in slc.nodos)
    raise ValueError(f"El nodo '{ref}' no existe en el slice. Claves válidas: {claves}")


def _sufijo_numerico(texto: str, prefijo: str) -> int:
    resto = texto[len(prefijo):] if texto.startswith(prefijo) else ""
    return int(resto) if resto.isdigit() else 0


def _siguiente_numero_vm(slc: Slice) -> int:
    return max([_sufijo_numerico(clave_de(n), "vm") for n in slc.nodos] + [0]) + 1


def _siguiente_numero_id_nodo(slc: Slice) -> int:
    pref = f"{slc.id}-n"
    return max([_sufijo_numerico(n.id, pref) for n in slc.nodos] + [-1]) + 1


def _siguiente_numero_enlace(slc: Slice) -> int:
    pref = f"{slc.id}-e"
    return max([_sufijo_numerico(e.id, pref) for e in slc.enlaces] + [-1]) + 1


# ---------------------------------------------------------------------------
# Capacidad de cada VM
# ---------------------------------------------------------------------------
def aplicar_capacidad(nodo: Nodo, flavor: str | None = None, vcpu: int | None = None,
                      ram_mb: int | None = None, disco_gb: float | None = None,
                      imagen: str | None = None) -> None:
    """Fija la capacidad de una VM: un flavor del catálogo y/o valores propios.

    Si se indica ``flavor`` se parte de él; los valores sueltos lo sobrescriben y
    entonces el flavor del nodo pasa a ``personalizado``. Lanza ValueError si un
    valor está fuera de rango, antes de modificar nada.
    """
    if vcpu is not None and not (VCPU_MIN <= vcpu <= VCPU_MAX):
        raise ValueError(f"vcpu debe estar entre {VCPU_MIN} y {VCPU_MAX}")
    if ram_mb is not None and not (RAM_MIN_MB <= ram_mb <= RAM_MAX_MB):
        raise ValueError(f"ram_mb debe estar entre {RAM_MIN_MB} y {RAM_MAX_MB}")
    if disco_gb is not None and not (DISCO_MIN_GB <= disco_gb <= DISCO_MAX_GB):
        raise ValueError(f"disco_gb debe estar entre {DISCO_MIN_GB:g} y {DISCO_MAX_GB:g}")
    base = obtener_flavor(flavor) if flavor is not None else None   # puede lanzar ValueError

    if base is not None:
        nodo.flavor = base.nombre
        nodo.vcpu, nodo.ram_mb, nodo.disco_gb, nodo.imagen = (
            base.vcpu, base.ram_mb, base.disco_gb, base.imagen)
    personalizado = False
    if vcpu is not None:
        nodo.vcpu, personalizado = vcpu, True
    if ram_mb is not None:
        nodo.ram_mb, personalizado = ram_mb, True
    if disco_gb is not None:
        nodo.disco_gb, personalizado = disco_gb, True
    if imagen is not None:
        nodo.imagen, personalizado = imagen, True
    if personalizado:
        nodo.flavor = FLAVOR_PERSONALIZADO


def _nuevo_nodo(slc: Slice, flavor: str | None = None, capacidad: dict | None = None,
                acceso_internet: bool = False) -> Nodo:
    numero_vm = _siguiente_numero_vm(slc)
    nodo = Nodo.desde_flavor(
        id=f"{slc.id}-n{_siguiente_numero_id_nodo(slc)}",
        nombre=f"{slc.nombre}-vm{numero_vm}",
        flavor=flavor or FLAVOR_POR_DEFECTO,
        acceso_internet=acceso_internet,
    )
    if capacidad:
        aplicar_capacidad(nodo, **capacidad)
    return nodo


# ---------------------------------------------------------------------------
# Operaciones de composición
# ---------------------------------------------------------------------------
def agregar_nodo(slc: Slice, flavor: str | None = None, capacidad: dict | None = None,
                acceso_internet: bool = False) -> Nodo:
    """Incorpora un nodo aislado; hay que enlazarlo para que el grafo sea válido."""
    if len(slc.nodos) >= MAX_NODOS:
        raise ValueError(f"Se alcanzó el máximo de {MAX_NODOS} nodos por slice")
    nodo = _nuevo_nodo(slc, flavor, capacidad, acceso_internet)
    slc.nodos.append(nodo)
    slc.tipo_topologia = clasificar(slc)
    return nodo


def _buscar_enlace(slc: Slice, a_id: str, b_id: str) -> Enlace | None:
    for e in slc.enlaces:
        if {e.nodo_origen, e.nodo_destino} == {a_id, b_id}:
            return e
    return None


def agregar_enlace(slc: Slice, a: str, b: str) -> Enlace:
    """Une dos nodos distintos que todavía no estén enlazados."""
    na, nb = resolver(slc, a), resolver(slc, b)
    if na.id == nb.id:
        raise ValueError("Un enlace debe unir dos nodos distintos")
    if _buscar_enlace(slc, na.id, nb.id) is not None:
        raise ValueError(f"Ya existe un enlace entre {clave_de(na)} y {clave_de(nb)}")
    if len(slc.enlaces) >= MAX_ENLACES:
        raise ValueError(f"Se alcanzó el máximo de {MAX_ENLACES} enlaces por slice")
    for n in (na, nb):
        if grado(slc, n.id) >= MAX_INTERFACES_POR_NODO:
            raise ValueError(
                f"{clave_de(n)} ya tiene {MAX_INTERFACES_POR_NODO} interfaces, el máximo soportado")
    enlace = Enlace(id=f"{slc.id}-e{_siguiente_numero_enlace(slc)}",
                    nodo_origen=na.id, nodo_destino=nb.id)
    slc.enlaces.append(enlace)
    slc.tipo_topologia = clasificar(slc)
    return enlace


def quitar_enlace(slc: Slice, a: str, b: str) -> None:
    na, nb = resolver(slc, a), resolver(slc, b)
    enlace = _buscar_enlace(slc, na.id, nb.id)
    if enlace is None:
        raise ValueError(f"No existe un enlace entre {clave_de(na)} y {clave_de(nb)}")
    slc.enlaces.remove(enlace)
    slc.tipo_topologia = clasificar(slc)


def eliminar_nodo(slc: Slice, ref: str) -> None:
    """Retira el nodo y todos los enlaces que dependían de él."""
    nodo = resolver(slc, ref)
    if len(slc.nodos) <= 2:
        raise ValueError("Un slice necesita al menos 2 nodos; elimine el slice completo")
    slc.nodos.remove(nodo)
    slc.enlaces = [e for e in slc.enlaces if nodo.id not in (e.nodo_origen, e.nodo_destino)]
    slc.tipo_topologia = clasificar(slc)


def injertar(slc: Slice, sobre: str, tipo: TipoTopologia, num_nodos: int,
             ramificacion: int = 2, flavor: str | None = None) -> list[Nodo]:
    """Aplica una figura del catálogo tomando un nodo existente como raíz.

    ``num_nodos`` cuenta solo los nodos NUEVOS; la raíz ya existe. Calcula antes
    todos los enlaces para que, si la figura es inválida, no quede nada a medias.
    """
    if num_nodos < 1:
        raise ValueError("Un injerto debe añadir al menos un nodo")
    raiz = resolver(slc, sobre)

    nuevos: list[Nodo] = []
    for _ in range(num_nodos):
        # los nodos aún no están en el slice: numeramos sobre una vista provisional
        provisional = Slice(id=slc.id, nombre=slc.nombre, owner=slc.owner,
                            tipo_topologia=slc.tipo_topologia,
                            nodos=slc.nodos + nuevos, enlaces=slc.enlaces)
        nuevos.append(_nuevo_nodo(provisional, flavor))

    figura = [raiz.id] + [n.id for n in nuevos]
    pares = topology_manager._figura_a_pares(tipo, figura, ramificacion)   # puede lanzar ValueError
    siguiente = _siguiente_numero_enlace(slc)
    enlaces_nuevos = [Enlace(id=f"{slc.id}-e{siguiente + i}", nodo_origen=o, nodo_destino=d)
                      for i, (o, d) in enumerate(pares)]

    # Se aplica solo si el resultado respeta los límites; si no, el slice queda intacto.
    antes_nodos, antes_enlaces, antes_tipo = list(slc.nodos), list(slc.enlaces), slc.tipo_topologia
    slc.nodos.extend(nuevos)
    slc.enlaces.extend(enlaces_nuevos)
    problemas = [p for p in validar(slc) if "interfaces" in p or "enlaces" in p or "nodos;" in p]
    if problemas:
        slc.nodos, slc.enlaces, slc.tipo_topologia = antes_nodos, antes_enlaces, antes_tipo
        raise ValueError(problemas[0])
    slc.tipo_topologia = clasificar(slc)
    return nuevos


# ---------------------------------------------------------------------------
# Análisis del grafo
# ---------------------------------------------------------------------------
def grado(slc: Slice, nodo_id: str) -> int:
    return sum(1 for e in slc.enlaces if nodo_id in (e.nodo_origen, e.nodo_destino))


def _vecinos(slc: Slice) -> dict[str, set[str]]:
    v: dict[str, set[str]] = {n.id: set() for n in slc.nodos}
    for e in slc.enlaces:
        if e.nodo_origen in v and e.nodo_destino in v:
            v[e.nodo_origen].add(e.nodo_destino)
            v[e.nodo_destino].add(e.nodo_origen)
    return v


def es_conexo(slc: Slice) -> bool:
    """True si desde cualquier nodo se puede llegar a todos los demás."""
    if not slc.nodos:
        return False
    v = _vecinos(slc)
    visto = {slc.nodos[0].id}
    pendientes = [slc.nodos[0].id]
    while pendientes:
        actual = pendientes.pop()
        for vecino in v[actual]:
            if vecino not in visto:
                visto.add(vecino)
                pendientes.append(vecino)
    return len(visto) == len(slc.nodos)


def validar(slc: Slice) -> list[str]:
    """Lista de problemas del grafo; vacía si el slice es desplegable.

    Es la validación *de diseño*. VM Placement sigue validando la capacidad real
    de los hosts, y R1C (cuando exista) vuelve a validar todo del lado servidor.
    """
    problemas: list[str] = []
    ids = {n.id for n in slc.nodos}
    if len(slc.nodos) < 2:
        problemas.append("El slice necesita al menos 2 nodos")
    if len(slc.nodos) > MAX_NODOS:
        problemas.append(f"Hay {len(slc.nodos)} nodos; el máximo es {MAX_NODOS}")
    vistos: set[frozenset] = set()
    for e in slc.enlaces:
        if e.nodo_origen not in ids or e.nodo_destino not in ids:
            problemas.append(f"El enlace {e.id} apunta a un nodo que no existe")
            continue
        if e.nodo_origen == e.nodo_destino:
            problemas.append(f"El enlace {e.id} une un nodo consigo mismo")
        par = frozenset((e.nodo_origen, e.nodo_destino))
        if par in vistos:
            problemas.append(f"Hay más de un enlace entre los mismos dos nodos ({e.id})")
        vistos.add(par)
    if len(slc.enlaces) > MAX_ENLACES:
        problemas.append(f"Hay {len(slc.enlaces)} enlaces; el máximo es {MAX_ENLACES}")
    for n in slc.nodos:
        g = grado(slc, n.id)
        if g > MAX_INTERFACES_POR_NODO:
            problemas.append(
                f"{clave_de(n)} tiene {g} interfaces; el máximo soportado es {MAX_INTERFACES_POR_NODO}")
    if len(slc.nodos) >= 2 and not es_conexo(slc):
        problemas.append("El grafo no es conexo: hay nodos sin camino hacia el resto")
    return problemas


def exigir_valido(slc: Slice) -> None:
    problemas = validar(slc)
    if problemas:
        raise ValueError("; ".join(problemas))


def clasificar(slc: Slice) -> TipoTopologia:
    """Figura del catálogo que describe el grafo, o ARBITRARIA si ninguna.

    Orden de comprobación: lineal, anillo, malla, árbol. Un triángulo es a la vez
    anillo y malla; se etiqueta ANILLO. Un árbol cualquiera (conexo, n-1 enlaces,
    no lineal) se etiqueta ÁRBOL.
    """
    n, m = len(slc.nodos), len(slc.enlaces)
    if n < 2 or not es_conexo(slc) or validar_sin_conexion(slc):
        return TipoTopologia.ARBITRARIA
    grados = sorted(grado(slc, x.id) for x in slc.nodos)
    if m == n - 1:
        if grados[-1] <= 2:
            return TipoTopologia.LINEAL
        return TipoTopologia.ARBOL if n >= 3 else TipoTopologia.ARBITRARIA
    if n >= 3 and m == n and grados[0] == grados[-1] == 2:
        return TipoTopologia.ANILLO
    if n >= 3 and m == n * (n - 1) // 2:
        return TipoTopologia.MALLA
    return TipoTopologia.ARBITRARIA


def validar_sin_conexion(slc: Slice) -> list[str]:
    """Problemas estructurales distintos de la conectividad (enlaces rotos o repetidos)."""
    return [p for p in validar(slc) if "conexo" not in p and "al menos 2" not in p]
