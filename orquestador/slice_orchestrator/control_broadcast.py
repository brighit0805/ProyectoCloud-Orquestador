"""
control_broadcast.py
--------------------
Broadcast Control (submódulo de Network Manager, arquitectura sección 6.2).

R5 pide "limitar el broadcast solo a los puertos y servidores que tienen VMs
de la subred". Con VLAN por enlace sobre OVS, el broadcast de una VLAN ya
queda confinado a los puertos de esa VLAN DENTRO de un servidor (acción
NORMAL de OVS). Lo que faltaba es lo que sale del servidor y lo que una VM
puede inyectar:

1. Poda de VLANs en el uplink de cada servidor (OVSDB, ``trunks``): solo
   viajan hacia ovs1 las VLANs que ese servidor comparte con OTRO servidor.
   Una VLAN cuyas VMs están todas en el mismo servidor nunca sale de él.
2. Poda de VLANs en ovs1 (opcional, si se configura el switch): cada puerto
   hacia un servidor solo lleva las VLANs de las VMs de ese servidor, así un
   broadcast de la VLAN X no llega a servidores sin VMs de X.
3. Storm control por puerto de VM (OpenFlow 1.3 + meters de OVS, Clase 14):
   el tráfico broadcast y multicast que entra desde cada VM (se identifica
   por su MAC, que el orquestador fija) pasa por un meter que descarta lo que
   supere ``limite_pps`` paquetes por segundo. El resto del tráfico sigue por
   la acción NORMAL, sin cambios.

4. QinQ (802.1ad, pedido por Germán el 25-nov), si ``QINQ`` está activo en
   config_cluster: cada slice tiene una S-VLAN (etiqueta externa, TPID
   0x88a8) y cada enlace su C-VLAN (etiqueta interna, 0x8100, la de siempre).
   En cada servidor:

       VM --(access C)--> br-int --patch qi<S> (trunk de las C del slice)-->
       br-qinq --patch qe<S> (dot1q-tunnel tag=S)--> uplink (trunk de S) --> ovs1

   El puerto ``dot1q-tunnel`` pone la etiqueta del slice encima de la del
   enlace al salir y la quita al entrar. Hacia ovs1 solo viajan S-VLANs: el
   aislamiento entre slices lo da la etiqueta externa y la poda del uplink y
   de ovs1 se hace por slice.

El módulo es puro: calcula el plan a partir de los slices activos y genera
los comandos. Quien los ejecuta es el driver (``DriverLinux``), que los
recalcula completos tras cada despliegue, edición o eliminación: el estado
deseado siempre se deriva de los slices, así que reaplicarlo es idempotente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .direccionamiento import interfaces_de
from .models import EstadoSlice, Slice

BROADCAST_Y_MULTICAST = "01:00:00:00:00:00/01:00:00:00:00:00"   # bit de grupo de la MAC destino


@dataclass(frozen=True)
class PuertoVM:
    """Una interfaz de enlace de una VM: la fuente posible de una tormenta."""
    slice_id: str
    vm: str
    mac: str
    vlan: int


@dataclass
class PlanBroadcast:
    """Estado deseado del control de broadcast, derivado de los slices activos."""
    vlans_por_host: dict[str, set[int]] = field(default_factory=dict)
    puertos_por_host: dict[str, list[PuertoVM]] = field(default_factory=dict)
    # QinQ: S-VLAN de cada slice y, por host, las C-VLAN de cada slice.
    servicio_por_slice: dict[str, int] = field(default_factory=dict)
    vlans_por_slice_host: dict[str, dict[str, set[int]]] = field(default_factory=dict)

    def hosts_de_vlan(self, vlan: int) -> set[str]:
        return {h for h, vs in self.vlans_por_host.items() if vlan in vs}

    def vlans_que_salen(self, host: str) -> set[int]:
        """VLANs del host que también tienen VMs en otro servidor: las únicas
        que deben cruzar su uplink."""
        return {v for v in self.vlans_por_host.get(host, set()) if len(self.hosts_de_vlan(v)) > 1}

    def slices_que_salen(self, host: str) -> dict[str, set[int]]:
        """QinQ: por cada slice con VMs en ``host``, sus C-VLAN que cruzan el
        uplink. Un slice con todas sus VMs en el mismo servidor no aparece."""
        salen = self.vlans_que_salen(host)
        res: dict[str, set[int]] = {}
        for sid, vlans in self.vlans_por_slice_host.get(host, {}).items():
            if vlans & salen:
                res[sid] = vlans & salen
        return res

    def svlans_que_salen(self, host: str) -> set[int]:
        """QinQ: S-VLAN de los slices que cruzan el uplink de ``host``."""
        return {self.servicio_por_slice[s] for s in self.slices_que_salen(host)
                if s in self.servicio_por_slice}

    def resumen(self) -> list[dict[str, Any]]:
        filas = []
        for host in sorted(self.vlans_por_host):
            locales = self.vlans_por_host[host]
            salen = self.vlans_que_salen(host)
            filas.append({
                "host": host,
                "vlans": sorted(locales),
                "vlans_uplink": sorted(salen),
                "vlans_solo_locales": sorted(locales - salen),
                "svlans_uplink": sorted(self.svlans_que_salen(host)),
                "puertos_con_storm_control": len(self.puertos_por_host.get(host, [])),
            })
        return filas


def calcular_plan(slices: Iterable[Slice]) -> PlanBroadcast:
    """Plan a partir de los slices ACTIVOS con sus nodos ya ubicados."""
    plan = PlanBroadcast()
    for slc in slices:
        if slc.estado != EstadoSlice.ACTIVO:
            continue
        if slc.vlan_servicio is not None:
            plan.servicio_por_slice[slc.id] = slc.vlan_servicio
        for nodo in slc.nodos:
            if nodo.host_id is None:
                continue
            for itf in interfaces_de(slc, nodo):
                plan.vlans_por_host.setdefault(nodo.host_id, set()).add(itf.vlan)
                plan.vlans_por_slice_host.setdefault(nodo.host_id, {}).setdefault(
                    slc.id, set()).add(itf.vlan)
                plan.puertos_por_host.setdefault(nodo.host_id, []).append(
                    PuertoVM(slc.id, nodo.nombre, itf.mac, itf.vlan))
    for puertos in plan.puertos_por_host.values():
        puertos.sort(key=lambda p: (p.slice_id, p.vm, p.mac))
    return plan


@dataclass
class ConfigSwitch:
    """ovs1: el switch OVS que une los servidores del clúster Linux."""
    destino_ssh: str                          # p. ej. "ubuntu@192.168.201.15"
    puerto_por_host: dict[str, str]           # host_id -> puerto de ovs1 hacia ese servidor


ETHERTYPES_QINQ = ("802.1ad", "802.1q")


@dataclass
class ConfigQinQ:
    """QinQ en los servidores Linux. ``bridge`` es el bridge "proveedor" que
    se crea en cada servidor: ahí quedan el uplink y un puerto dot1q-tunnel
    por slice. ``ethertype`` es el TPID de la etiqueta externa: 802.1ad
    (0x88a8, el estándar) o 802.1q (0x8100) si algún equipo no entiende el
    primero. ``mtu_vm`` es el MTU de las interfaces de enlace de las VMs."""
    bridge: str = "br-qinq"
    ethertype: str = "802.1ad"
    mtu_vm: int | None = 1496

    def __post_init__(self) -> None:
        if self.ethertype not in ETHERTYPES_QINQ:
            raise ValueError(f"ethertype QinQ inválido: {self.ethertype!r} "
                             f"(use {' o '.join(ETHERTYPES_QINQ)})")


@dataclass
class ConfigBroadcast:
    """Parámetros del control de broadcast. Lo que no se configura no se toca:
    sin ``uplink_por_host`` no se podan los uplinks y sin ``switch`` no se
    toca ovs1 (cambiar trunks con un nombre de puerto equivocado cortaría la
    conectividad, así que nunca se asume un nombre)."""
    bridge_por_host: dict[str, str] = field(default_factory=dict)
    uplink_por_host: dict[str, str] = field(default_factory=dict)
    switch: ConfigSwitch | None = None
    limite_pps: int = 200                     # broadcast+multicast por puerto de VM
    rafaga: int = 100                         # paquetes de ráfaga tolerados
    vlan_sin_uso: int = 4094                  # trunks vacío en OVS = TODAS las VLANs
    cookie: int = 0xB0CA57
    storm_control: bool = True
    qinq: ConfigQinQ | None = None            # None = una sola etiqueta (VLAN por enlace)

    @classmethod
    def desde_modulo(cls, cfg: Any) -> "ConfigBroadcast":
        """Lee config_cluster (o cualquier objeto con los mismos nombres).
        Los campos nuevos son opcionales: si faltan, esa parte no se aplica."""
        workers = getattr(cfg, "WORKERS", {}) or {}
        bridges = {h: d["bridge"] for h, d in workers.items() if d.get("bridge")}
        uplinks = {h: d["uplink"] for h, d in workers.items() if d.get("uplink")}
        sw = getattr(cfg, "SWITCH_OVS", None)
        switch = None
        if sw and sw.get("destino_ssh") and sw.get("puerto_por_host"):
            switch = ConfigSwitch(sw["destino_ssh"], dict(sw["puerto_por_host"]))
        bc = getattr(cfg, "CONTROL_BROADCAST", {}) or {}
        q = getattr(cfg, "QINQ", None) or {}
        qinq = None
        if q.get("activo"):
            dq = ConfigQinQ()
            qinq = ConfigQinQ(bridge=q.get("bridge", dq.bridge),
                              ethertype=q.get("ethertype", dq.ethertype),
                              mtu_vm=q.get("mtu_vm", dq.mtu_vm))
        d = cls()
        return cls(bridge_por_host=bridges, uplink_por_host=uplinks, switch=switch, qinq=qinq,
                   limite_pps=int(bc.get("limite_pps", d.limite_pps)),
                   rafaga=int(bc.get("rafaga", d.rafaga)),
                   vlan_sin_uso=int(bc.get("vlan_sin_uso", d.vlan_sin_uso)),
                   storm_control=bool(bc.get("storm_control", d.storm_control)))


def _trunks(vlans: set[int], vlan_sin_uso: int) -> str:
    # En OVS, ``trunks`` vacío significa "todas las VLANs": para no dejar pasar
    # ninguna se deja solo una VLAN reservada que nadie usa.
    return ",".join(str(v) for v in sorted(vlans)) if vlans else str(vlan_sin_uso)


def comandos_host(host: str, plan: PlanBroadcast, cfg: ConfigBroadcast) -> list[str]:
    """Comandos (en orden) para dejar el bridge del servidor en el estado del plan."""
    bridge = cfg.bridge_por_host.get(host)
    if not bridge:
        return []
    of13 = f"sudo ovs-ofctl -O OpenFlow13"
    cmds = [f"sudo ovs-vsctl set bridge {bridge} protocols=OpenFlow10,OpenFlow13"]
    uplink = cfg.uplink_por_host.get(host)
    if uplink and cfg.qinq is not None:
        cmds.extend(_comandos_qinq(host, bridge, uplink, plan, cfg))
    elif uplink:
        cmds.append(f"sudo ovs-vsctl set port {uplink} vlan_mode=trunk "
                    f"trunks={_trunks(plan.vlans_que_salen(host), cfg.vlan_sin_uso)}")
    if cfg.storm_control:
        cookie = f"0x{cfg.cookie:x}"
        # Se borra solo lo nuestro (por cookie) y se vuelve a crear: idempotente.
        cmds.append(f"{of13} del-flows {bridge} cookie={cookie}/-1")
        cmds.append(f"{of13} del-meters {bridge}")
        # La regla de reenvío normal se reafirma con nuestra cookie para que el
        # bridge nunca quede sin forwarding si alguien borró la de fábrica.
        cmds.append(f"{of13} add-flow {bridge} \"cookie={cookie},priority=0,actions=NORMAL\"")
        for i, p in enumerate(plan.puertos_por_host.get(host, []), start=1):
            cmds.append(f"{of13} add-meter {bridge} meter={i},pktps,burst,"
                        f"band=type=drop,rate={cfg.limite_pps},burst_size={cfg.rafaga}")
            cmds.append(f"{of13} add-flow {bridge} \"cookie={cookie},priority=200,dl_src={p.mac},"
                        f"dl_dst={BROADCAST_Y_MULTICAST},actions=meter:{i},NORMAL\"")
    return cmds


def puertos_patch(vlan_servicio: int) -> tuple[str, str]:
    """Nombres del par de puertos patch de un slice: lado br-int y lado QinQ."""
    return f"qi{vlan_servicio}", f"qe{vlan_servicio}"


def _limpiar_patches(bridge: str, prefijo: str, deseados: list[str]) -> str:
    """Borra los puertos patch ``<prefijo><n>`` del bridge que ya no están en
    el plan (slices eliminados). Solo toca los nombres que crea este módulo."""
    lista = " ".join(deseados)
    return (f"for p in $(sudo ovs-vsctl list-ports {bridge}); do case \"$p\" in "
            f"{prefijo}[0-9]*) case \" {lista} \" in *\" $p \"*) ;; "
            f"*) sudo ovs-vsctl del-port {bridge} \"$p\" ;; esac ;; esac; done")


def _comandos_qinq(host: str, bridge: str, uplink: str, plan: PlanBroadcast,
                   cfg: ConfigBroadcast) -> list[str]:
    """QinQ en un servidor (ver el docstring del módulo). Idempotente: se
    puede reaplicar con el plan completo tras cada cambio."""
    q = cfg.qinq
    assert q is not None
    ext = q.bridge
    vsctl = "sudo ovs-vsctl"
    cmds = [
        # Sin esto OVS solo procesa una etiqueta y dot1q-tunnel no funciona.
        f"{vsctl} set Open_vSwitch . other_config:vlan-limit=2",
        f"{vsctl} --may-exist add-br {ext}",
        # El uplink pasa del bridge de las VMs al bridge QinQ (solo la primera vez).
        f"{vsctl} --if-exists del-port {bridge} {uplink} -- --may-exist add-port {ext} {uplink}",
        f"{vsctl} set port {uplink} vlan_mode=trunk "
        f"trunks={_trunks(plan.svlans_que_salen(host), cfg.vlan_sin_uso)}",
    ]
    lado_int: list[str] = []
    lado_ext: list[str] = []
    for sid, cvlans in sorted(plan.slices_que_salen(host).items()):
        s = plan.servicio_por_slice.get(sid)
        if s is None:
            continue
        pi, pe = puertos_patch(s)
        c = ",".join(str(v) for v in sorted(cvlans))
        cmds.append(f"{vsctl} --may-exist add-port {bridge} {pi} "
                    f"-- set interface {pi} type=patch options:peer={pe} "
                    f"-- set port {pi} vlan_mode=trunk trunks={c}")
        cmds.append(f"{vsctl} --may-exist add-port {ext} {pe} "
                    f"-- set interface {pe} type=patch options:peer={pi} "
                    f"-- set port {pe} vlan_mode=dot1q-tunnel tag={s} cvlans={c} "
                    f"other_config:qinq-ethtype={q.ethertype}")
        lado_int.append(pi)
        lado_ext.append(pe)
    cmds.append(_limpiar_patches(bridge, "qi", lado_int))
    cmds.append(_limpiar_patches(ext, "qe", lado_ext))
    return cmds


def comandos_switch(plan: PlanBroadcast, cfg: ConfigBroadcast) -> list[str]:
    """Poda de VLANs en ovs1: cada puerto hacia un servidor lleva solo las
    VLANs que ese servidor comparte con otro (las únicas que cruzan ovs1).
    Con QinQ, ovs1 solo ve S-VLANs: se poda por slice."""
    if cfg.switch is None:
        return []
    salen = plan.svlans_que_salen if cfg.qinq is not None else plan.vlans_que_salen
    return [f"sudo ovs-vsctl set port {puerto} vlan_mode=trunk "
            f"trunks={_trunks(salen(host), cfg.vlan_sin_uso)}"
            for host, puerto in sorted(cfg.switch.puerto_por_host.items())]
