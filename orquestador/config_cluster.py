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
    "server1": {"ip": "192.168.201.1", "bridge": "br-int"},
    "server2": {"ip": "192.168.201.2", "bridge": "br-int"},
    "server3": {"ip": "192.168.201.3", "bridge": "br-int"},
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

# Imágenes base que deben existir en el pool de CADA server
IMAGENES_BASE = ["cirros-0.6.2.qcow2", "ubuntu-22.04.qcow2"]
# Carpeta en server4 con las imágenes base: el orquestador las copia a cada server al desplegar
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
