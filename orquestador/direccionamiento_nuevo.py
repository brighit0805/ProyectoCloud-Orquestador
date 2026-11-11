"""
direccionamiento.py
--------------------
Qué IP lleva cada interfaz de cada VM, y cómo se le entrega a la VM al arrancar.

Esquema (el mismo que ya se validó a mano en el laboratorio):
  * cada enlace del slice es una subred /30 propia: 10.30.<n>.0/30, donde <n>
    es la posición del enlace en el slice (0, 1, 2, ...);
  * el nodo de origen del enlace toma .1 y el de destino .2.
  Ejemplo EX1: VLAN 3000 (VM1-VM2) = 10.30.0.0/30  -> VM1 10.30.0.1, VM2 10.30.0.2.

La IP no se escribe a mano dentro de la VM: se genera aquí y viaja en una
«semilla» cloud-init (un CD-ROM virtual que la VM lee al primer arranque).
Cada interfaz se identifica por su MAC, que es determinista, porque el nombre
(enp0s3, eth0...) cambia según el sistema operativo.

Dos formatos, porque las dos imágenes entienden cosas distintas:
  * Ubuntu  -> cloud-config + network-config v2 (la red queda persistente).
  * Cirros  -> script de shell + meta-data en JSON (no entiende network-config
              y su NoCloud solo parsea meta-data como JSON).
"""

from __future__ import annotations

import ipaddress
import json
import zlib
from dataclasses import dataclass

from .models import Nodo, Slice

# Plantilla de subred por enlace; {n} es el índice del enlace.
PLANTILLA_SUBRED = "10.30.{n}.0/30"

# Usuario y contraseña de las imágenes del laboratorio
CREDENCIALES = {
    "ubuntu": ("ubuntu", "ubuntu"),
    "cirros": ("cirros", "gocubsgo"),
}


