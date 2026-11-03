#!/usr/bin/env python3
"""Revisa, SIN modificar nada, que el clúster esté listo para desplegar la EX1.

Se ejecuta desde server4:  python3 verificar_cluster.py
Comprueba por worker: SSH, libvirt, pool, imágenes base, restos de despliegues
anteriores (VMs/discos), bridge OVS y alcance al headnode. Además comprueba si
las IPs candidatas de VM1 y VM3 parecen libres.
"""
import subprocess

import config_cluster as cfg
from cluster_utils import ssh

OK, MAL, AVISO = "[ OK ]", "[FALLA]", "[AVISO]"


def linea(estado, texto):
    print(f"  {estado} {texto}")


def main():
    problemas = 0
    for w, datos in cfg.WORKERS.items():
        print(f"\n== {w} ({datos['ip']}) ==")
        rc, out = ssh(w, "hostname")
        if rc != 0:
            linea(MAL, f"SSH sin clave falla: {out}"); problemas += 1; continue
        linea(OK, f"SSH sin contraseña ({out})")

        rc, out = ssh(w, f"virsh -c qemu:///system pool-info {cfg.POOL_LIBVIRT} | grep -i state")
        linea(OK if "running" in out.lower() else MAL, f"pool '{cfg.POOL_LIBVIRT}': {out or 'no responde'}")

        rc, out = ssh(w, f"virsh -c qemu:///system vol-list {cfg.POOL_LIBVIRT}")
        for img in cfg.IMAGENES_BASE:
            linea(OK if img not in out else AVISO,
                  f"imagen base {img}: " + ("no está (el orquestador la copiará)" if img not in out
                                           else "YA está en el worker (el paso 2 pide workers sin imágenes)"))

        previos = [l.split()[0] for l in out.splitlines()
                   if l.split() and l.split()[0] not in cfg.IMAGENES_BASE
                   and (l.split()[0].startswith(cfg.NOMBRE_SLICE) or l.split()[0].startswith("seed-"))]
        if previos:
            linea(AVISO, f"quedan discos de despliegues anteriores: {', '.join(previos)}")
        rc, doms = ssh(w, "virsh -c qemu:///system list --all --name")
        restos = [d for d in doms.split() if d.startswith(cfg.NOMBRE_SLICE)]
        if restos:
            linea(AVISO, f"quedan VMs de despliegues anteriores: {', '.join(restos)}")

        rc, out = ssh(w, f"ip -br link show {datos['bridge']}")
        linea(OK if rc == 0 else MAL, f"bridge {datos['bridge']}")
        problemas += 0 if rc == 0 else 1

        rc, out = ssh(w, f"ping -c1 -W2 {cfg.HEADNODE_IP_INTERNA}")
        linea(OK if rc == 0 else AVISO,
              f"alcanza al headnode {cfg.HEADNODE_IP_INTERNA} sin etiqueta VLAN"
              + ("" if rc == 0 else "  <- la interfaz de acceso de VM1/VM3 NO llegaría por la red sin etiqueta"))

    print("\n== Repositorio de imágenes en server4 ==")
    import os
    for img in cfg.IMAGENES_BASE:
        ok = os.path.isfile(os.path.join(cfg.REPO_IMAGENES, img))
        linea(OK if ok else MAL, f"{cfg.REPO_IMAGENES}/{img}")
        problemas += 0 if ok else 1

    print("\n== IPs candidatas para VM1 y VM3 (acceso) ==")
    for vm, ip in cfg.IPS_ACCESO.items():
        r = subprocess.run(["ping", "-c1", "-W1", ip], capture_output=True)
        libre = r.returncode != 0
        linea(OK if libre else MAL, f"{vm}: {ip} {'no responde (parece libre)' if libre else 'YA RESPONDE: está en uso'}")
        problemas += 0 if libre else 1
    print("\nRESULTADO:", "listo para desplegar" if problemas == 0 else f"{problemas} problema(s) por resolver")
    return problemas


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
