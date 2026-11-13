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

Verificación asíncrona (hallazgo del 12-nov, clúster real): un driver cuya
creación es asíncrona (OpenStack: Nova responde 202 "aceptado" antes de que
la VM termine de construirse) puede aceptar la orden y, en segundo plano,
terminar la VM en estado ERROR real -- sin que crear_vm() llegue a lanzar
ninguna excepción. Si el driver expone el método opcional `verificar_vm()`
(ver driver_base.py), justo después de cada crear_vm() se espera aquí a que
la plataforma confirme ACTIVE; si confirma ERROR o nunca llega a tiempo, se
lanza una excepción que dispara el MISMO mecanismo de rollback que ya existe
para fallas síncronas (no es un camino nuevo, es extender el existente a
este tipo de falla). DriverLinux no se ve afectado: no implementa
`verificar_vm()` porque su creación ya es síncrona.
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
    intentos_verificacion: int = 20,
    espera_verificacion_s: float = 3.0,
) -> Slice:
    """
    ``config_acceso`` (opcional) activa la interfaz de acceso a Internet de los
    nodos con ``acceso_internet``: {"ips": {"vm1": "10.0.10.31", ...},
    "prefijo": 24, "gateway": "10.0.10.100", "vlan": None}.

    ``reintentos``: veces que se intenta cada operación del driver antes de
    darla por fallida (1 = sin reintentos). Solo si se agotan se hace rollback.

    ``intentos_verificacion``/``espera_verificacion_s``: solo aplican si el
    driver implementa ``verificar_vm()`` (ver driver_base.py). Tras cada
    crear_vm(), se consulta el estado real hasta ``intentos_verificacion``
    veces (esperando ``espera_verificacion_s`` segundos entre cada una) hasta
    que la plataforma confirme ACTIVE. Si confirma ERROR o se agotan los
    intentos sin confirmar ACTIVE, se aborta el despliegue y se revierte todo
    lo creado hasta ese punto (mismo mecanismo que cualquier otra falla).

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
    # Se propaga el driver para que, en estrategia OBJETIVO, el ResourceMonitor
    # del scheduler refresque el estado REAL de los hosts justo antes de
    # decidir (Lab 7). En las estrategias clasicas el driver simplemente se
    # ignora (asignar_hosts solo lo usa para OBJETIVO).
    asignacion = vm_placement.asignar_hosts(slc, hosts, estrategia, driver=driver)
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
        # Drivers con noción de proyecto (OpenStack) lo preparan aquí; DriverLinux no lo necesita.
        if hasattr(driver, "preparar_slice"):
            driver.preparar_slice(slc)

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
                        # En un bus varios enlaces de la cadena comparten la misma
                        # red (ver network_manager.asignar_redes); sin este chequeo
                        # un nodo intermedio terminaría con dos NIC duplicadas sobre
                        # la misma red en vez de la única interfaz que le corresponde.
                        tupla = (enlace.nombre_red, enlace.vlan_id)
                        if tupla not in redes_vm:
                            redes_vm.append(tupla)
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
                # Se registra ANTES de verificar: si la verificación de abajo
                # falla, el rollback (que recorre `ejecutados`) debe borrar
                # también esta VM, no solo las anteriores.
                ejecutados.append({"accion": "crear_vm", "nodo_id": nodo.id, "host_id": host.id})
                _verificar_vm_activa(
                    driver, host.uri_libvirt, nodo.nombre,
                    f"VM {nodo.nombre} en {host.id}", intentos_verificacion, espera_verificacion_s)
                nodo.estado = "activo"

    except Exception as exc:
        logger.error("Falló el despliegue del slice %s en el paso %s: %s. Ejecutando rollback...",
                     slc.id, locals().get("paso", "preparación"), exc)
        slc.estado = EstadoSlice.ERROR
        _rollback(slc, hosts_por_id, ejecutados, driver)
        _liberar_slice_driver(driver, slc)
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


_ESTADOS_TERMINALES_ERROR = {"ERROR"}
_ESTADOS_TERMINALES_OK = {"ACTIVE"}


def _verificar_vm_activa(driver, host_uri: str, nombre_vm: str, descripcion: str,
                         intentos: int, espera_s: float) -> None:
    """Espera a que la plataforma confirme que la VM recién creada quedó
    realmente ACTIVE, si el driver sabe consultarlo (``verificar_vm``,
    opcional -- ver driver_base.py). Si el driver no lo implementa (como
    DriverLinux, cuya creación ya es síncrona), no hace nada: se asume éxito,
    igual que antes de este chequeo.

    Lanza RuntimeError si la plataforma confirma ERROR, o si se agotan los
    intentos sin llegar a un estado terminal -- en ambos casos el llamador
    (desplegar_slice_existente) lo captura y dispara el rollback existente."""
    if not hasattr(driver, "verificar_vm"):
        return
    estado = "DESCONOCIDO"
    for intento in range(1, max(1, intentos) + 1):
        estado = driver.verificar_vm(host_uri, nombre_vm)
        if estado in _ESTADOS_TERMINALES_OK:
            return
        if estado in _ESTADOS_TERMINALES_ERROR:
            raise RuntimeError(
                f"{descripcion}: la plataforma confirma estado '{estado}' real "
                f"(no ACTIVE) tras la creación -- la creación asíncrona falló "
                f"aunque la orden inicial haya sido aceptada")
        if intento < intentos:
            time.sleep(espera_s)
    raise RuntimeError(
        f"{descripcion}: tras {intentos} verificaciones no se confirmó ACTIVE "
        f"(último estado real reportado: '{estado}')")


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


def _liberar_slice_driver(driver, slc: Slice) -> None:
    """Pide al driver (si lo soporta) que libere lo propio del slice, p. ej. el
    proyecto de OpenStack. Un fallo aquí se registra pero no oculta el error original."""
    if hasattr(driver, "liberar_slice"):
        try:
            driver.liberar_slice(slc.id)
        except Exception as exc:        # noqa: BLE001
            logger.warning("No se pudo liberar el slice %s en el driver: %s", slc.id, exc)


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

    # Hallazgo del 12-nov (prueba real contra el clúster): con el driver
    # OpenStack real, cada invocación de la CLI crea una instancia NUEVA del
    # driver -- preparar_slice() (llamado durante el despliegue) guarda el
    # project_id solo en memoria de esa instancia, nunca en el estado
    # persistido. Si se elimina el slice desde otra invocación/proceso, el
    # driver nuevo no sabe a qué proyecto de OpenStack pertenecen las VMs, y
    # antes de este fix 'eliminar_vm' simplemente no hacía nada (sin avisar),
    # dejando VMs reales corriendo mientras el orquestador reportaba
    # "eliminado". preparar_slice() ya es idempotente (reutiliza el proyecto
    # si ya existe, por nombre), así que repetir la llamada aquí es seguro.
    if hasattr(driver, "preparar_slice"):
        driver.preparar_slice(slc)

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

    _liberar_slice_driver(driver, slc)
    vm_placement.liberar_asignacion(slc, hosts)
    vlan_allocator.liberar(slc.id)
    slc.estado = EstadoSlice.ELIMINADO
    logger.info("Slice %s eliminado", slc.id)


# ---------------------------------------------------------------------------
# Edición en vivo de un slice ya ACTIVO (13-nov)
# ---------------------------------------------------------------------------
class EdicionEnVivoError(Exception):
    """Falló aplicar la edición contra la infraestructura real; se revirtió
    lo que esta llamada alcanzó a agregar (nunca lo que ya existía antes)."""


def _par(e) -> frozenset:
    return frozenset((e.nodo_origen, e.nodo_destino))


def editar_slice_desplegado(
    slc_viejo: Slice,
    slc_nuevo: Slice,
    hosts: list[Host],
    driver: DriverLinux | None = None,
    vlan_allocator: network_manager.VLANAllocator | None = None,
    estrategia: vm_placement.EstrategiaPlacement = vm_placement.EstrategiaPlacement.BALANCEADO,
    config_acceso: dict | None = None,
    clave_ssh_publica: str | None = None,
    reintentos: int = 1,
    espera_reintento_s: float = 2.0,
    intentos_verificacion: int = 20,
    espera_verificacion_s: float = 3.0,
) -> Slice:
    """
    Aplica sobre un slice YA DESPLEGADO (ACTIVO) la diferencia entre su grafo
    actual (``slc_viejo``, tal cual está corriendo: con host_id/vlan_id/
    nombre_red asignados) y el grafo editado (``slc_nuevo``, mismo id,
    construido aplicando composicion.* sobre una COPIA de slc_viejo que
    conserva esos campos para lo que no cambió -- a diferencia del camino de
    edición en BORRADOR, aquí NO se borran host_id/vlan_id/nombre_red antes
    de llamar a esta función). Crea/conecta solo lo que se agregó y borra/
    desconecta solo lo que se quitó -- las VMs y enlaces sin cambios no se
    tocan.

    Limitación conocida: si el slice editado deja de clasificar como BUS
    (clasificar() no reconoce esa figura, solo lineal/anillo/malla/árbol),
    cada enlace nuevo recibe su propia VLAN, igual que las demás topologías
    -- la edición en vivo de un slice BUS no preserva el segmento
    compartido. No bloqueante hoy: bus no se ofrece como injerto editable.

    Lanza EdicionEnVivoError si algo falla al AGREGAR; en ese caso revierte
    únicamente lo que esta llamada alcanzó a crear/conectar (las VMs/enlaces
    que ya estaban corriendo antes de editar quedan intactos). Un fallo al
    QUITAR algo (después de que agregar ya terminó bien) no revierte las
    altas ya aplicadas -- se registra como advertencia y se informa qué
    recurso pudo haber quedado sin limpiar, en vez de deshacer trabajo que
    sí se completó.
    """
    driver = driver or _DRIVER_PREDETERMINADO
    vlan_allocator = vlan_allocator or _VLAN_ALLOCATOR_PREDETERMINADO
    hosts_por_id = {h.id: h for h in hosts}

    # Mismo hallazgo del 12-nov que ya resolvían desplegar_slice_existente/
    # eliminar_slice: 'slice editar' corre en un proceso de CLI nuevo, con
    # una instancia de DriverOpenStack que nunca llamó a preparar_slice() y
    # por lo tanto no conoce el proyecto del slice (proyecto_id solo vive en
    # memoria de esa instancia). Sin este llamado, conectar_interfaz/crear_red
    # fallan con "No hay proyecto activo" aunque el slice sí exista y esté
    # ACTIVO. preparar_slice() es idempotente (reutiliza el proyecto si ya
    # existe), así que llamarlo aquí es seguro y no toca nada ya desplegado.
    # Se le pasa slc_VIEJO (no slc_nuevo): preparar_slice calcula
    # direccionamiento.interfaces_de() de cada nodo/enlace que recibe, y los
    # enlaces/nodos nuevos de slc_nuevo todavía no tienen vlan_id/nombre_red
    # asignado en este punto (eso ocurre más abajo) -- usar slc_nuevo aquí
    # rompería con "el enlace no tiene red/VLAN asignada" antes de tiempo.
    if hasattr(driver, "preparar_slice"):
        driver.preparar_slice(slc_viejo)

    ids_viejos = {n.id for n in slc_viejo.nodos}
    ids_nuevos = {n.id for n in slc_nuevo.nodos}
    nodos_agregados = [n for n in slc_nuevo.nodos if n.id not in ids_viejos]
    nodos_quitados = [n for n in slc_viejo.nodos if n.id not in ids_nuevos]

    pares_viejos = {_par(e): e for e in slc_viejo.enlaces}
    pares_nuevos = {_par(e): e for e in slc_nuevo.enlaces}
    enlaces_agregados = [e for par, e in pares_nuevos.items() if par not in pares_viejos]
    enlaces_quitados = [e for par, e in pares_viejos.items() if par not in pares_nuevos]

    nodos_por_id_nuevo = {n.id: n for n in slc_nuevo.nodos}
    ejecutados: list[dict] = []

    try:
        # --- 1) Colocar los nodos nuevos (host físico), sin tocar los que ya corrían ---
        if nodos_agregados:
            mini = Slice(id=slc_nuevo.id, nombre=slc_nuevo.nombre, owner=slc_nuevo.owner,
                        tipo_topologia=slc_nuevo.tipo_topologia, nodos=nodos_agregados,
                        enlaces=[], zona_disponibilidad=slc_nuevo.zona_disponibilidad)
            asignacion = vm_placement.asignar_hosts(mini, hosts, estrategia, driver=driver)
            vm_placement.confirmar_asignacion(mini, hosts, asignacion)
            for n in nodos_agregados:
                ejecutados.append({"accion": "coloco_nodo", "nodo_id": n.id})

        # --- 2) Reservar VLAN + crear la red de cada enlace nuevo ---
        for enlace in enlaces_agregados:
            vlan_id = vlan_allocator.reservar_una(slc_nuevo.id)
            enlace.vlan_id = vlan_id
            enlace.nombre_red = f"br-{slc_nuevo.id}-vivo-vlan{vlan_id}"
            ejecutados.append({"accion": "vlan_vivo", "vlan_id": vlan_id})
            n_origen = nodos_por_id_nuevo[enlace.nodo_origen]
            n_destino = nodos_por_id_nuevo[enlace.nodo_destino]
            host_origen = hosts_por_id[n_origen.host_id]
            host_destino = hosts_por_id[n_destino.host_id]
            for h in {host_origen.id: host_origen, host_destino.id: host_destino}.values():
                _con_reintentos(
                    lambda h=h: driver.crear_red(h.uri_libvirt, enlace.nombre_red, enlace.vlan_id),
                    f"crear_red {enlace.nombre_red} en {h.id}", reintentos, espera_reintento_s)
                ejecutados.append({"accion": "crear_red", "enlace_id": enlace.id, "host_id": h.id})

            # Si alguno de los dos extremos YA estaba corriendo (no es un nodo
            # nuevo de este mismo cambio), hay que conectarle la interfaz en
            # caliente -- si es nuevo, la recibirá al crearse en el paso 3.
            for n, host in ((n_origen, host_origen), (n_destino, host_destino)):
                if n.id in ids_viejos:
                    itf = next((i for i in direccionamiento.interfaces_de(slc_nuevo, n)
                               if i.enlace_id == enlace.id), None)
                    if itf is None:
                        raise RuntimeError(f"No se pudo calcular la interfaz del enlace {enlace.id}")
                    _con_reintentos(
                        lambda n=n, host=host, itf=itf: driver.conectar_interfaz(
                            host.uri_libvirt, n.nombre, enlace.nombre_red, enlace.vlan_id,
                            mac=itf.mac, ip=itf.cidr.split("/")[0]),
                        f"conectar_interfaz {n.nombre}:{enlace.nombre_red}", reintentos, espera_reintento_s)
                    ejecutados.append({"accion": "conectar_interfaz", "nodo_id": n.id,
                                       "host_id": host.id, "nombre_red": enlace.nombre_red})

        # --- 3) Crear las VMs nuevas, con TODAS sus interfaces (viejas+nuevas) ---
        for nodo in nodos_agregados:
            host = hosts_por_id[nodo.host_id]
            redes_vm = []
            for enlace in slc_nuevo.enlaces:
                if nodo.id in (enlace.nodo_origen, enlace.nodo_destino):
                    if enlace.nombre_red is None or enlace.vlan_id is None:
                        raise RuntimeError(f"Red incompleta para el enlace {enlace.id}")
                    tupla = (enlace.nombre_red, enlace.vlan_id)
                    if tupla not in redes_vm:
                        redes_vm.append(tupla)
            interfaces = direccionamiento.interfaces_de(slc_nuevo, nodo)
            acceso_nic, acceso_drv = _interfaz_acceso(slc_nuevo, nodo, config_acceso)
            semilla = direccionamiento.archivos_semilla(nodo, interfaces, acceso_nic, clave_ssh_publica)
            copiada = _con_reintentos(
                lambda: driver.asegurar_imagen(host.uri_libvirt, nodo.imagen),
                f"copiar imagen {nodo.imagen} a {host.id}", reintentos, espera_reintento_s)
            if copiada:
                ejecutados.append({"accion": "asegurar_imagen", "host_id": host.id, "imagen": nodo.imagen})
            _con_reintentos(
                lambda: driver.crear_vm(
                    host_uri=host.uri_libvirt, nombre_vm=nodo.nombre, vcpu=nodo.vcpu,
                    ram_mb=nodo.ram_mb, disco_gb=nodo.disco_gb, imagen=nodo.imagen,
                    redes=redes_vm, macs=[i.mac for i in interfaces], semilla=semilla, acceso=acceso_drv),
                f"crear_vm {nodo.nombre} en {host.id}", reintentos, espera_reintento_s)
            ejecutados.append({"accion": "crear_vm", "nodo_id": nodo.id, "host_id": host.id})
            _verificar_vm_activa(
                driver, host.uri_libvirt, nodo.nombre,
                f"VM {nodo.nombre} en {host.id}", intentos_verificacion, espera_verificacion_s)
            nodo.estado = "activo"

    except Exception as exc:
        logger.error("Falló la edición en vivo del slice %s: %s. Revirtiendo solo lo agregado...",
                     slc_nuevo.id, exc)
        _revertir_edicion(slc_nuevo, hosts_por_id, ejecutados, driver, vlan_allocator)
        raise EdicionEnVivoError(f"Edición del slice '{slc_nuevo.id}' abortada y revertida: {exc}") from exc

    # --- 4) Quitar lo que sobra: enlaces primero, luego nodos completos ---
    # (best-effort: un fallo aquí no deshace las altas ya confirmadas arriba)
    avisos: list[str] = []
    nombres_red_vivos = {e.nombre_red for e in slc_nuevo.enlaces if e.nombre_red}
    for enlace in enlaces_quitados:
        if enlace.nodo_origen not in ids_nuevos or enlace.nodo_destino not in ids_nuevos:
            continue    # su extremo se está borrando completo; lo maneja el paso de nodos
        try:
            for nid in (enlace.nodo_origen, enlace.nodo_destino):
                n_viejo = slc_viejo.nodo(nid)
                driver.desconectar_interfaz(hosts_por_id[n_viejo.host_id].uri_libvirt,
                                            n_viejo.nombre, enlace.nombre_red)
            if enlace.nombre_red not in nombres_red_vivos:
                for nid in (enlace.nodo_origen, enlace.nodo_destino):
                    n_viejo = slc_viejo.nodo(nid)
                    driver.eliminar_red(hosts_por_id[n_viejo.host_id].uri_libvirt, enlace.nombre_red)
                if enlace.vlan_id is not None:
                    vlan_allocator.liberar_una(slc_nuevo.id, enlace.vlan_id)
        except Exception as exc:       # noqa: BLE001
            avisos.append(f"no se pudo desconectar/borrar el enlace {enlace.id}: {exc}")
            logger.warning("Edición de %s: %s", slc_nuevo.id, avisos[-1])

    for nodo in nodos_quitados:
        try:
            host = hosts_por_id[nodo.host_id]
            driver.eliminar_vm(host.uri_libvirt, nodo.nombre)
            host.vcpu_usados = max(0, host.vcpu_usados - nodo.vcpu)
            host.ram_usados_mb = max(0, host.ram_usados_mb - nodo.ram_mb)
        except Exception as exc:       # noqa: BLE001
            avisos.append(f"no se pudo eliminar la VM {nodo.nombre}: {exc}")
            logger.warning("Edición de %s: %s", slc_nuevo.id, avisos[-1])
    for enlace in slc_viejo.enlaces:
        if _par(enlace) in pares_nuevos:
            continue
        if enlace.nodo_origen in ids_nuevos and enlace.nodo_destino in ids_nuevos:
            continue    # ya se manejó arriba (ambos extremos sobreviven)
        if enlace.nombre_red in nombres_red_vivos or enlace.nombre_red is None:
            continue
        try:
            host_o = hosts_por_id[slc_viejo.nodo(enlace.nodo_origen).host_id]
            host_d = hosts_por_id[slc_viejo.nodo(enlace.nodo_destino).host_id]
            for h in {host_o.id: host_o, host_d.id: host_d}.values():
                driver.eliminar_red(h.uri_libvirt, enlace.nombre_red)
            if enlace.vlan_id is not None:
                vlan_allocator.liberar_una(slc_nuevo.id, enlace.vlan_id)
        except Exception as exc:       # noqa: BLE001
            avisos.append(f"no se pudo borrar la red {enlace.nombre_red}: {exc}")
            logger.warning("Edición de %s: %s", slc_nuevo.id, avisos[-1])

    if avisos:
        logger.warning("Edición en vivo de %s terminó con %d advertencia(s) al limpiar "
                       "lo quitado (las altas sí se aplicaron): %s",
                       slc_nuevo.id, len(avisos), "; ".join(avisos))
    slc_nuevo.estado = EstadoSlice.ACTIVO
    logger.info("Slice %s editado en vivo: +%d nodo(s)/-%d nodo(s), +%d enlace(s)/-%d enlace(s)",
               slc_nuevo.id, len(nodos_agregados), len(nodos_quitados),
               len(enlaces_agregados), len(enlaces_quitados))
    return slc_nuevo


def _revertir_edicion(slc: Slice, hosts_por_id: dict[str, Host], ejecutados: list[dict],
                      driver: DriverLinux, vlan_allocator: network_manager.VLANAllocator) -> None:
    """Deshace, en orden inverso, SOLO lo que ``editar_slice_desplegado``
    alcanzó a agregar en esta llamada -- nunca lo que ya estaba corriendo
    antes de editar."""
    for paso in reversed(ejecutados):
        try:
            if paso["accion"] == "crear_vm":
                nodo = slc.nodo(paso["nodo_id"])
                driver.eliminar_vm(hosts_por_id[paso["host_id"]].uri_libvirt, nodo.nombre)
            elif paso["accion"] == "conectar_interfaz":
                nodo = slc.nodo(paso["nodo_id"])
                driver.desconectar_interfaz(hosts_por_id[paso["host_id"]].uri_libvirt,
                                            nodo.nombre, paso["nombre_red"])
            elif paso["accion"] == "crear_red":
                enlace = next(e for e in slc.enlaces if e.id == paso["enlace_id"])
                driver.eliminar_red(hosts_por_id[paso["host_id"]].uri_libvirt, enlace.nombre_red)
            elif paso["accion"] == "vlan_vivo":
                vlan_allocator.liberar_una(slc.id, paso["vlan_id"])
            elif paso["accion"] == "asegurar_imagen":
                driver.liberar_imagen_si_huerfana(hosts_por_id[paso["host_id"]].uri_libvirt, paso["imagen"])
            elif paso["accion"] == "coloco_nodo":
                nodo = slc.nodo(paso["nodo_id"])
                if nodo.host_id is not None:
                    host = hosts_por_id[nodo.host_id]
                    host.vcpu_usados = max(0, host.vcpu_usados - nodo.vcpu)
                    host.ram_usados_mb = max(0, host.ram_usados_mb - nodo.ram_mb)
                nodo.host_id = None
        except Exception as exc:       # noqa: BLE001
            logger.warning("No se pudo revertir el paso %s de la edición de %s: %s",
                           paso, slc.id, exc)