def mac_enlace(slice_id: str, nodo_id: str, enlace_id: str) -> str:
    """MAC estable de la interfaz de un nodo en un enlace (rango 52:54:00)."""
    h = zlib.crc32(f"{slice_id}|{nodo_id}|{enlace_id}".encode())
    return "52:54:00:%02x:%02x:%02x" % ((h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF)


def mac_acceso(slice_id: str, nodo_id: str) -> str:
    """MAC de la interfaz de acceso (la que sale hacia el headnode)."""
    h = zlib.crc32(f"{slice_id}|{nodo_id}|acceso".encode())
    return "52:54:00:%02x:%02x:%02x" % ((h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF)


@dataclass(frozen=True)
class InterfazEnlace:
    enlace_id: str
    red: str
    vlan: int
    mac: str
    cidr: str          # p. ej. 10.30.0.1/30
    vecino: str        # id del nodo del otro extremo


@dataclass(frozen=True)
class InterfazAcceso:
    mac: str
    ip: str | None = None
    prefijo: int = 24
    gateway: str | None = None
    vlan: int | None = None     # None = sin etiqueta
    red_libvirt: str | None = None   # si se define: NIC NAT de libvirt (p. ej. "default") con DHCP


def subred_del_enlace(indice: int) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(PLANTILLA_SUBRED.format(n=indice))


def interfaces_de(slc: Slice, nodo: Nodo) -> list[InterfazEnlace]:
    """Interfaces de enlace del nodo, en el MISMO orden en que el orquestador
    se las pasa al driver (el de ``slc.enlaces``)."""
    salida = []
    for i, e in enumerate(slc.enlaces):
        if nodo.id not in (e.nodo_origen, e.nodo_destino):
            continue
        if e.nombre_red is None or e.vlan_id is None:
            raise RuntimeError(f"El enlace {e.id} no tiene red/VLAN asignada")
        sub = subred_del_enlace(i)
        es_origen = nodo.id == e.nodo_origen
        ip = sub[1] if es_origen else sub[2]
        salida.append(InterfazEnlace(
            enlace_id=e.id, red=e.nombre_red, vlan=e.vlan_id,
            mac=mac_enlace(slc.id, nodo.id, e.id), cidr=f"{ip}/30",
            vecino=e.nodo_destino if es_origen else e.nodo_origen,
        ))
    return salida


def plan_completo(slc: Slice) -> dict[str, list[InterfazEnlace]]:
    """nodo_id -> sus interfaces con IP. Sirve para documentar y verificar."""
    return {n.id: interfaces_de(slc, n) for n in slc.nodos}


# ---------------------------------------------------------------- semilla ---

def meta_data(nodo: Nodo) -> str:
    return f"instance-id: {nodo.nombre}\nlocal-hostname: {nodo.nombre}\n"


def meta_data_cirros(nodo: Nodo) -> str:
    """Cirros lee el meta-data NoCloud con un parser JSON (json2fstree). Con YAML
    falla ('failed to parse json'), descarta el datasource y nunca ejecuta el
    user-data. Por eso para Cirros el meta-data va en JSON."""
    return json.dumps({"instance-id": nodo.nombre,
                       "local-hostname": nodo.nombre}) + "\n"


def network_config_ubuntu(interfaces: list[InterfazEnlace],
                          acceso: InterfazAcceso | None = None) -> str:
    """network-config v2: cada interfaz se identifica por MAC."""
    l = ["version: 2", "ethernets:"]
    n = 0
    for itf in interfaces:
        l += [f"  e{n}:",
              "    match:", f"      macaddress: \"{itf.mac}\"",
              f"    set-name: e{n}",
              "    dhcp4: false",
              f"    addresses: [{itf.cidr}]"]
        n += 1
    if acceso:
        l += [f"  e{n}:",
              "    match:", f"      macaddress: \"{acceso.mac}\"",
              f"    set-name: e{n}"]
        if acceso.red_libvirt:       # NAT de libvirt: DHCP entrega IP, gateway y DNS
            l += ["    dhcp4: true"]
        else:
            l += ["    dhcp4: false",
                  f"    addresses: [{acceso.ip}/{acceso.prefijo}]",
                  f"    gateway4: {acceso.gateway}",
                  "    nameservers:", "      addresses: [8.8.8.8]"]
    return "\n".join(l) + "\n"


def user_data_ubuntu(nodo: Nodo, clave_ssh_publica: str | None = None) -> str:
    usuario, clave = CREDENCIALES["ubuntu"]
    l = ["#cloud-config",
         f"hostname: {nodo.nombre}",
         "manage_etc_hosts: true",
         "users:",
         f"  - name: {usuario}",
         "    groups: sudo",
         "    shell: /bin/bash",
         "    sudo: ALL=(ALL) NOPASSWD:ALL",
         "    lock_passwd: false",
         f"    plain_text_passwd: {clave}"]
    if clave_ssh_publica:
        l += ["    ssh_authorized_keys:", f"      - {clave_ssh_publica}"]
    l += ["ssh_pwauth: true", "chpasswd:", "  expire: false"]
    return "\n".join(l) + "\n"


def user_data_cirros(nodo: Nodo, interfaces: list[InterfazEnlace],
                     acceso: InterfazAcceso | None = None) -> str:
    """Script de primer arranque para Cirros: ubica cada interfaz por MAC."""
    l = ["#!/bin/sh",
         f"# Direccionamiento de {nodo.nombre} generado por el orquestador",
         "nombre_if() { for d in /sys/class/net/*; do",
         '  [ "$(cat "$d/address" 2>/dev/null)" = "$1" ] && basename "$d" && return; done; }',
         'poner_ip() { i=$(nombre_if "$1"); [ -n "$i" ] || return 1;',
         '  ip addr flush dev "$i"; ip addr add "$2" dev "$i"; ip link set "$i" up; }',
         ""]
    for itf in interfaces:
        l.append(f"poner_ip {itf.mac} {itf.cidr}   # enlace {itf.enlace_id}")
    if acceso and acceso.red_libvirt:
        l.append('i=$(nombre_if ' + acceso.mac + '); [ -n "$i" ] && udhcpc -i "$i" -n -q   # acceso por DHCP')
    elif acceso:
        l.append(f"poner_ip {acceso.mac} {acceso.ip}/{acceso.prefijo}   # acceso")
        l.append(f"ip route replace default via {acceso.gateway}")
        l.append("echo 'nameserver 8.8.8.8' > /etc/resolv.conf")
    return "\n".join(l) + "\n"


def archivos_semilla(nodo: Nodo, interfaces: list[InterfazEnlace],
                     acceso: InterfazAcceso | None = None,
                     clave_ssh_publica: str | None = None) -> dict[str, str]:
    """Contenido de los archivos de la semilla cloud-init de un nodo:
    {nombre_en_la_iso: texto}. Es lo que se empaqueta en el CD-ROM."""
    if nodo.flavor == "cirros":
        return {"user-data": user_data_cirros(nodo, interfaces, acceso),
                "meta-data": meta_data_cirros(nodo)}
    return {"user-data": user_data_ubuntu(nodo, clave_ssh_publica),
            "meta-data": meta_data(nodo),
            "network-config": network_config_ubuntu(interfaces, acceso)}
