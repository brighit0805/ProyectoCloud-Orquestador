"""
config_cluster.py
-----------------
Datos reales del clúster del Grupo 2, en UN solo lugar. Si algo cambia en el
laboratorio, se cambia aquí y no en los scripts.

Corregido 17-nov: el mapeo original apuntaba a worker1/2/3, que en realidad
son nodos del clúster OpenStack (confirmado con el doc oficial del curso),
no los nodos Linux/KVM. Los nodos Linux reales son server1/2/3 (+ server4
como headnode de scripts). IPs verificadas por VNC (ip -4 -br a) el 17-nov.
"""

USUARIO_SSH = "ubuntu"
POOL_LIBVIRT = "default"

# server -> (IP de gestión, bridge)
# NOTA sobre 'bridge': no hay un bridge OVS real configurado todavía en estos
# hosts (se verificó con 'ovs-vsctl show' -> vacío). Solo existe 'virbr0', el
# bridge NAT que libvirt crea por defecto al instalarse -- ese es el que usa
# MODO_ACCESO="nat_libvirt" (ver config_acceso() abajo) para la salida a
# Internet de las VMs, y no depende de este campo 'bridge'. Este campo es
# para las VLANs de los ENLACES entre VMs de un mismo slice (ver VLAN_INICIO/
# VLAN_FIN) -- sin un bridge OVS real todavía, crear esos enlaces fallará;
# queda documentado como limitación pendiente, no simulado como si ya funcionara.
WORKERS = {
    "server1": {"ip": "192.168.201.1", "bridge": "br-int", "uplink": "ens4"},   # uplink verificado con ovs-vsctl list-ports (24-nov)
    "server2": {"ip": "192.168.201.2", "bridge": "br-int", "uplink": "ens4"},   # uplink verificado con ovs-vsctl list-ports (24-nov)
    "server3": {"ip": "192.168.201.3", "bridge": "br-int", "uplink": "ens4"},   # uplink verificado con ovs-vsctl list-ports (24-nov)
    # server4 (192.168.201.4) existe pero NO es un worker KVM: sin br-int/libvirt/OVS visibles y el
    # headnode no entra por SSH (24-nov). Descomentar solo si Germán confirma que se usa y lo habilitan.
    # "server4": {"ip": "192.168.201.4", "bridge": "br-int"},
}

# Switches OVS dedicados del clúster Linux (Fase 2, nodos separados de los KVM workers).
# OVS1 pertenece al clúster Linux; OVS2 pertenece al clúster OpenStack.
# Germán (23-nov): "Deben aplicar reglas en servidores y switches de datos (OVS1 y 2)".
# Para el driver Linux solo aplica OVS1.
# IPs verificadas por VNC (ip -4 -br a) el 23-nov-2026.
OVS_SWITCHES = {
    "ovs1": {"ip": "192.168.201.5", "bridge": "br-int"},   # switch Linux cluster
    # ovs2: 192.168.202.5 — cluster OpenStack, no aplica para driver Linux
}

# Zona de disponibilidad del clúster Linux: agrupa todos los hosts de WORKERS
# (hoy server1..server3). Para el examen (Ex2) basta con quitar "server1" de WORKERS
# o usar otro nombre de zona aquí.
ZONA_LINUX = "az-lab"

# Imágenes base que deben existir en el pool de CADA server
IMAGENES_BASE = ["cirros-0.6.2.qcow2", "ubuntu-22.04.qcow2"]
# Carpeta EN EL HEADNODE (quien orquesta) con las imágenes base: el orquestador las copia a cada server al desplegar
REPO_IMAGENES = "/home/ubuntu/imagenes-ex1"      # CONFIRMAR: ahí deben estar los dos .qcow2
ARCHIVO_ESTADO_IMAGENES = "estado_imagenes.json"   # copias que creó el orquestador

# Headnode / gateway
# NOTA: estos valores (IP externa, interfaz interna, red interna) NO se usan
# cuando MODO_ACCESO = "nat_libvirt" (el modo activo, ver abajo) -- están
# desactualizados (apuntaban a 10.0.10.0/24, que era la red vieja de
# worker1/2/3). Si algún día se cambia a MODO_ACCESO="headnode", hay que
# verificarlos de nuevo contra el clúster real antes de usarlos.
HEADNODE_IP_EXTERNA = "10.20.11.142"   # CONFIRMAR: no usado en modo nat_libvirt
HEADNODE_IFACE_EXTERNA = "ens3"
HEADNODE_GW_EXTERNO = "10.20.11.1"     # CONFIRMAR: no usado en modo nat_libvirt
HEADNODE_IP_INTERNA = "10.0.10.100"    # CONFIRMAR: no usado en modo nat_libvirt
HEADNODE_IFACE_INTERNA = "ens4"
RED_INTERNA = "10.0.10.0/24"           # CONFIRMAR: no usado en modo nat_libvirt

# Red de acceso de VM1 y VM3 (solo aplica si MODO_ACCESO = "headnode")
VLAN_ACCESO = None
IPS_ACCESO = {"vm1": "10.0.10.31", "vm3": "10.0.10.33"}
PREFIJO_ACCESO = 24

# VLAN de los enlaces entre VMs (una por enlace) -- requiere bridge OVS real, ver nota arriba
VLAN_INICIO, VLAN_FIN = 3000, 3099

