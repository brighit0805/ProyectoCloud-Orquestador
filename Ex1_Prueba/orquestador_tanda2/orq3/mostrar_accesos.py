#!/usr/bin/env python3
import re
import subprocess

import config_cluster as cfg


VMS_ACCESO = {
    "demo-ex1-vm1": "worker1",
    "demo-ex1-vm3": "worker3",
}


def ejecutar(cmd):
    r = subprocess.run(cmd, text=True, capture_output=True)
    return r.returncode, r.stdout, r.stderr


def obtener_ip(vm, worker):
    uri = cfg.uri(worker)

    rc, out, err = ejecutar([
        "virsh", "-c", uri,
        "domifaddr", vm,
        "--source", "lease"
    ])

    if rc != 0:
        return None

    # Busca IPs NAT de libvirt: 192.168.122.x/24
    m = re.search(r"(192\.168\.122\.\d+)/\d+", out)
    return m.group(1) if m else None


def main():
    print("===== ACCESO SSH A VM1 Y VM3 =====\n")

    for vm, worker in VMS_ACCESO.items():
        ip = obtener_ip(vm, worker)
        worker_ip = cfg.WORKERS[worker]["ip"]

        print(f"{vm} -> {worker}")

        if not ip:
            print("  IP NAT: NO ENCONTRADA")
            print("  Verifique que la VM esté running y haya recibido DHCP.\n")
            continue

        print(f"  IP NAT: {ip}")
        print("  Desde Windows PowerShell:")
        print(
            f"  ssh -J {cfg.USUARIO_SSH}@{cfg.HEADNODE_IP_EXTERNA},"
            f"{cfg.USUARIO_SSH}@{worker_ip} "
            f"ubuntu@{ip}"
        )
        print()


if __name__ == "__main__":
    main()
