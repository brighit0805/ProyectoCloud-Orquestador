"""
orchestrator.py
----------------
Función de orquestación de nivel superior. Encadena, EN ESTE ORDEN,
todos los módulos ya construidos, exactamente como lo describe la
arquitectura del Entregable 2 (sección 4/5, cadena Topology Manager ->
VM Placement -> Network Manager -> DriverManager/Driver Linux):

    1. topology_manager.construir_topologia()   -> arma el Slice (nodos+enlaces)
    2. topology_manager.calcular_plan_despliegue() -> orden de creación/rollback
    3. vm_placement.asignar_hosts()/confirmar_asignacion() -> host físico por VM
    4. network_manager.asignar_redes()/redes_por_nodo()    -> bridge/VLAN por enlace
    5. driver_linux.crear_red() / crear_vm()   -> ejecución real/simulada

No hay ninguna API/HTTP aquí: `desplegar_slice` y `eliminar_slice` son
funciones Python normales, invocables directamente, tal como pide el
enunciado de este laboratorio ("sin APIs, sólo funciones").

Manejo de fallas: si una VM falla a mitad de despliegue, se ejecuta el
`orden_rollback` calculado por Topology Manager, deshaciendo solo lo que
alcanzó a crearse (RF-18 del Entregable 1).
"""

from __future__ import annotations
import logging

from .models import Slice, Host, EstadoSlice, TipoTopologia
from . import topology_manager
from . import vm_placement
from . import network_manager
from .driver_linux import DriverLinux

logger = logging.getLogger("orchestrator")
_DRIVER_PREDETERMINADO = DriverLinux()
_VLAN_ALLOCATOR_PREDETERMINADO = network_manager.VLANAllocator()


class DespliegueFallidoError(Exception):
    """Se lanza cuando el despliegue falla y ya se ejecutó el rollback."""


def desplegar_slice(
    slice_id: str,
    nombre: str,
    owner: str,
    tipo: TipoTopologia,
    num_nodos: int,
    hosts: list[Host],
    zona_disponibilidad: str | None = None,
    vcpu_por_nodo: int = 1,
    ram_mb_por_nodo: int = 512,
    imagen: str = "generic-linux.qcow2",
    driver: DriverLinux | None = None,
    vlan_allocator: network_manager.VLANAllocator | None = None,
) -> Slice:
    """
    Despliega un slice completo (lineal o anillo) distribuido entre
    varios hosts Linux. Devuelve el Slice ya con estado ACTIVO, hosts y
    redes asignadas. Si algo falla, hace rollback y relanza el error
    original envuelto en DespliegueFallidoError.
    """
    driver = driver or _DRIVER_PREDETERMINADO
    vlan_allocator = vlan_allocator or _VLAN_ALLOCATOR_PREDETERMINADO

    # 1) Topology Manager: arma la forma del slice (nodos + enlaces)
    slc = topology_manager.construir_topologia(
        slice_id=slice_id,
        nombre=nombre,
        owner=owner,
        tipo=tipo,
        num_nodos=num_nodos,
        vcpu_por_nodo=vcpu_por_nodo,
        ram_mb_por_nodo=ram_mb_por_nodo,
        imagen=imagen,
    )
    slc.zona_disponibilidad = zona_disponibilidad
    plan = topology_manager.calcular_plan_despliegue(slc)
    logger.info("Plan de despliegue calculado para slice %s: %d pasos", slc.id, len(plan["orden_creacion"]))

    # 2) VM Placement: decide el host físico de cada VM (multi-host)
    asignacion = vm_placement.asignar_hosts(slc, hosts)
    hosts_por_id = {h.id: h for h in hosts}
    if len(hosts_por_id) != len(hosts):
        raise ValueError("Los identificadores de host deben ser únicos")

    # El compromiso de capacidad y la reserva de VLAN ocurren antes de tocar
    # infraestructura, pero ambos se revierten si cualquier paso falla.
    vm_placement.confirmar_asignacion(slc, hosts, asignacion)
    ejecutados: list[dict[str, str]] = []
    try:
        # 3) Network Manager: VLAN única por enlace.
        network_manager.asignar_redes(slc, vlan_allocator)
        redes_por_nodo = network_manager.redes_por_nodo(slc)

        # 4) Driver Linux: ejecuta el plan y registra cada recurso concreto
        # inmediatamente, para poder revertir incluso un fallo parcial.
        for paso in plan["orden_creacion"]:
            if paso["accion"] == "crear_red":
                enlace = next(e for e in slc.enlaces if e.id == paso["enlace_id"])
                # una red puede tocar dos hosts distintos (VLAN provider
                # compartida a nivel de clúster); la creamos en el host
                # de cada extremo para asegurar conectividad L2 entre ellos
                host_origen = hosts_por_id[slc.nodo(enlace.nodo_origen).host_id]
                host_destino = hosts_por_id[slc.nodo(enlace.nodo_destino).host_id]
                if enlace.vlan_id is None:
                    raise RuntimeError(f"El enlace {enlace.id} no tiene VLAN asignada")
                for h in {host_origen.id: host_origen, host_destino.id: host_destino}.values():
                    driver.crear_red(h.uri_libvirt, enlace.nombre_red, enlace.vlan_id)
                    ejecutados.append({"accion": "crear_red", "enlace_id": enlace.id, "host_id": h.id})

            elif paso["accion"] == "crear_vm":
                nodo = slc.nodo(paso["nodo_id"])
                host = hosts_por_id[nodo.host_id]
                redes_vm = []
                for enlace in slc.enlaces:
                    if nodo.id in (enlace.nodo_origen, enlace.nodo_destino):
                        if enlace.nombre_red is None or enlace.vlan_id is None:
                            raise RuntimeError(f"Red incompleta para el enlace {enlace.id}")
                        redes_vm.append((enlace.nombre_red, enlace.vlan_id))
                driver.crear_vm(
                    host_uri=host.uri_libvirt,
                    nombre_vm=nodo.nombre,
                    vcpu=nodo.vcpu,
                    ram_mb=nodo.ram_mb,
                    disco_gb=nodo.disco_gb,
                    imagen=nodo.imagen,
                    redes=redes_vm,
                )
                nodo.estado = "activo"
                ejecutados.append({"accion": "crear_vm", "nodo_id": nodo.id, "host_id": host.id})

    except Exception as exc:
        logger.error("Falló el despliegue del slice %s en el paso %s: %s. Ejecutando rollback...",
                     slc.id, locals().get("paso", "preparación"), exc)
        slc.estado = EstadoSlice.ERROR
        _rollback(slc, hosts_por_id, ejecutados, driver)
        vm_placement.liberar_asignacion(slc, hosts)
        vlan_allocator.liberar(slc.id)
        raise DespliegueFallidoError(f"Despliegue de slice '{slc.id}' abortado y revertido: {exc}") from exc

    slc.estado = EstadoSlice.ACTIVO
    logger.info("Slice %s desplegado correctamente en %d host(s)", slc.id, len({n.host_id for n in slc.nodos}))
    return slc


