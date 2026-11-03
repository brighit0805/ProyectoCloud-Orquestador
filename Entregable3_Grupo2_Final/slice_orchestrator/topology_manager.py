"""
topology_manager.py
--------------------
Topology Manager (submódulo de B) Orquestación).

Responsabilidad (según la arquitectura del Entregable 2): evaluar y
calcular CÓMO se va a desplegar un slice — no decide en qué host físico
va cada VM (eso es trabajo de VM Placement), decide la forma de la red
lógica del slice, el orden de creación, y la estrategia de rollback.

Este módulo cubre las dos topologías pedidas para este entregable:
lineal y anillo. Son funciones puras: reciben datos, devuelven datos,
no tocan la infraestructura.
"""

from __future__ import annotations

from .models import Slice, Nodo, Enlace, TipoTopologia


def construir_topologia(
    slice_id: str,
    nombre: str,
    owner: str,
    tipo: TipoTopologia,
    num_nodos: int,
    vcpu_por_nodo: int = 1,
    ram_mb_por_nodo: int = 512,
    imagen: str = "generic-linux.qcow2",
) -> Slice:
    """
    Construye la estructura de nodos y enlaces de un slice, según el
    tipo de topología solicitado. No crea nada en la infraestructura:
    solo arma el modelo de datos (Slice) que los demás módulos usarán.
    """
    if num_nodos < 2:
        raise ValueError("Una topología lineal o de anillo necesita al menos 2 nodos")
    if tipo == TipoTopologia.ANILLO and num_nodos < 3:
        raise ValueError("Un anillo necesita al menos 3 nodos para no duplicar el enlace")

    nodos = [
        Nodo(
            id=f"{slice_id}-n{i}",
            nombre=f"{nombre}-vm{i}",
            vcpu=vcpu_por_nodo,
            ram_mb=ram_mb_por_nodo,
            imagen=imagen,
        )
        for i in range(num_nodos)
    ]

    enlaces: list[Enlace] = []
    if tipo == TipoTopologia.LINEAL:
        # n0 -- n1 -- n2 -- ... -- n(k-1)
        for i in range(num_nodos - 1):
            enlaces.append(
                Enlace(
                    id=f"{slice_id}-e{i}",
                    nodo_origen=nodos[i].id,
                    nodo_destino=nodos[i + 1].id,
                )
            )
    elif tipo == TipoTopologia.ANILLO:
        # n0 -- n1 -- n2 -- ... -- n(k-1) -- n0
        for i in range(num_nodos):
            j = (i + 1) % num_nodos
            enlaces.append(
                Enlace(
                    id=f"{slice_id}-e{i}",
                    nodo_origen=nodos[i].id,
                    nodo_destino=nodos[j].id,
                )
            )
    else:
        raise ValueError(f"Tipo de topología no soportado en este entregable: {tipo}")

    return Slice(
        id=slice_id,
        nombre=nombre,
        owner=owner,
        tipo_topologia=tipo,
        nodos=nodos,
        enlaces=enlaces,
    )


def calcular_plan_despliegue(slc: Slice) -> dict:
    """
    Calcula el plan de despliegue: qué se crea primero, qué después,
    y en qué orden se haría el rollback si algo falla a la mitad.

    Regla usada (coherente con el Entregable 2): primero se crean los
    segmentos de red de cada enlace, porque cada VM necesita saber a
    qué interfaz(es) conectarse al nacer; luego se crean las VMs.

    Devuelve un plan explícito en vez de ejecutar nada directamente,
    para que sea auditable y para que el DriverManager pueda hacer
    rollback siguiendo el orden inverso exacto de lo ya creado.
    """
    pasos_red = [{"accion": "crear_red", "enlace_id": e.id} for e in slc.enlaces]
    pasos_vm = [{"accion": "crear_vm", "nodo_id": n.id} for n in slc.nodos]

    return {
        "slice_id": slc.id,
        "orden_creacion": pasos_red + pasos_vm,
        # el rollback deshace exactamente lo contrario, en orden inverso
        "orden_rollback": list(reversed(pasos_vm + pasos_red)),
    }