# Control de broadcast (R5, slice_orchestrator/control_broadcast.py).
# Lo que no se configure aquí NO se toca en el clúster:
#  - Poda de VLANs en el uplink de cada server: agregar "uplink": "<interfaz del bridge
#    hacia ovs1>" en cada entrada de WORKERS (CONFIRMAR nombres reales con ovs-vsctl show).
#  - Poda de VLANs en ovs1: llenar SWITCH_OVS, p. ej.
#      {"destino_ssh": "ubuntu@192.168.201.5",   # ovs1, ver OVS_SWITCHES arriba
#       "puerto_por_host": {"server1": "<puerto>", "server2": "<puerto>", "server3": "<puerto>"}}
#  - Storm control (OpenFlow 1.3 + meters) en el bridge de cada server: se aplica siempre
#    que el bridge exista; si falla, solo queda un warning en el log.
# Los comandos usan 'sudo ovs-vsctl' y 'sudo ovs-ofctl': requieren NOPASSWD para esos
# binarios, igual que iptables para el SSH entrante.
#
# Mapeo server -> puerto de ovs1, confirmado el 26-nov cruzando el diagrama de
# topología oficial (VNRT, panel "doble click" de cada nodo) con las MACs
# reales del clúster:
#   - Cada server tiene DOS interfaces: 'ens4' (uplink de gestión, va a MPLink1,
#     es la que ya podamos en WORKERS/uplink de arriba) y 'ens3' (va directo a
#     ovs1, HOY sin agregar a ningún bridge del propio server -- no lleva
#     tráfico real de las VMs todavía).
#   - server1.ens3 (MAC fa:16:3e:f3:3d:13) <-> ovs1.ens4 (Enlace 2 del VNRT)
#   - server2.ens3 (MAC fa:16:3e:75:a3:97) <-> ovs1.ens5 (Enlace 3 del VNRT)
#   - server3.ens3 (MAC fa:16:3e:8f:37:7e) <-> ovs1.ens6 (Enlace 4 del VNRT)
# ovs1 tiene su propia IP de gestión en un puerto interno separado (tipo
# "internal", no ens4/ens5/ens6), así que podar VLANs en estos tres puertos no
# le corta el acceso SSH a ovs1 mismo (confirmado en vivo).
# OJO: como ens3 no está en ningún bridge del lado server todavía, esta regla
# no tiene efecto visible en el tráfico actual de las VMs (que hoy viaja por
# MPLink1 vía ens4/br-int) -- cumple el pedido de Germán de "aplicar reglas en
# OVS1", pero si el diseño final requiere que las VLANs de los enlaces crucen
# por ovs1 de verdad, falta un paso aparte: agregar ens3 al bridge de cada
# server (fuera del alcance de este cambio).
SWITCH_OVS = {
    "destino_ssh": "ubuntu@192.168.201.5",
    "puerto_por_host": {"server1": "ens4", "server2": "ens5", "server3": "ens6"},
}
# Q-in-Q / 802.1ad (R5, pedido por Germán 25-nov-2026 tras su objeción al límite
# de 4094 VLAN de 802.1Q): envuelve TODO el tráfico que cruza el uplink de cada
# server (y el puerto equivalente en ovs1, si SWITCH_OVS está configurado) en
# una segunda VLAN exterior fija ("S-VLAN", aquí 100) -- las VLAN de cada
# enlace (C-VLAN, VLAN_INICIO/VLAN_FIN arriba) siguen siendo una por enlace,
# sin cambios, y las VMs no ven ni usan esta segunda etiqueta para nada (ver
# driver_linux._interfaz_xml -- su puerto OVS sigue siendo access, intacto).
# "qinq": False (como hoy) = comportamiento IDÉNTICO al de siempre, un solo
# tag por enlace, sin tocar el modo del puerto del uplink.
CONTROL_BROADCAST = {"limite_pps": 200, "rafaga": 100, "vlan_sin_uso": 4094, "storm_control": True,
                     "qinq": False, "qinq_svlan": 100, "qinq_ethertype": "0x8100"}


def uri(worker: str) -> str:
    return f"qemu+ssh://{USUARIO_SSH}@{WORKERS[worker]['ip']}/system"


# Modo de acceso a Internet de VM1/VM3:
#   "nat_libvirt": NIC en la red NAT 'default' de libvirt del server (DHCP) -- ya confirmada
#                  real: existe 'virbr0' en server1/2/3. No requiere root en el headnode.
#   "headnode":    NIC en la red interna con IP fija y NAT en el headnode (requiere root,
#                  y los valores HEADNODE_* de arriba, hoy sin verificar).
MODO_ACCESO = "nat_libvirt"
RED_NAT_LIBVIRT = "default"


def config_acceso() -> dict:
    if MODO_ACCESO == "nat_libvirt":
        return {"red_libvirt": RED_NAT_LIBVIRT}
    return {"ips": IPS_ACCESO, "prefijo": PREFIJO_ACCESO,
            "gateway": HEADNODE_IP_INTERNA, "vlan": VLAN_ACCESO}


NOMBRE_SLICE = "demo-ex1"       # prefijo de las VMs: demo-ex1-vm1 ... demo-ex1-vm6
CARPETA_IMAGENES = "/var/lib/libvirt/images"