def _rollback(slc: Slice, hosts_por_id: dict[str, Host],
              ejecutados: list[dict[str, str]], driver: DriverLinux) -> None:
    """Deshace, en orden inverso, únicamente los pasos que sí llegaron a
    ejecutarse (idempotente: seguro de llamar aunque un paso ya se haya
    revertido)."""
    for paso in reversed(ejecutados):
        if paso["accion"] == "crear_vm":
            nodo = slc.nodo(paso["nodo_id"])
            host = hosts_por_id[paso["host_id"]]
            driver.eliminar_vm(host.uri_libvirt, nodo.nombre)
            nodo.estado = "pendiente"
        elif paso["accion"] == "crear_red":
            enlace = next(e for e in slc.enlaces if e.id == paso["enlace_id"])
            host = hosts_por_id[paso["host_id"]]
            driver.eliminar_red(host.uri_libvirt, enlace.nombre_red)


def eliminar_slice(slc: Slice, hosts: list[Host], driver: DriverLinux | None = None,
                   vlan_allocator: network_manager.VLANAllocator | None = None) -> None:
    """Elimina todas las VMs y redes de un slice ya desplegado."""
    if slc.estado == EstadoSlice.ELIMINADO:
        return
    hosts_por_id = {h.id: h for h in hosts}
    driver = driver or _DRIVER_PREDETERMINADO
    vlan_allocator = vlan_allocator or _VLAN_ALLOCATOR_PREDETERMINADO
    slc.estado = EstadoSlice.ELIMINANDO

    for nodo in slc.nodos:
        if nodo.host_id is None:
            continue
        host = hosts_por_id[nodo.host_id]
        driver.eliminar_vm(host.uri_libvirt, nodo.nombre)
        nodo.estado = "pendiente"

    for enlace in slc.enlaces:
        if enlace.nombre_red is None:
            continue
        host_origen = hosts_por_id[slc.nodo(enlace.nodo_origen).host_id]
        host_destino = hosts_por_id[slc.nodo(enlace.nodo_destino).host_id]
        for h in {host_origen.id: host_origen, host_destino.id: host_destino}.values():
            driver.eliminar_red(h.uri_libvirt, enlace.nombre_red)

    vm_placement.liberar_asignacion(slc, hosts)
    vlan_allocator.liberar(slc.id)
    slc.estado = EstadoSlice.ELIMINADO
    logger.info("Slice %s eliminado", slc.id)
