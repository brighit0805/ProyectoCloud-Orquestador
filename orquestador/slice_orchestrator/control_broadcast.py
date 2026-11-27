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

    def hosts_de_vlan(self, vlan: int) -> set[str]:
        return {h for h, vs in self.vlans_por_host.items() if vlan in vs}

    def vlans_que_salen(self, host: str) -> set[int]:
        """VLANs del host que también tienen VMs en otro servidor: las únicas
        que deben cruzar su uplink."""
        return {v for v in self.vlans_por_host.get(host, set()) if len(self.hosts_de_vlan(v)) > 1}

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
                "puertos_con_storm_control": len(self.puertos_por_host.get(host, [])),
            })
        return filas


def calcular_plan(slices: Iterable[Slice]) -> PlanBroadcast:
    """Plan a partir de los slices ACTIVOS con sus nodos ya ubicados."""
    plan = PlanBroadcast()
    for slc in slices:
        if slc.estado != EstadoSlice.ACTIVO:
            continue
        for nodo in slc.nodos:
            if nodo.host_id is None:
                continue
            for itf in interfaces_de(slc, nodo):
                plan.vlans_por_host.setdefault(nodo.host_id, set()).add(itf.vlan)
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
    # Q-in-Q / 802.1ad (R5, pedido por Germán 25-nov-2026 tras su objeción al
    # límite de 4094 VLAN de 802.1Q). Ver nota larga en comandos_host() sobre
    # qué resuelve y qué no esta primera versión.
    qinq: bool = False
    qinq_svlan: int | None = None              # UNA sola S-VLAN, fija, para todo el clúster
    qinq_ethertype: str = "0x8100"             # 0x88a8 (802.1ad "real") si el switch de arriba lo soporta

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
        d = cls()
        return cls(bridge_por_host=bridges, uplink_por_host=uplinks, switch=switch,
                   limite_pps=int(bc.get("limite_pps", d.limite_pps)),
                   rafaga=int(bc.get("rafaga", d.rafaga)),
                   vlan_sin_uso=int(bc.get("vlan_sin_uso", d.vlan_sin_uso)),
                   storm_control=bool(bc.get("storm_control", d.storm_control)),
                   qinq=bool(bc.get("qinq", d.qinq)),
                   qinq_svlan=(int(bc["qinq_svlan"]) if bc.get("qinq_svlan") is not None else None),
                   qinq_ethertype=str(bc.get("qinq_ethertype", d.qinq_ethertype)))


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
    if uplink:
        trunks = _trunks(plan.vlans_que_salen(host), cfg.vlan_sin_uso)
        if cfg.qinq and cfg.qinq_svlan is not None:
            # Q-in-Q (802.1ad): el puerto deja de ser un trunk 802.1Q plano y
            # pasa a "dot1q-tunnel" -- OVS añade SIEMPRE una segunda etiqueta
            # exterior (la S-VLAN fija del clúster) a todo lo que sale por
            # este puerto, y la quita a todo lo que entra, ANTES de que el
            # resto del pipeline vea el paquete. La poda de VLANs de siempre
            # (`trunks`, la lista de C-VLAN que de verdad cruzan a otro
            # servidor) se conserva tal cual -- sigue restringiendo qué
            # C-VLAN interiores puede llevar el túnel, ahora dentro de la
            # S-VLAN exterior en vez de a secas.
            #
            # Qué multiplica y qué no (ver docstring de ConfigBroadcast):
            # dentro de ESTE clúster la C-VLAN sigue siendo única por enlace
            # (el VLANAllocator no cambia) -- lo que gana con esto es que el
            # backbone compartido (ovs1 y lo que haya detrás) puede traer
            # tráfico de OTRO grupo/clúster con sus propias C-VLAN sin que
            # choquen con las nuestras, porque cada uno queda envuelto en su
            # propia S-VLAN. Dar a cada SLICE (no a todo el clúster) su propia
            # S-VLAN sobre este mismo puerto compartido exigiría reglas
            # OpenFlow por C-VLAN que decidieran cuál S-VLAN empujar -- se
            # dejó fuera a propósito: no hay forma de validarlas sin acceso
            # real al clúster antes del freeze del 5-dic, y una regla mal
            # escrita ahí corta la conectividad del uplink completo.
            cmds.append(f"sudo ovs-vsctl set port {uplink} vlan_mode=dot1q-tunnel "
                        f"tag={cfg.qinq_svlan} trunks={trunks} "
                        f"other_config:qinq-ethtype={cfg.qinq_ethertype}")
        else:
            # IMPORTANTE (encontrado en vivo el 26-nov probando contra el
            # clúster real): NUNCA usar vlan_mode=trunk a secas aquí. Este
            # mismo puerto (uplink) suele llevar también el tráfico de
            # gestión (SSH) hacia el propio servidor, que viaja SIN etiqueta
            # VLAN. "trunk" a secas descarta todo lo no etiquetado, así que
            # el comando se corta la conexión que lo está aplicando (si se
            # ejecuta por SSH, la sesión se queda "colgada" -- no es que el
            # comando falle, es que ya no hay forma de ver su respuesta).
            # "native-untagged" dice: deja pasar lo no etiquetado como
            # siempre (gestión) Y ADEMÁS lo etiquetado de `trunks` (los
            # enlaces entre VMs) -- así no nos cortamos el acceso remoto.
            cmds.append(f"sudo ovs-vsctl set port {uplink} vlan_mode=native-untagged "
                        f"tag=0 trunks={trunks}")
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


