"""
config_cluster.py
-----------------
Datos reales del clúster del Grupo 2, en UN solo lugar. Si algo cambia en el
laboratorio, se cambia aquí y no en los scripts.

Los valores marcados con  # CONFIRMAR  son supuestos razonables que alguien
del grupo debe verificar en el clúster antes del demo.
"""

USUARIO_SSH = "ubuntu"
POOL_LIBVIRT = "default"

# worker -> (IP de gestión, bridge OVS)
WORKERS = {
    "worker1": {"ip": "10.0.10.1", "bridge": "br-int"},
    "worker2": {"ip": "10.0.10.2", "bridge": "ovs1"},
    "worker3": {"ip": "10.0.10.3", "bridge": "br-int"},
}

# Imágenes base que deben existir en el pool de CADA worker
IMAGENES_BASE = ["cirros-0.6.2.qcow2", "ubuntu-22.04.qcow2"]
# Carpeta en server4 con las imágenes base: el orquestador las copia a cada worker al desplegar
REPO_IMAGENES = "/home/ubuntu/imagenes-ex1"      # CONFIRMAR: ahí deben estar los dos .qcow2
ARCHIVO_ESTADO_IMAGENES = "estado_imagenes.json"   # copias que creó el orquestador

# Headnode / gateway
HEADNODE_IP_EXTERNA = "10.20.11.142"
HEADNODE_IFACE_EXTERNA = "ens3"       # salida a Internet
HEADNODE_GW_EXTERNO = "10.20.11.1"
HEADNODE_IP_INTERNA = "10.0.10.100"   # gateway que verán VM1 y VM3
HEADNODE_IFACE_INTERNA = "ens4"
RED_INTERNA = "10.0.10.0/24"

# Red de acceso de VM1 y VM3 (salida a Internet y SSH entrante)
VLAN_ACCESO = None                    # CONFIRMAR: None = sin etiqueta (la misma red que el headnode)
IPS_ACCESO = {"vm1": "10.0.10.31", "vm3": "10.0.10.33"}   # CONFIRMAR que estén libres
PREFIJO_ACCESO = 24

# VLAN de los enlaces entre VMs (una por enlace)
VLAN_INICIO, VLAN_FIN = 3000, 3099


def uri(worker: str) -> str:
    return f"qemu+ssh://{USUARIO_SSH}@{WORKERS[worker]['ip']}/system"


# Interfaz de acceso a Internet de VM1 y VM3 (ver ejecutar_ex1_real.py --acceso)
# Modo de acceso a Internet de VM1/VM3:
#   "nat_libvirt": 3.ª NIC en la red NAT 'default' de libvirt del worker (DHCP). No requiere root en el headnode.
#   "headnode":    NIC en la red interna con IP fija y NAT en el headnode (headnode_nat.sh, requiere root).
MODO_ACCESO = "nat_libvirt"
RED_NAT_LIBVIRT = "default"


def config_acceso() -> dict:
    if MODO_ACCESO == "nat_libvirt":
        return {"red_libvirt": RED_NAT_LIBVIRT}
    return {"ips": IPS_ACCESO, "prefijo": PREFIJO_ACCESO,
            "gateway": HEADNODE_IP_INTERNA, "vlan": VLAN_ACCESO}


NOMBRE_SLICE = "demo-ex1"       # prefijo de las VMs: demo-ex1-vm1 ... demo-ex1-vm6
CARPETA_IMAGENES = "/var/lib/libvirt/images"
