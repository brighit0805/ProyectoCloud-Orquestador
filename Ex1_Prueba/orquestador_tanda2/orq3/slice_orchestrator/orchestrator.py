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
import time

from .models import Slice, Host, EstadoSlice, TipoTopologia
from . import topology_manager
from . import vm_placement
from . import network_manager
from . import direccionamiento
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
    flavor_map: dict[str, str] | None = None,
    internet_access: list[str] | None = None,
    injertos: "list[topology_manager.Injerto] | None" = None,
    estrategia: vm_placement.EstrategiaPlacement = vm_placement.EstrategiaPlacement.BALANCEADO,
) -> Slice:
    """
    Construye y despliega un slice completo distribuido entre varios hosts
    Linux. Devuelve el Slice ya con estado ACTIVO, hosts y redes asignadas.
    Si algo falla, hace rollback y relanza el error original envuelto en
    DespliegueFallidoError.

    Los parámetros ``flavor_map``, ``internet_access`` e ``injertos`` se pasan
    tal cual a Topology Manager; ``estrategia`` a VM Placement.
    """
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
        flavor_map=flavor_map,
        internet_access=internet_access,
        injertos=injertos,
    )
    slc.zona_disponibilidad = zona_disponibilidad
    return desplegar_slice_existente(
        slc, hosts, driver=driver, vlan_allocator=vlan_allocator, estrategia=estrategia
    )


def desplegar_slice_existente(
    slc: Slice,
    hosts: list[Host],
    driver: DriverLinux | None = None,
    vlan_allocator: network_manager.VLANAllocator | None = None,
    estrategia: vm_placement.EstrategiaPlacement = vm_placement.EstrategiaPlacement.BALANCEADO,
    config_acceso: dict | None = None,
    clave_ssh_publica: str | None = None,
    reintentos: int = 1,
    espera_reintento_s: float = 2.0,
) -> Slice:
    """
    ``config_acceso`` (opcional) activa la interfaz de acceso a Internet de los
    nodos con ``acceso_internet``: {"ips": {"vm1": "10.0.10.31", ...},
    "prefijo": 24, "gateway": "10.0.10.100", "vlan": None}.

    ``reintentos``: veces que se intenta cada operación del driver antes de
    darla por fallida (1 = sin reintentos). Solo si se agotan se hace rollback.

    Despliega un Slice ya construido —por ejemplo uno en estado BORRADOR, o
    el que devuelve ``construir_ex1_topology``— sin volver a generar su
    topología. Es el punto de entrada que usa la demostración de la EX1.
    """
    driver = driver or _DRIVER_PREDETERMINADO
    vlan_allocator = vlan_allocator or _VLAN_ALLOCATOR_PREDETERMINADO

    slc.estado = EstadoSlice.CREANDO
    plan = topology_manager.calcular_plan_despliegue(slc)
    logger.info("Plan de despliegue calculado para slice %s: %d pasos", slc.id, len(plan["orden_creacion"]))

    # 2) VM Placement: decide el host físico de cada VM (multi-host)
    asignacion = vm_placement.asignar_hosts(slc, hosts, estrategia)
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
                    _con_reintentos(
                        lambda h=h: driver.crear_red(h.uri_libvirt, enlace.nombre_red, enlace.vlan_id),
                        f"crear_red {enlace.nombre_red} en {h.id}", reintentos, espera_reintento_s)
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
                # Direccionamiento: IP y MAC de cada interfaz, y semilla cloud-init
                interfaces = direccionamiento.interfaces_de(slc, nodo)
                acceso_nic, acceso_drv = _interfaz_acceso(slc, nodo, config_acceso)
                semilla = direccionamiento.archivos_semilla(
                    nodo, interfaces, acceso_nic, clave_ssh_publica)
                copiada = _con_reintentos(
                    lambda: driver.asegurar_imagen(host.uri_libvirt, nodo.imagen),
                    f"copiar imagen {nodo.imagen} a {host.id}", reintentos, espera_reintento_s)
                if copiada:
                    ejecutados.append({"accion": "asegurar_imagen", "host_id": host.id,
                                       "imagen": nodo.imagen})
                _con_reintentos(
                    lambda: driver.crear_vm(
                        host_uri=host.uri_libvirt,
                        nombre_vm=nodo.nombre,
                        vcpu=nodo.vcpu,
                        ram_mb=nodo.ram_mb,
                        disco_gb=nodo.disco_gb,
                        imagen=nodo.imagen,
                        redes=redes_vm,
                        macs=[i.mac for i in interfaces],
                        semilla=semilla,
                        acceso=acceso_drv,
                    ),
                    f"crear_vm {nodo.nombre} en {host.id}", reintentos, espera_reintento_s)
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


def _con_reintentos(operacion, descripcion: str, reintentos: int, espera_s: float):
    """Ejecuta ``operacion``; si falla la repite hasta ``reintentos`` veces en total.
    Es seguro porque las operaciones del driver son idempotentes."""
    for intento in range(1, max(1, reintentos) + 1):
        try:
            return operacion()
        except Exception as exc:        # noqa: BLE001
            if intento >= reintentos:
                raise
            logger.warning("%s falló (intento %d/%d): %s. Reintentando en %.0f s...",
                           descripcion, intento, reintentos, exc, espera_s)
            time.sleep(espera_s)


def _interfaz_acceso(slc: Slice, nodo, config: dict | None):
    """Interfaz extra hacia el headnode para los nodos con salida a Internet.
    Devuelve (InterfazAcceso para la semilla, dict para el driver) o (None, None)."""
    if not config or not nodo.acceso_internet:
        return None, None
    if config.get("red_libvirt"):                   # NIC NAT de libvirt con DHCP
        mac = direccionamiento.mac_acceso(slc.id, nodo.id)
        nic = direccionamiento.InterfazAcceso(mac=mac, red_libvirt=config["red_libvirt"])
        return nic, {"mac": mac, "red_libvirt": config["red_libvirt"]}
    clave = nodo.nombre.rsplit("-", 1)[-1]          # "demo-ex1-vm1" -> "vm1"
    ip = config["ips"].get(clave)
    if ip is None:
        raise ValueError(f"config_acceso no define IP para {clave}")
    mac = direccionamiento.mac_acceso(slc.id, nodo.id)
    nic = direccionamiento.InterfazAcceso(
        mac=mac, ip=ip, prefijo=config.get("prefijo", 24),
        gateway=config["gateway"], vlan=config.get("vlan"))
    return nic, {"vlan": config.get("vlan"), "mac": mac}


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
        elif paso["accion"] == "asegurar_imagen":
            host = hosts_por_id[paso["host_id"]]
            driver.liberar_imagen_si_huerfana(host.uri_libvirt, paso["imagen"])


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

    # Borrado inteligente: la copia de la imagen base se elimina de cada worker
    # donde ya no quede ninguna VM que dependa de ella.
    for host_id, imagen in sorted({(n.host_id, n.imagen) for n in slc.nodos if n.host_id}):
        driver.liberar_imagen_si_huerfana(hosts_por_id[host_id].uri_libvirt, imagen)

    vm_placement.liberar_asignacion(slc, hosts)
    vlan_allocator.liberar(slc.id)
    slc.estado = EstadoSlice.ELIMINADO
    logger.info("Slice %s eliminado", slc.id)