def comandos_revertir_host(host: str, cfg: ConfigBroadcast) -> list[str]:
    """Deshace SOLO el cambio riesgoso (el modo del puerto uplink), dejándolo
    sin restricción de VLAN -- es lo que corre la red de seguridad (ver
    driver_linux._ejecutar_con_auto_reversion) si el comando real no llega a
    confirmarse a tiempo. No toca storm control (flows/meters): esos no
    pueden cortar la conexión de gestión, así que no necesitan red de
    seguridad."""
    uplink = cfg.uplink_por_host.get(host)
    if not uplink:
        return []
    return [f"sudo ovs-vsctl clear port {uplink} vlan_mode",
            f"sudo ovs-vsctl clear port {uplink} tag",
            f"sudo ovs-vsctl clear port {uplink} trunks"]


def comandos_revertir_switch(cfg: ConfigBroadcast) -> dict[str, list[str]]:
    """Igual que ``comandos_revertir_host`` pero para cada puerto de ovs1,
    uno por host (la reversión se corre en la sesión SSH hacia ovs1, por
    host, igual que ``comandos_switch`` arma el cambio por host)."""
    if cfg.switch is None:
        return {}
    return {host: [f"sudo ovs-vsctl clear port {puerto} vlan_mode",
                   f"sudo ovs-vsctl clear port {puerto} tag",
                   f"sudo ovs-vsctl clear port {puerto} trunks"]
            for host, puerto in cfg.switch.puerto_por_host.items()}


def comandos_switch(plan: PlanBroadcast, cfg: ConfigBroadcast) -> list[str]:
    """Poda de VLANs en ovs1: cada puerto hacia un servidor lleva solo las
    VLANs que ese servidor comparte con otro (las únicas que cruzan ovs1).

    Con Q-in-Q habilitado, el puerto de ovs1 hacia cada servidor debe hablar
    el mismo modo "dot1q-tunnel" que su contraparte en comandos_host(), o el
    switch no entendería la doble etiqueta que llega -- ambos lados de un
    mismo enlace físico deben configurarse igual."""
    if cfg.switch is None:
        return []
    cmds = []
    for host, puerto in sorted(cfg.switch.puerto_por_host.items()):
        trunks = _trunks(plan.vlans_que_salen(host), cfg.vlan_sin_uso)
        if cfg.qinq and cfg.qinq_svlan is not None:
            cmds.append(f"sudo ovs-vsctl set port {puerto} vlan_mode=dot1q-tunnel "
                        f"tag={cfg.qinq_svlan} trunks={trunks} "
                        f"other_config:qinq-ethtype={cfg.qinq_ethertype}")
        else:
            # Mismo motivo que en comandos_host(): "trunk" a secas cortaría
            # el acceso de gestión a ovs1 si viaja sin etiqueta por este
            # mismo puerto.
            cmds.append(f"sudo ovs-vsctl set port {puerto} vlan_mode=native-untagged "
                        f"tag=0 trunks={trunks}")
    return cmds
