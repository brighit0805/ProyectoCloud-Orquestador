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
    # uplink: verificado con ovs-vsctl list-ports (24-nov), va a MPLink1 (gestión).
    # uplink_datos: ens3, directo a ovs1 (26-nov); se usa solo con UPLINK_DATOS_SEPARADO = True.
    "server1": {"ip": "192.168.201.1", "bridge": "br-int", "uplink": "ens4", "uplink_datos": "ens3"},
    "server2": {"ip": "192.168.201.2", "bridge": "br-int", "uplink": "ens4", "uplink_datos": "ens3"},
    "server3": {"ip": "192.168.201.3", "bridge": "br-int", "uplink": "ens4", "uplink_datos": "ens3"},
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
CONTROL_BROADCAST = {"limite_pps": 200, "rafaga": 100, "vlan_sin_uso": 4094, "storm_control": True}

# Aislamiento entre slices con QinQ (802.1ad), indicado por Germán (25-nov): etiqueta externa
# (S-VLAN, rango 2000-2999) = una por slice; etiqueta interna (C-VLAN) = la VLAN de cada enlace.
# Con "activo": True, en cada server se crea el bridge 'br-qinq', el uplink (ens4) pasa de
# br-int a br-qinq y cada slice sale por un puerto dot1q-tunnel con su S-VLAN. Lo no etiquetado
# (gestión) sigue llegando a br-int por el puente qnat-int/qnat-ext, y si el comando no se
# confirma en 30 s el vigía de driver_linux devuelve ens4 a br-int solo.
# Viene APAGADO: activarlo cuando se pruebe en el clúster (ver PR de QinQ).
#  - ethertype: "802.1ad" (TPID 0x88a8, estándar) o "802.1q" (0x8100) si ovs1 no reenvía 802.1ad.
#  - mtu_vm: MTU de las interfaces de enlace de las VMs (dos etiquetas = 8 bytes más por trama).
# Para volver a una sola etiqueta: "activo": False y, en cada server, devolver el uplink a br-int:
#   sudo ovs-vsctl --if-exists del-port br-qinq ens4 -- --may-exist add-port br-int ens4
# Uplink de datos separado (R5, 27-nov): con True, las VLANs de las VMs salen por
# "uplink_datos" (ens3 -> ovs1) y "uplink" (ens4 -> MPLink1) queda solo con la gestión.
# Así lo pide el enunciado (tráfico de VMs por OVS) y un error en las VLANs no corta el SSH.
# Activar después de que entrega-qinq/prueba_qinq_ens3.sh confirme el enlace ens3 <-> ovs1.
# Con QinQ activo además, ens3 va a br-qinq y ens4 ya no se mueve de br-int.
UPLINK_DATOS_SEPARADO = False

QINQ = {"activo": False, "bridge": "br-qinq", "ethertype": "802.1ad", "mtu_vm": 1496}


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
