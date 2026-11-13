"""
topology_manager.py
--------------------
Topology Manager (submódulo de B) Orquestación).

Responsabilidad (según la arquitectura del Entregable 2): evaluar y
calcular CÓMO se va a desplegar un slice — no decide en qué host físico
va cada VM (eso es trabajo de VM Placement), decide la forma de la red
lógica del slice, el orden de creación, y la estrategia de rollback.

Las topologías predefinidas se tratan como GENERADORES DE GRAFO y no
como plantillas cerradas: el usuario parte de una del catálogo y puede
injertar otras sobre un nodo existente, obteniendo topologías
arbitrarias sin casos especiales escritos a mano. La EX1-Topology es
exactamente eso: un anillo de cuatro nodos con una rama lineal de dos
injertada sobre VM4.

Son funciones puras: reciben datos, devuelven datos, no tocan la
infraestructura.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    Slice,
    Nodo,
    Enlace,
    TipoTopologia,
    FLAVOR_POR_DEFECTO,
    obtener_flavor,
)


# ---------------------------------------------------------------------------
# Composición de topologías
# ---------------------------------------------------------------------------
@dataclass
class Injerto:
    """Una topología del catálogo aplicada sobre un nodo ya existente.

    ``sobre`` identifica el nodo raíz y acepta cualquiera de las tres formas
    con que puede nombrarse un nodo: su clave corta (``"vm4"``), su nombre
    completo (``"demo-vm3"``) o su id (``"slice-004-n3"``).

    ``num_nodos`` es cuántos nodos NUEVOS añade el injerto; el nodo raíz no
    se cuenta porque ya existe.
    """

    sobre: str
    tipo: TipoTopologia
    num_nodos: int
    ramificacion: int = 2      # solo lo usa la topología ÁRBOL


def _figura_a_pares(tipo: TipoTopologia, ids: list[str],
                    ramificacion: int = 2) -> list[tuple[str, str]]:
    """Dada una figura (lista ordenada de ids), devuelve sus pares enlazados.

    Es el único punto donde vive la forma de cada topología del catálogo, de
    modo que añadir una nueva sea añadir una rama aquí.

    ``ramificacion`` solo importa para ÁRBOL: cuántos hijos tiene cada nodo
    (el primer id es la raíz y los demás se reparten por niveles).
    """
    if tipo == TipoTopologia.LINEAL:
        if len(ids) < 2:
            raise ValueError("Una topología lineal necesita al menos 2 nodos")
        return [(ids[i], ids[i + 1]) for i in range(len(ids) - 1)]

    if tipo == TipoTopologia.ANILLO:
        if len(ids) < 3:
            raise ValueError("Un anillo necesita al menos 3 nodos para no duplicar el enlace")
        return [(ids[i], ids[(i + 1) % len(ids)]) for i in range(len(ids))]

    if tipo == TipoTopologia.MALLA:
        if len(ids) < 3:
            raise ValueError("Una malla necesita al menos 3 nodos")
        return [(ids[i], ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids))]

    if tipo == TipoTopologia.ARBOL:
        if len(ids) < 3:
            raise ValueError("Un árbol necesita al menos 3 nodos")
        if ramificacion < 2:
            raise ValueError("El factor de ramificación de un árbol debe ser al menos 2")
        return [(ids[(i - 1) // ramificacion], ids[i]) for i in range(1, len(ids))]

    if tipo == TipoTopologia.BUS:
        if len(ids) < 2:
            raise ValueError("Un bus necesita al menos 2 nodos")
        # Misma forma de cadena que LINEAL: n-1 pares consecutivos. Lo que
        # distingue al bus de un lineal es que, más adelante, Network
        # Manager le asigna UNA sola VLAN/red compartida a todos estos
        # pares (en vez de una VLAN distinta por par), de modo que los n
        # nodos terminan en un único dominio de difusión.
        return [(ids[i], ids[i + 1]) for i in range(len(ids) - 1)]

    raise ValueError(f"Tipo de topología no soportado como figura: {tipo}")


def _crear_enlaces(slice_id: str, tipo: TipoTopologia, ids: list[str],
                   contador: int, ramificacion: int = 2) -> tuple[list[Enlace], int]:
    """Materializa los pares de una figura como objetos Enlace."""
    enlaces: list[Enlace] = []
    for origen, destino in _figura_a_pares(tipo, ids, ramificacion):
        enlaces.append(
            Enlace(id=f"{slice_id}-e{contador}", nodo_origen=origen, nodo_destino=destino)
        )
        contador += 1
    return enlaces, contador


def _indice_de_claves(nodos: list[Nodo]) -> dict[str, str]:
    """Permite referirse a un nodo por clave corta, por nombre o por id."""
    indice: dict[str, str] = {}
    for i, nodo in enumerate(nodos):
        indice[f"vm{i + 1}"] = nodo.id      # clave corta, 1-indexada como el diagrama
        indice[nodo.nombre] = nodo.id
        indice[nodo.id] = nodo.id
    return indice


def _resolver(clave: str, indice: dict[str, str], contexto: str) -> str:
    try:
        return indice[clave]
    except KeyError:
        conocidas = ", ".join(k for k in indice if k.startswith("vm"))
        raise ValueError(
            f"{contexto}: el nodo '{clave}' no existe en el slice. "
            f"Claves válidas: {conocidas}"
        ) from None


# ---------------------------------------------------------------------------
# Construcción del slice
# ---------------------------------------------------------------------------
def construir_topologia(
    slice_id: str,
    nombre: str,
    owner: str,
    tipo: TipoTopologia,
    num_nodos: int,
    vcpu_por_nodo: int = 1,
    ram_mb_por_nodo: int = 512,
    imagen: str = "generic-linux.qcow2",
    flavor_map: dict[str, str] | None = None,
    internet_access: list[str] | None = None,
    injertos: list[Injerto] | None = None,
    ramificacion: int = 2,
) -> Slice:
    """
    Construye la estructura de nodos y enlaces de un slice. No crea nada en
    la infraestructura: solo arma el modelo de datos que los demás módulos
    usarán.

    Parámetros opcionales añadidos para la EX1-Topology:

    * ``flavor_map``      {clave_nodo: nombre_flavor}. Los nodos nombrados
                          toman vcpu, RAM, disco e imagen del catálogo; los
                          no nombrados conservan los valores por defecto.
    * ``internet_access`` claves de los nodos que necesitan salida a Internet
                          y acceso SSH entrante (R5).
    * ``injertos``        topologías adicionales aplicadas sobre un nodo ya
                          existente. Si hay alguno, el slice resultante queda
                          marcado como MIXTA.

    Las claves de nodo admiten tres formas: ``"vm1"`` (clave corta, igual que
    en el diagrama), el nombre completo del nodo o su id.
    """
    if num_nodos < 2:
        raise ValueError("Una topología necesita al menos 2 nodos")

    # --- 1. Nodos de la topología base -------------------------------------
    nodos = [
        Nodo(
            id=f"{slice_id}-n{i}",
            nombre=f"{nombre}-vm{i + 1}",
            vcpu=vcpu_por_nodo,
            ram_mb=ram_mb_por_nodo,
            imagen=imagen,
        )
        for i in range(num_nodos)
    ]

    # --- 2. Enlaces de la topología base ------------------------------------
    enlaces, contador = _crear_enlaces(
        slice_id, tipo, [n.id for n in nodos], contador=0, ramificacion=ramificacion
    )

    # --- 3. Injertos --------------------------------------------------------
    tipo_final = tipo
    for injerto in injertos or []:
        if injerto.num_nodos < 1:
            raise ValueError("Un injerto debe añadir al menos un nodo")

        raiz_id = _resolver(injerto.sobre, _indice_de_claves(nodos), "injerto")

        nuevos = [
            Nodo(
                id=f"{slice_id}-n{len(nodos) + k}",
                nombre=f"{nombre}-vm{len(nodos) + k + 1}",
                vcpu=vcpu_por_nodo,
                ram_mb=ram_mb_por_nodo,
                imagen=imagen,
            )
            for k in range(injerto.num_nodos)
        ]
        # la figura del injerto incluye la raíz, que ya existe
        figura = [raiz_id] + [n.id for n in nuevos]
        enlaces_injerto, contador = _crear_enlaces(
            slice_id, injerto.tipo, figura, contador, injerto.ramificacion
        )

        nodos.extend(nuevos)
        enlaces.extend(enlaces_injerto)
        tipo_final = TipoTopologia.MIXTA

    # --- 4. Flavors por nodo -------------------------------------------------
    indice = _indice_de_claves(nodos)
    nodos_por_id = {n.id: n for n in nodos}

    for clave, nombre_flavor in (flavor_map or {}).items():
        nodo = nodos_por_id[_resolver(clave, indice, "flavor_map")]
        f = obtener_flavor(nombre_flavor)
        nodo.flavor = f.nombre
        nodo.vcpu = f.vcpu
        nodo.ram_mb = f.ram_mb
        nodo.disco_gb = f.disco_gb
        nodo.imagen = f.imagen

    # --- 5. Acceso a Internet -------------------------------------------------
    for clave in internet_access or []:
        nodos_por_id[_resolver(clave, indice, "internet_access")].acceso_internet = True

    return Slice(
        id=slice_id,
        nombre=nombre,
        owner=owner,
        tipo_topologia=tipo_final,
        nodos=nodos,
        enlaces=enlaces,
    )


def construir_ex1_topology(
    slice_id: str,
    nombre: str,
    owner: str,
    zona_disponibilidad: str | None = None,
) -> Slice:
    """Atajo para la EX1-Topology, con la especificación exacta del diagrama.

    Anillo VM1–VM2–VM3–VM4–VM1 más rama lineal VM4–VM5–VM6.
    Flavor cirros en VM2 y VM6, ubuntu en el resto.
    Salida a Internet y SSH entrante solo en VM1 y VM3.
    """
    slc = construir_topologia(
        slice_id=slice_id,
        nombre=nombre,
        owner=owner,
        tipo=TipoTopologia.ANILLO,
        num_nodos=4,
        injertos=[Injerto(sobre="vm4", tipo=TipoTopologia.LINEAL, num_nodos=2)],
        flavor_map={
            "vm1": "ubuntu", "vm2": "cirros", "vm3": "ubuntu",
            "vm4": "ubuntu", "vm5": "ubuntu", "vm6": "cirros",
        },
        internet_access=["vm1", "vm3"],
    )
    slc.zona_disponibilidad = zona_disponibilidad
    return slc


# ---------------------------------------------------------------------------
# Plan de despliegue
# ---------------------------------------------------------------------------
def calcular_plan_despliegue(slc: Slice) -> dict:
    """
    Calcula el plan de despliegue: qué se crea primero, qué después, y en
    qué orden se haría el rollback si algo falla a la mitad.

    Regla usada (coherente con el Entregable 2): primero se crean los
    segmentos de red de cada enlace, porque cada VM necesita saber a qué
    interfaz(es) conectarse al nacer; luego se crean las VMs.

    El plan se expresa de dos formas equivalentes:

    * ``orden_creacion``  secuencia plana, para ejecución en serie.
    * ``olas``            agrupación por dependencias, para ejecución en
                          paralelo: dentro de una ola nada depende de nada,
                          de modo que todas sus operaciones pueden lanzarse
                          a la vez. En la EX1-Topology son dos olas —las seis
                          redes y luego las seis VMs— en lugar de doce pasos.

    Devuelve un plan explícito en vez de ejecutar nada, para que sea
    auditable y para que el rollback siga el orden inverso exacto de lo ya
    creado.
    """
    pasos_red = [{"accion": "crear_red", "enlace_id": e.id} for e in slc.enlaces]
    pasos_vm = [{"accion": "crear_vm", "nodo_id": n.id} for n in slc.nodos]

    orden_creacion = pasos_red + pasos_vm

    return {
        "slice_id": slc.id,
        "orden_creacion": orden_creacion,
        "olas": [pasos_red, pasos_vm],
        # El rollback deshace exactamente lo contrario, en orden inverso: las
        # VMs primero y las redes después, porque una red no puede eliminarse
        # mientras siga teniendo VMs conectadas.
        "orden_rollback": list(reversed(orden_creacion)),
        "olas_rollback": [list(reversed(pasos_vm)), list(reversed(pasos_red))],
    }
